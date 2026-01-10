#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua
======================================
Versión: 1.9.0-enhanced-report

CAMBIOS v1.9.0:
- Análisis por períodos de 8 horas (Noche/Mañana/Tarde)
- Evaluación automática de nivel de riesgo (BAJO/MODERADO/ALTO/CRÍTICO)
- Análisis de tendencias temporales (SpO2 y FC)
- System prompt médico especializado
- Reducción de 50 a 10 últimos registros (eficiencia de tokens)
- Prompt mejorado con estructura clínica profesional
"""

import eventlet
eventlet.monkey_patch()

import os
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import deque
from io import BytesIO
import numpy as np

try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    psycopg2, pool, RealDictCursor = None, None, None

from flask import Flask, render_template, jsonify, request, send_file
from flask_socketio import SocketIO
from weasyprint import HTML
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(".env")
client = OpenAI()
LLM_MODEL = "gpt-4o-mini"

SYSTEM_NAME = "HumanS – Monitorización Vital Continua"
ALGORITHM_VERSION = "1.9.0-enhanced-report"

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "tu-email@gmail.com")  # Tu email de Brevo

email_config = {"email_to": "", "patient_name": "", "patient_room": "", "patient_residence": ""}

CRITICAL_SPO2 = 92
CRITICAL_HR_LOW = 60
CRITICAL_HR_HIGH = 150
EMAIL_COOLDOWN = 300

MAX_HISTORY = 120
spo2_hist = deque(maxlen=MAX_HISTORY)
hr_hist = deque(maxlen=MAX_HISTORY)

packet_count = 0
current_distance = 0.0
current_rssi = None
last_packet_time = None
last_spo2_alert_time = 0
last_hr_alert_time = 0
last_spo2_critical = False
last_hr_critical = False

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'humans-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

DATABASE_URL = os.environ.get("DATABASE_URL")
db_pool = None

# ============================================================
# DATABASE FUNCTIONS
# ============================================================

def init_db_pool():
    global db_pool
    if not POSTGRES_AVAILABLE or not DATABASE_URL: return False
    try:
        conn_str = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL.startswith("postgres://") else DATABASE_URL
        db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=conn_str)
        print("✅ PostgreSQL conectado")
        return True
    except Exception as e:
        print(f"❌ PostgreSQL: {e}")
        return False

def get_db_connection(): return db_pool.getconn() if db_pool else None
def release_db_connection(conn):
    if db_pool and conn: db_pool.putconn(conn)

def init_database():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vital_signs (id SERIAL PRIMARY KEY, timestamp TIMESTAMPTZ DEFAULT NOW(),
                    spo2 INTEGER, hr INTEGER, spo2_critical BOOLEAN, hr_critical BOOLEAN, distance FLOAT, rssi INTEGER, patient_id VARCHAR(100));
                CREATE TABLE IF NOT EXISTS alerts (id SERIAL PRIMARY KEY, timestamp TIMESTAMPTZ DEFAULT NOW(),
                    alert_type VARCHAR(50), spo2 INTEGER, hr INTEGER, message TEXT, email_sent BOOLEAN, email_to VARCHAR(255), patient_id VARCHAR(100));
                CREATE TABLE IF NOT EXISTS email_config (id SERIAL PRIMARY KEY, email_to VARCHAR(255), patient_name VARCHAR(255),
                    patient_room VARCHAR(100), patient_residence VARCHAR(255), updated_at TIMESTAMPTZ DEFAULT NOW());
            """)
            conn.commit()
            load_email_config()
    except: conn.rollback()
    finally: release_db_connection(conn)

def load_email_config():
    global email_config
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM email_config ORDER BY updated_at DESC LIMIT 1")
            r = cur.fetchone()
            if r:
                email_config = {"email_to": r.get("email_to",""), "patient_name": r.get("patient_name",""),
                               "patient_room": r.get("patient_room",""), "patient_residence": r.get("patient_residence","")}
                print(f"✅ Email config: {email_config['email_to']}")
    except: pass
    finally: release_db_connection(conn)

def save_email_config_db(email_to, patient_name, patient_room, patient_residence):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO email_config (email_to,patient_name,patient_room,patient_residence) VALUES (%s,%s,%s,%s)",
                       (email_to, patient_name, patient_room, patient_residence))
            conn.commit()
    except: conn.rollback()
    finally: release_db_connection(conn)

def save_vital(spo2, hr, spo2_crit, hr_crit, dist, rssi, patient_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO vital_signs (spo2,hr,spo2_critical,hr_critical,distance,rssi,patient_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (spo2, hr, spo2_crit, hr_crit, dist, rssi, patient_id))
            conn.commit()
    except: conn.rollback()
    finally: release_db_connection(conn)

def save_alert(alert_type, spo2, hr, msg, sent, email_to, patient_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO alerts (alert_type,spo2,hr,message,email_sent,email_to,patient_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (alert_type, spo2, hr, msg, sent, email_to, patient_id))
            conn.commit()
    except: conn.rollback()
    finally: release_db_connection(conn)

# ============================================================
# CLINICAL ANALYSIS (from server9_advance.py)
# ============================================================

def classify_spo2_episodes(spo2_list, hr_list, threshold=92):
    """
    Clasifica descensos de SpO2 en eventos clínicos reales vs artefactos.
    
    CRITERIOS CLÍNICOS:
    - Evento clínico: duración >= 30 muestras (30s), variación HR > 5 bpm
    - Artefacto: descensos breves, recuperación rápida, sin correlación HR
    """
    MIN_SAMPLES = 30
    MIN_HR_VARIATION = 5
    
    clinical = 0
    artifacts = 0
    buffer = []

    for s, h in zip(spo2_list, hr_list):
        if s < threshold:
            buffer.append((s, h))
        else:
            if buffer:
                duration = len(buffer)
                hr_values = [x[1] for x in buffer]
                hr_var = max(hr_values) - min(hr_values) if hr_values else 0
                
                if duration >= MIN_SAMPLES and hr_var > MIN_HR_VARIATION:
                    clinical += 1
                else:
                    artifacts += 1
                buffer = []
    
    if buffer:
        duration = len(buffer)
        hr_values = [x[1] for x in buffer]
        hr_var = max(hr_values) - min(hr_values) if hr_values else 0
        if duration >= MIN_SAMPLES and hr_var > MIN_HR_VARIATION:
            clinical += 1
        else:
            artifacts += 1
    
    return clinical, artifacts


def get_8hour_periods(hours=24):
    """
    Obtiene estadísticas por períodos de 8 horas.
    Retorna lista de diccionarios con stats de cada período.
    """
    from collections import defaultdict
    
    if not db_pool:
        # Sin base de datos: usar datos en memoria (sesión actual)
        if not spo2_hist or len(spo2_hist) < 10:
            return []
        
        # Simular un único período con los datos actuales
        spo2_arr = np.array(list(spo2_hist))
        hr_arr = np.array(list(hr_hist))
        now = datetime.now(timezone.utc)
        
        return [{
            "period_name": "Sesión actual",
            "date": now.strftime("%Y-%m-%d"),
            "start": "Inicio sesión",
            "end": now.strftime("%H:%M"),
            "samples": len(spo2_arr),
            "spo2_min": int(np.min(spo2_arr)),
            "spo2_max": int(np.max(spo2_arr)),
            "spo2_avg": round(float(np.mean(spo2_arr)), 1),
            "hr_min": int(np.min(hr_arr)),
            "hr_max": int(np.max(hr_arr)),
            "hr_avg": round(float(np.mean(hr_arr)), 1),
        }]
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"""
                SELECT spo2, hr, timestamp 
                FROM vital_signs 
                WHERE timestamp > NOW() - INTERVAL '{hours} hours'
                ORDER BY timestamp ASC
            """)
            rows = cur.fetchall()
            
            if not rows:
                return []
            
            # Agrupar por períodos de 8 horas
            periods = defaultdict(list)
            
            for row in rows:
                ts = row['timestamp']
                hour = ts.hour
                if hour < 8:
                    period_name = "Noche (00:00-08:00)"
                elif hour < 16:
                    period_name = "Mañana (08:00-16:00)"
                else:
                    period_name = "Tarde (16:00-24:00)"
                
                key = f"{ts.strftime('%Y-%m-%d')}_{period_name}"
                periods[key].append({
                    'spo2': row['spo2'],
                    'hr': row['hr'],
                    'timestamp': ts,
                    'period_name': period_name
                })
            
            result = []
            for key, data in sorted(periods.items()):
                if len(data) < 5:
                    continue
                    
                spo2_vals = [d['spo2'] for d in data]
                hr_vals = [d['hr'] for d in data]
                
                result.append({
                    "period_name": data[0]['period_name'],
                    "date": data[0]['timestamp'].strftime("%Y-%m-%d"),
                    "start": data[0]['timestamp'].strftime("%H:%M"),
                    "end": data[-1]['timestamp'].strftime("%H:%M"),
                    "samples": len(data),
                    "spo2_min": int(np.min(spo2_vals)),
                    "spo2_max": int(np.max(spo2_vals)),
                    "spo2_avg": round(float(np.mean(spo2_vals)), 1),
                    "hr_min": int(np.min(hr_vals)),
                    "hr_max": int(np.max(hr_vals)),
                    "hr_avg": round(float(np.mean(hr_vals)), 1),
                })
            
            return result
            
    except Exception as e:
        print(f"[ERROR] get_8hour_periods: {e}")
        return []
    finally:
        release_db_connection(conn)


def calculate_trend(values):
    """Calcula tendencia temporal: mejorando, estable, empeorando."""
    if not values or len(values) < 6:
        return "Datos insuficientes"
    
    mid = len(values) // 2
    first_half = np.mean(values[:mid])
    second_half = np.mean(values[mid:])
    
    diff = second_half - first_half
    
    if abs(diff) < 1:
        return "Estable"
    elif diff > 0:
        return f"Ascendente (+{diff:.1f})"
    else:
        return f"Descendente ({diff:.1f})"


def assess_risk_level(summary):
    """Evalúa nivel de riesgo global del paciente."""
    score = 0
    reasons = []
    
    # SpO2
    if summary['spo2_avg'] < 88:
        score += 4
        reasons.append("SpO2 media muy baja")
    elif summary['spo2_avg'] < 90:
        score += 3
        reasons.append("SpO2 media baja")
    elif summary['spo2_avg'] < 92:
        score += 2
        reasons.append("SpO2 media límite")
    elif summary['spo2_avg'] < 94:
        score += 1
    
    if summary['spo2_clinical_events'] >= 3:
        score += 3
        reasons.append(f"{summary['spo2_clinical_events']} eventos de hipoxemia")
    elif summary['spo2_clinical_events'] > 0:
        score += summary['spo2_clinical_events']
    
    if summary['spo2_p5'] < 85:
        score += 3
        reasons.append("Nadir SpO2 crítico")
    elif summary['spo2_p5'] < 88:
        score += 2
        reasons.append("Nadir SpO2 bajo")
    elif summary['spo2_p5'] < 90:
        score += 1
    
    pct_below_90 = 100 * summary['spo2_below_90'] / max(summary['total_samples'], 1)
    if pct_below_90 > 10:
        score += 2
        reasons.append(f"{pct_below_90:.1f}% tiempo en hipoxemia")
    elif pct_below_90 > 5:
        score += 1
    
    # FC
    if summary['hr_avg'] < 50 or summary['hr_avg'] > 120:
        score += 3
        reasons.append("FC media muy alterada")
    elif summary['hr_avg'] < 55 or summary['hr_avg'] > 110:
        score += 2
        reasons.append("FC media alterada")
    elif summary['hr_avg'] < 60 or summary['hr_avg'] > 100:
        score += 1
    
    pct_brady = 100 * summary['hr_bradycardia'] / max(summary['total_samples'], 1)
    pct_tachy = 100 * summary['hr_tachycardia'] / max(summary['total_samples'], 1)
    
    if pct_brady > 20:
        score += 2
        reasons.append("Bradicardia frecuente")
    elif pct_brady > 10:
        score += 1
    
    if pct_tachy > 20:
        score += 2
        reasons.append("Taquicardia frecuente")
    elif pct_tachy > 10:
        score += 1
    
    if summary['spo2_std'] > 5:
        score += 1
        reasons.append("Alta variabilidad SpO2")
    
    if score >= 7:
        level, emoji, action = "CRÍTICO", "🔴", "Requiere evaluación médica urgente"
    elif score >= 5:
        level, emoji, action = "ALTO", "🟠", "Evaluación médica recomendada en 24h"
    elif score >= 3:
        level, emoji, action = "MODERADO", "🟡", "Vigilancia estrecha, considerar consulta"
    else:
        level, emoji, action = "BAJO", "🟢", "Continuar monitorización rutinaria"
    
    return {
        "level": level,
        "emoji": emoji,
        "action": action,
        "score": score,
        "reasons": reasons if reasons else ["Parámetros dentro de rangos normales"]
    }

def get_vital_signs_for_report(hours=24):
    """Obtiene datos vitales para el informe"""
    if not db_pool:
        if not spo2_hist: return None
        # Crear lista de últimos 50 valores con timestamp simulado
        last_50 = []
        now = datetime.now(timezone.utc)
        for i, (s, h) in enumerate(zip(list(spo2_hist)[-50:], list(hr_hist)[-50:])):
            last_50.append({
                "timestamp": (now - timedelta(seconds=(50-i))).strftime("%H:%M:%S"),
                "spo2": s,
                "hr": h
            })
        return {
            "spo2_list": list(spo2_hist),
            "hr_list": list(hr_hist),
            "timestamp_start": "Sesión actual",
            "timestamp_end": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_samples": len(spo2_hist),
            "last_50_readings": last_50
        }
    
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Obtener todos los datos del período
            cur.execute(f"""
                SELECT spo2, hr, timestamp 
                FROM vital_signs 
                WHERE timestamp > NOW() - INTERVAL '{hours} hours'
                ORDER BY timestamp ASC
            """)
            rows = cur.fetchall()
            if not rows: return None
            
            # Obtener últimos 50 registros
            cur.execute("""
                SELECT spo2, hr, timestamp 
                FROM vital_signs 
                ORDER BY timestamp DESC 
                LIMIT 50
            """)
            last_50_rows = cur.fetchall()
            last_50_rows.reverse()  # Ordenar cronológicamente
            
            last_50 = []
            for r in last_50_rows:
                last_50.append({
                    "timestamp": r['timestamp'].strftime("%Y-%m-%d %H:%M:%S"),
                    "spo2": r['spo2'],
                    "hr": r['hr']
                })
            
            return {
                "spo2_list": [r['spo2'] for r in rows],
                "hr_list": [r['hr'] for r in rows],
                "timestamp_start": rows[0]['timestamp'].strftime("%Y-%m-%d %H:%M:%S UTC"),
                "timestamp_end": rows[-1]['timestamp'].strftime("%Y-%m-%d %H:%M:%S UTC"),
                "total_samples": len(rows),
                "last_50_readings": last_50
            }
    except Exception as e:
        print(f"[ERROR] get_vital_signs_for_report: {e}")
        return None
    finally:
        release_db_connection(conn)

def process_data_for_analysis(hours=24):
    """Procesa datos y genera estadísticas para el informe - VERSIÓN MEJORADA"""
    data = get_vital_signs_for_report(hours)
    if not data or not data["spo2_list"]:
        return None

    spo2 = np.array(data["spo2_list"])
    hr = np.array(data["hr_list"])

    clinical, artifacts = classify_spo2_episodes(data["spo2_list"], data["hr_list"])
    
    # Obtener períodos de 8 horas
    periods_8h = get_8hour_periods(hours)
    
    # Calcular tendencias
    trend_spo2 = calculate_trend(data["spo2_list"])
    trend_hr = calculate_trend(data["hr_list"])

    summary = {
        "timestamp_start": data["timestamp_start"],
        "timestamp_end": data["timestamp_end"],
        "total_samples": data["total_samples"],
        # SpO2
        "spo2_avg": round(float(np.mean(spo2)), 1),
        "spo2_min": int(np.min(spo2)),
        "spo2_max": int(np.max(spo2)),
        "spo2_p5": int(np.percentile(spo2, 5)),
        "spo2_std": round(float(np.std(spo2)), 2),
        "spo2_below_90": int(np.sum(spo2 < 90)),
        "spo2_below_92": int(np.sum(spo2 < 92)),
        "spo2_clinical_events": clinical,
        "spo2_artifact_events": artifacts,
        "spo2_trend": trend_spo2,
        # HR
        "hr_avg": round(float(np.mean(hr)), 1),
        "hr_min": int(np.min(hr)),
        "hr_max": int(np.max(hr)),
        "hr_std": round(float(np.std(hr)), 2),
        "hr_bradycardia": int(np.sum(hr < 60)),
        "hr_tachycardia": int(np.sum(hr > 100)),
        "hr_trend": trend_hr,
        # Períodos de 8 horas
        "periods_8h": periods_8h,
        # Últimos registros (reducido a 10)
        "last_10_readings": data.get("last_50_readings", [])[-10:]
    }
    
    # Calcular nivel de riesgo
    summary["risk"] = assess_risk_level(summary)
    
    return summary

def get_statistics(hours=24):
    """Estadísticas básicas"""
    if not db_pool:
        if not spo2_hist: return None
        return {"total_samples": len(spo2_hist), "spo2_avg": round(sum(spo2_hist)/len(spo2_hist),2),
                "spo2_min": min(spo2_hist), "spo2_max": max(spo2_hist),
                "hr_avg": round(sum(hr_hist)/len(hr_hist),2) if hr_hist else 0,
                "hr_min": min(hr_hist) if hr_hist else 0, "hr_max": max(hr_hist) if hr_hist else 0}
    conn = get_db_connection()
    if not conn: return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) as total_samples, ROUND(AVG(spo2)::numeric,2) as spo2_avg, MIN(spo2) as spo2_min, MAX(spo2) as spo2_max, ROUND(AVG(hr)::numeric,2) as hr_avg, MIN(hr) as hr_min, MAX(hr) as hr_max FROM vital_signs WHERE timestamp > NOW() - INTERVAL '{hours} hours'")
            return dict(cur.fetchone() or {})
    except: return None
    finally: release_db_connection(conn)

# ============================================================
# LLM PROMPT FOR MEDICAL REPORT - VERSIÓN 2.0
# ============================================================

# System prompt mejorado para la API
SYSTEM_PROMPT_MEDICAL = """Eres un médico internista con experiencia en telemonitorización 
de pacientes crónicos en entornos residenciales. Generas informes clínicos profesionales 
siguiendo estándares de documentación médica.

PRINCIPIOS:
- Objetividad científica sin alarmismo innecesario
- Diferenciación clara entre eventos clínicos reales y artefactos técnicos
- Recomendaciones accionables y proporcionadas al nivel de riesgo
- Lenguaje comprensible para cuidadores y familiares no médicos
- Énfasis en tendencias temporales y evolución

Devuelve siempre HTML válido y completo, sin explicaciones ni bloques de código markdown."""


def generate_llm_prompt(summary, patient):
    """Genera prompt profesional para el informe médico - VERSIÓN 2.0"""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Formatear tabla de períodos de 8 horas
    periods_table = ""
    if summary.get('periods_8h'):
        periods_table = "\n## ANÁLISIS POR PERÍODOS DE 8 HORAS:\n"
        periods_table += "| Período | Fecha | Muestras | SpO2 Mín | SpO2 Máx | SpO2 Prom | FC Mín | FC Máx | FC Prom |\n"
        periods_table += "|---------|-------|----------|----------|----------|-----------|--------|--------|--------|\n"
        for p in summary['periods_8h']:
            date_str = p.get('date', 'N/A')
            periods_table += f"| {p['period_name']} | {date_str} | {p['samples']} | {p['spo2_min']}% | {p['spo2_max']}% | {p['spo2_avg']}% | {p['hr_min']} | {p['hr_max']} | {p['hr_avg']} |\n"
    
    # Formatear últimos 10 registros
    last_10_table = ""
    last_10 = summary.get('last_10_readings', [])
    if last_10:
        last_10_table = "\n## ÚLTIMOS 10 REGISTROS:\n"
        last_10_table += "| # | Timestamp | SpO2 | FC |\n"
        for i, r in enumerate(last_10, 1):
            spo2_mark = " ⚠️" if r['spo2'] < 92 else ""
            hr_mark = " ⚠️" if r['hr'] < 60 or r['hr'] > 100 else ""
            last_10_table += f"| {i} | {r['timestamp']} | {r['spo2']}%{spo2_mark} | {r['hr']} bpm{hr_mark} |\n"
    
    # Información de riesgo
    risk = summary.get('risk', {})
    risk_info = f"""
## EVALUACIÓN DE RIESGO AUTOMÁTICA:
• Nivel: {risk.get('emoji', '⚪')} {risk.get('level', 'No calculado')}
• Puntuación: {risk.get('score', 0)}/10
• Acción sugerida: {risk.get('action', 'N/A')}
• Factores: {', '.join(risk.get('reasons', ['N/A']))}
"""

    # Contexto del paciente
    patient_context = ""
    age = patient.get('age')
    if age:
        try:
            age_int = int(age)
            if age_int >= 80:
                patient_context = "⚠️ Paciente muy mayor (≥80 años): considerar fragilidad y comorbilidades."
            elif age_int >= 65:
                patient_context = "Paciente geriátrico: valorar contexto clínico global."
        except:
            pass

    return f"""Genera un informe médico profesional en HTML completo y válido.

══════════════════════════════════════════════════════════════
DATOS DEL PACIENTE
══════════════════════════════════════════════════════════════
• Nombre: {patient.get('name', 'No especificado')}
• Edad: {patient.get('age', 'No especificado')} años
• Residencia: {patient.get('residence', 'No especificado')}
• Habitación: {patient.get('room', 'No especificado')}
{patient_context}

══════════════════════════════════════════════════════════════
PERÍODO DE MONITORIZACIÓN
══════════════════════════════════════════════════════════════
• Inicio: {summary['timestamp_start']}
• Fin: {summary['timestamp_end']}
• Duración: {summary['total_samples']:,} muestras (~{max(1, summary['total_samples']//60)} minutos)
• Dispositivo: Pulsioxímetro HUMANS (precisión ±2% SpO2, ±3 bpm)

══════════════════════════════════════════════════════════════
SATURACIÓN DE OXÍGENO (SpO2)
══════════════════════════════════════════════════════════════
• Media: {summary['spo2_avg']}%
• Mínima: {summary['spo2_min']}% | Máxima: {summary['spo2_max']}%
• Percentil 5: {summary['spo2_p5']}%
• Desviación estándar: {summary['spo2_std']}%
• Tendencia: {summary.get('spo2_trend', 'N/A')}
• Muestras < 90%: {summary['spo2_below_90']} ({100*summary['spo2_below_90']/max(summary['total_samples'],1):.2f}%)
• Muestras < 92%: {summary['spo2_below_92']} ({100*summary['spo2_below_92']/max(summary['total_samples'],1):.2f}%)

══════════════════════════════════════════════════════════════
FRECUENCIA CARDÍACA (FC)
══════════════════════════════════════════════════════════════
• Media: {summary['hr_avg']} bpm
• Mínima: {summary['hr_min']} bpm | Máxima: {summary['hr_max']} bpm
• Desviación estándar: {summary['hr_std']} bpm
• Tendencia: {summary.get('hr_trend', 'N/A')}
• Bradicardia (<60 bpm): {summary['hr_bradycardia']} muestras
• Taquicardia (>100 bpm): {summary['hr_tachycardia']} muestras

══════════════════════════════════════════════════════════════
ANÁLISIS CLÍNICO DE EVENTOS
══════════════════════════════════════════════════════════════
• Eventos clínicos de hipoxemia sostenida: {summary['spo2_clinical_events']}
• Artefactos de señal (descensos transitorios): {summary['spo2_artifact_events']}

NOTA: Los artefactos son descensos breves (<30s) por movimiento del sensor, 
SIN correlación con cambios en frecuencia cardíaca. NO representan hipoxemia clínica.
{risk_info}
{periods_table}
{last_10_table}
══════════════════════════════════════════════════════════════
ESTRUCTURA DEL INFORME HTML
══════════════════════════════════════════════════════════════

Genera el informe con estas secciones EN ESTE ORDEN:

1. **ENCABEZADO**
   - Título: "Informe de Monitorización Vital Continua"
   - Subtítulo con nombre del paciente
   - Badge/etiqueta con nivel de riesgo (color según nivel)

2. **DATOS DEL PACIENTE** (tabla compacta)
   - Nombre, Edad, Residencia, Habitación
   - Período de monitorización

3. **RESUMEN EJECUTIVO** (máximo 4 líneas)
   - Estado general
   - Nivel de riesgo con emoji
   - Hallazgo más relevante
   - Acción recomendada

4. **TABLA DE PARÁMETROS VITALES GLOBALES**
   | Parámetro | Media | Mín | Máx | Tendencia | Estado |
   - SpO2 y FC con sus valores
   - Colorear según normalidad

5. **TABLA DE ANÁLISIS POR PERÍODOS DE 8 HORAS** ⭐ MUY IMPORTANTE
   - Mostrar cada período con: nombre, fecha, muestras, SpO2 (mín/máx/prom), FC (mín/máx/prom)
   - Resaltar períodos con valores alterados en amarillo/rojo
   - Esta tabla muestra la evolución temporal del paciente

6. **ANÁLISIS DE EVENTOS**
   - Diferenciar eventos clínicos vs artefactos
   - Explicar en lenguaje comprensible

7. **INTERPRETACIÓN CLÍNICA**
   - Valoración médica objetiva
   - Correlación SpO2-FC
   - Análisis de tendencias

8. **TABLA DE ÚLTIMOS 10 REGISTROS**
   - Solo 10 registros (no 50)
   - Resaltar valores críticos en rojo

9. **RECOMENDACIONES** (según nivel de riesgo)
   - 🟢 BAJO: Continuar monitorización
   - 🟡 MODERADO: Vigilancia estrecha
   - 🟠 ALTO: Evaluación médica en 24h
   - 🔴 CRÍTICO: Atención urgente

10. **PIE DE PÁGINA**
    - Disclaimer legal
    - Fecha: {now_utc}
    - Sistema: {SYSTEM_NAME} v{ALGORITHM_VERSION}

══════════════════════════════════════════════════════════════
ESTILOS CSS
══════════════════════════════════════════════════════════════
- Fuente: Arial, Helvetica, sans-serif
- Encabezados: #1a5276 (azul oscuro)
- Riesgo BAJO: #27ae60 (verde)
- Riesgo MODERADO: #f39c12 (amarillo/naranja)
- Riesgo ALTO: #e67e22 (naranja)
- Riesgo CRÍTICO: #c0392b (rojo)
- Tablas: bordes #ddd, zebra striping, padding 8px
- Badges de riesgo: bordes redondeados, padding 5px 15px
- Optimizado para impresión A4
- Fuente pequeña (11px) para tablas de datos

Devuelve SOLO HTML válido y completo. Sin explicaciones ni markdown."""

# ============================================================
# EMAIL FUNCTIONS
# ============================================================

def check_email_config():
    issues = []
    if not BREVO_API_KEY: issues.append("BREVO_API_KEY no configurado")
    if not email_config.get("email_to"): issues.append("Email destinatario no configurado")
    return {"configured": len(issues)==0, "issues": issues, "provider": "Brevo API"}

def generate_email_html(alert_type, spo2, hr, patient_info):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")
    color = "#17a2b8" if alert_type == 'test' else "#dc3545"
    if alert_type == 'test':
        title = "🧪 TEST - Sistema de Alertas"
    elif alert_type == 'spo2':
        title = f"⚠️ ALERTA: SpO2 Bajo ({spo2}%)"
    else:
        title = f"⚠️ ALERTA: {'Bradicardia' if hr < CRITICAL_HR_LOW else 'Taquicardia'} ({hr} bpm)"
    
    return f"""<!DOCTYPE html><html><body style="font-family:Arial;max-width:600px;margin:auto;padding:20px;">
    <div style="background:{color};color:white;padding:20px;border-radius:10px 10px 0 0;text-align:center;"><h1 style="margin:0;">{title}</h1></div>
    <div style="background:#f8f9fa;padding:20px;border-radius:0 0 10px 10px;">
    <div style="font-size:42px;font-weight:bold;color:{color};text-align:center;margin:20px;">SpO2: {spo2}% | HR: {hr} bpm</div>
    <p><strong>Paciente:</strong> {patient_info.get('name','N/A')} | <strong>Hab:</strong> {patient_info.get('room','N/A')}</p>
    <p style="font-size:11px;color:#888;text-align:center;">{SYSTEM_NAME} v{ALGORITHM_VERSION} | {now}</p>
    </div></body></html>"""

def send_email_brevo(recipient, subject, html):
    if not BREVO_API_KEY: return {"success": False, "error": "API key no configurado"}
    if not recipient: return {"success": False, "error": "Sin destinatario"}
    print(f"📧 Enviando a {recipient}...")
    try:
        r = requests.post("https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "accept": "application/json"},
            json={"sender": {"email": EMAIL_FROM}, "to": [{"email": recipient}], "subject": subject, "htmlContent": html}, timeout=30)
        if r.status_code == 201:
            print(f"✅ Email enviado! ID: {r.json().get('messageId')}")
            return {"success": True}
        print(f"❌ Error: {r.text}")
        return {"success": False, "error": r.json().get("message", f"HTTP {r.status_code}")}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {"success": False, "error": str(e)}

def send_alert_email(alert_type, spo2, hr):
    recipient = email_config.get("email_to")
    if not recipient: return False
    patient_info = {"name": email_config.get("patient_name","N/A"), "room": email_config.get("patient_room","N/A")}
    if alert_type == 'spo2':
        subject = f"⚠️ ALERTA HumanS - SpO2 {spo2}% - {patient_info['name']}"
    else:
        cond = "Bradicardia" if hr < CRITICAL_HR_LOW else "Taquicardia"
        subject = f"⚠️ ALERTA HumanS - {cond} {hr}bpm - {patient_info['name']}"
    result = send_email_brevo(recipient, subject, generate_email_html(alert_type, spo2, hr, patient_info))
    save_alert(alert_type, spo2, hr, subject, result["success"], recipient, patient_info["name"])
    if result["success"]:
        socketio.emit('alert_sent', {'type': alert_type, 'message': f'Email enviado a {recipient}'})
    return result["success"]

def send_alert_async(alert_type, spo2, hr):
    eventlet.spawn(lambda: send_alert_email(alert_type, spo2, hr))

# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index(): return render_template("index11.html")

@app.route("/api/data", methods=["POST"])
def receive_data():
    global packet_count, current_distance, current_rssi, last_packet_time
    global last_spo2_critical, last_hr_critical, last_spo2_alert_time, last_hr_alert_time
    try:
        p = request.get_json(force=True)
        spo2, hr = p.get("spo2"), p.get("hr")
        if spo2 is None or hr is None: return jsonify({"error": "Missing data"}), 400
        if not (35 <= spo2 <= 100) or not (25 <= hr <= 250): return jsonify({"error": "Out of range"}), 400
        
        packet_count += 1
        current_distance = p.get("distance", 0)
        current_rssi = p.get("rssi")
        last_packet_time = datetime.now(timezone.utc)
        spo2_hist.append(spo2)
        hr_hist.append(hr)
        
        spo2_crit = spo2 < CRITICAL_SPO2
        hr_crit = hr < CRITICAL_HR_LOW or hr > CRITICAL_HR_HIGH
        eventlet.spawn(save_vital, spo2, hr, spo2_crit, hr_crit, current_distance, current_rssi, email_config.get("patient_name"))
        
        now = time.time()
        if spo2_crit and not last_spo2_critical:
            print(f"🚨 SpO2 CRÍTICO: {spo2}%")
            if now - last_spo2_alert_time > EMAIL_COOLDOWN:
                send_alert_async('spo2', spo2, hr)
                last_spo2_alert_time = now
        if hr_crit and not last_hr_critical:
            print(f"🚨 HR CRÍTICO: {hr} bpm")
            if now - last_hr_alert_time > EMAIL_COOLDOWN:
                send_alert_async('hr', spo2, hr)
                last_hr_alert_time = now
        
        last_spo2_critical, last_hr_critical = spo2_crit, hr_crit
        
        socketio.emit('update', {"spo2": spo2, "hr": hr, "spo2_history": list(spo2_hist), 
                                "hr_history": list(hr_hist), "spo2_critical": spo2_crit, "hr_critical": hr_crit})
        socketio.emit('raw_update', {"count": packet_count, "distance": current_distance, "rssi": current_rssi})
        return jsonify({"status": "ok", "packet": packet_count})
    except Exception as e:
        print(f"[ERROR] {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/report/pdf", methods=["POST"])
def api_report_pdf():
    """Genera informe médico PDF con análisis clínico completo"""
    print("=== GENERANDO INFORME MÉDICO ===")
    
    try:
        patient = request.get_json(silent=True) or {}
        print(f"[LOG] Paciente: {patient}")
        
        summary = process_data_for_analysis(hours=24)
        print(f"[LOG] Resumen: {summary}")
        
        if not summary:
            return jsonify({"error": "No hay datos suficientes para generar el informe"}), 400

        prompt = generate_llm_prompt(summary, patient)
        print(f"[LOG] Prompt generado ({len(prompt)} chars)")

        print("[LOG] Llamando a OpenAI...")
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_MEDICAL},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=5500
        )
        
        html_content = response.choices[0].message.content.strip()
        print(f"[LOG] HTML recibido ({len(html_content)} chars)")

        if html_content.startswith("```html"):
            html_content = html_content[7:]
        elif html_content.startswith("```"):
            html_content = html_content[3:]
        if html_content.endswith("```"):
            html_content = html_content[:-3]
        html_content = html_content.strip()

        if "<html" not in html_content.lower() and "<!doctype" not in html_content.lower():
            raise ValueError("La respuesta no es HTML válido")

        print("[LOG] Generando PDF...")
        pdf = BytesIO()
        HTML(string=html_content).write_pdf(pdf)
        pdf.seek(0)
        print("[LOG] PDF generado ✓")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        patient_name = patient.get('name', 'paciente').replace(' ', '_')
        filename = f"informe_{patient_name}_{timestamp}.pdf"

        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Error al generar el informe: {str(e)}"}), 500

@app.route("/api/email/config", methods=["GET"])
def get_email_cfg():
    c = check_email_config()
    return jsonify({"email_to": email_config.get("email_to",""), "patient_name": email_config.get("patient_name",""),
                   "smtp_configured": c["configured"], "smtp_issues": c["issues"], "provider": c["provider"],
                   "thresholds": {"spo2_critical": CRITICAL_SPO2, "hr_low": CRITICAL_HR_LOW, "hr_high": CRITICAL_HR_HIGH}})

@app.route("/api/email/config", methods=["POST"])
def set_email_cfg():
    global email_config
    d = request.get_json()
    email = d.get("email_to","").strip()
    if not email or "@" not in email: return jsonify({"error": "Email inválido"}), 400
    email_config = {"email_to": email, "patient_name": d.get("patient_name",""),
                   "patient_room": d.get("patient_room",""), "patient_residence": d.get("patient_residence","")}
    save_email_config_db(email, email_config["patient_name"], email_config["patient_room"], email_config["patient_residence"])
    print(f"✅ Email configurado: {email}")
    return jsonify({"status": "ok", "email_to": email})

@app.route("/api/email/test", methods=["POST"])
def test_email():
    d = request.get_json() or {}
    recipient = d.get("email_to") or email_config.get("email_to")
    if not recipient: return jsonify({"error": "No hay email configurado"}), 400
    if not BREVO_API_KEY: return jsonify({"error": "BREVO_API_KEY no configurado"}), 500
    patient = d.get("patient_name") or email_config.get("patient_name","Prueba")
    result = send_email_brevo(recipient, f"🧪 TEST HumanS - {patient}", generate_email_html('test', 97, 72, {"name": patient}))
    if result["success"]: return jsonify({"status": "ok", "message": f"✅ Email enviado a {recipient}"})
    return jsonify({"error": result.get("error")}), 500

@app.route("/api/diagnostics", methods=["GET"])
def diagnostics():
    return jsonify({"packet_count": packet_count, "version": ALGORITHM_VERSION,
                   "email": check_email_config(), "database": "ok" if db_pool else "no"})

@app.route("/api/data/statistics", methods=["GET"])
def stats(): 
    return jsonify(get_statistics() or {"error": "No data"})

# ============================================================
# WEBSOCKET
# ============================================================

@socketio.on('connect')
def on_connect():
    print(f'[WS] Conectado ({len(spo2_hist)} datos)')
    if spo2_hist:
        socketio.emit('update', {"spo2": spo2_hist[-1], "hr": hr_hist[-1], "spo2_history": list(spo2_hist),
                                "hr_history": list(hr_hist), "spo2_critical": spo2_hist[-1] < CRITICAL_SPO2,
                                "hr_critical": hr_hist[-1] < CRITICAL_HR_LOW or hr_hist[-1] > CRITICAL_HR_HIGH})
    socketio.emit('raw_update', {"count": packet_count, "distance": current_distance, "rssi": current_rssi})

@socketio.on('disconnect')
def on_disconnect(): print('[WS] Desconectado')

# ============================================================
# INICIALIZACIÓN (se ejecuta siempre, incluso con gunicorn)
# ============================================================

print(f"""
╔══════════════════════════════════════════════════════════════╗
║  {SYSTEM_NAME}
║  Versión: {ALGORITHM_VERSION}
╠══════════════════════════════════════════════════════════════╣
║  DATABASE_URL: {'✅ Configurado' if DATABASE_URL else '❌ No configurado'}
║  BREVO_API_KEY: {'✅ Configurado' if BREVO_API_KEY else '❌ No configurado'}
║  OPENAI_API_KEY: {'✅ Configurado' if os.environ.get('OPENAI_API_KEY') else '❌ No configurado'}
╚══════════════════════════════════════════════════════════════╝
""")

# Inicializar base de datos al cargar el módulo
if init_db_pool():
    init_database()
else:
    print("⚠️ Ejecutando SIN base de datos (solo memoria)")

# ============================================================
# MAIN (solo para ejecución local directa)
# ============================================================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))