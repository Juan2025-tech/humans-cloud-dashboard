#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua
======================================
Versión: 1.8.0-improved-report

CAMBIOS:
- Informe médico PDF mejorado con análisis clínico
- Clasificación de eventos clínicos vs artefactos
- Prompt profesional para LLM
- Resend API para emails
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
ALGORITHM_VERSION = "1.8.0-improved-report"

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "HumanS <onboarding@resend.dev>")

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
    """Procesa datos y genera estadísticas para el informe"""
    data = get_vital_signs_for_report(hours)
    if not data or not data["spo2_list"]:
        return None

    spo2 = np.array(data["spo2_list"])
    hr = np.array(data["hr_list"])

    clinical, artifacts = classify_spo2_episodes(data["spo2_list"], data["hr_list"])

    return {
        "timestamp_start": data["timestamp_start"],
        "timestamp_end": data["timestamp_end"],
        "total_samples": data["total_samples"],
        "spo2_avg": round(float(np.mean(spo2)), 1),
        "spo2_min": int(np.min(spo2)),
        "spo2_max": int(np.max(spo2)),
        "spo2_p5": int(np.percentile(spo2, 5)),
        "spo2_std": round(float(np.std(spo2)), 2),
        "spo2_below_90": int(np.sum(spo2 < 90)),
        "spo2_below_92": int(np.sum(spo2 < 92)),
        "spo2_clinical_events": clinical,
        "spo2_artifact_events": artifacts,
        "hr_avg": round(float(np.mean(hr)), 1),
        "hr_min": int(np.min(hr)),
        "hr_max": int(np.max(hr)),
        "hr_std": round(float(np.std(hr)), 2),
        "hr_bradycardia": int(np.sum(hr < 60)),
        "hr_tachycardia": int(np.sum(hr > 100)),
        "last_50_readings": data.get("last_50_readings", [])
    }

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
# LLM PROMPT FOR MEDICAL REPORT
# ============================================================

def generate_llm_prompt(summary, patient):
    """Genera prompt profesional para el informe médico"""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    
    spo2_interpretation = "dentro de rangos normales"
    if summary['spo2_avg'] < 92:
        spo2_interpretation = "por debajo de valores óptimos, requiere evaluación"
    elif summary['spo2_avg'] < 95:
        spo2_interpretation = "en rango aceptable, monitorizar"
    
    hr_interpretation = "dentro de rangos normales"
    if summary['hr_avg'] < 60:
        hr_interpretation = "tendencia bradicárdica"
    elif summary['hr_avg'] > 100:
        hr_interpretation = "tendencia taquicárdica"

    # Formatear últimos 50 valores para el prompt
    last_50 = summary.get('last_50_readings', [])
    last_50_table = ""
    if last_50:
        last_50_table = "\nÚLTIMOS 50 REGISTROS:\n"
        last_50_table += "| # | Timestamp | SpO2 (%) | HR (bpm) |\n"
        for i, r in enumerate(last_50, 1):
            spo2_val = r['spo2']
            hr_val = r['hr']
            # Marcar valores críticos
            spo2_mark = "⚠️" if spo2_val < 92 else ""
            hr_mark = "⚠️" if hr_val < 60 or hr_val > 150 else ""
            last_50_table += f"| {i} | {r['timestamp']} | {spo2_val}{spo2_mark} | {hr_val}{hr_mark} |\n"

    return f"""Genera un informe médico profesional en HTML completo y válido.

DATOS DEL PACIENTE:
• Nombre: {patient.get('name', 'No especificado')}
• Edad: {patient.get('age', 'No especificado')} años
• Residencia: {patient.get('residence', 'No especificado')}
• Habitación: {patient.get('room', 'No especificado')}

PERÍODO DE MONITORIZACIÓN:
• Inicio: {summary['timestamp_start']}
• Fin: {summary['timestamp_end']}
• Total de muestras: {summary['total_samples']:,}

SATURACIÓN DE OXÍGENO (SpO2):
• Media: {summary['spo2_avg']}% (Interpretación: {spo2_interpretation})
• Mínima: {summary['spo2_min']}% | Máxima: {summary['spo2_max']}%
• Percentil 5: {summary['spo2_p5']}%
• Desviación estándar: {summary['spo2_std']}%
• Muestras < 90%: {summary['spo2_below_90']} ({round(100*summary['spo2_below_90']/max(summary['total_samples'],1), 2)}%)
• Muestras < 92%: {summary['spo2_below_92']} ({round(100*summary['spo2_below_92']/max(summary['total_samples'],1), 2)}%)

FRECUENCIA CARDÍACA (FC):
• Media: {summary['hr_avg']} bpm (Interpretación: {hr_interpretation})
• Mínima: {summary['hr_min']} bpm | Máxima: {summary['hr_max']} bpm
• Desviación estándar: {summary['hr_std']} bpm
• Episodios bradicardia (<60 bpm): {summary['hr_bradycardia']}
• Episodios taquicardia (>100 bpm): {summary['hr_tachycardia']}

ANÁLISIS CLÍNICO DE EVENTOS:
• Eventos clínicos de hipoxemia sostenida: {summary['spo2_clinical_events']}
• Artefactos de señal (descensos transitorios): {summary['spo2_artifact_events']}

NOTA: Los {summary['spo2_artifact_events']} artefactos son descensos breves por movimiento del sensor. NO representan hipoxemia clínica.
{last_50_table}
ESTRUCTURA DEL INFORME HTML:
1. Encabezado con título "Informe de Monitorización Vital Continua" y datos del paciente
2. Sección "Resumen Ejecutivo" - máximo 3-4 líneas, profesional y objetivo
3. Tabla con parámetros vitales principales (estadísticas SpO2 y FC)
4. Sección "Análisis de Eventos" explicando eventos clínicos vs artefactos
5. Sección "Interpretación Clínica" con valoración médica
6. **TABLA DE ÚLTIMOS 50 REGISTROS** - tabla con columnas: #, Timestamp, SpO2 (%), HR (bpm)
   - Resaltar en ROJO los valores críticos (SpO2 < 92% o HR < 60 o HR > 150)
   - Resaltar en VERDE los valores normales
   - Usar fuente monoespaciada para los números
7. Sección "Conclusiones y Recomendaciones"
8. Pie de página:
   - Aviso: "Este informe es orientativo y no sustituye el juicio clínico profesional"
   - Fecha: {now_utc}
   - Sistema: {SYSTEM_NAME} v{ALGORITHM_VERSION}

ESTILOS CSS:
- Fuente: Arial, sans-serif
- Colores: azul (#1a5276) encabezados, gris (#5d6d7e) texto
- Tablas con bordes suaves y zebra striping (filas alternas)
- Valores críticos en rojo (#c0392b), normales en verde (#27ae60)
- Tabla de registros: fuente pequeña (11px), compacta
- Diseño limpio para impresión A4

Devuelve SOLO HTML válido completo. Sin explicaciones."""

# ============================================================
# EMAIL FUNCTIONS
# ============================================================

def check_email_config():
    issues = []
    if not RESEND_API_KEY: issues.append("RESEND_API_KEY no configurado")
    if not email_config.get("email_to"): issues.append("Email destinatario no configurado")
    return {"configured": len(issues)==0, "issues": issues, "provider": "Resend API"}

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

def send_email_resend(recipient, subject, html):
    if not RESEND_API_KEY: return {"success": False, "error": "API key no configurado"}
    if not recipient: return {"success": False, "error": "Sin destinatario"}
    print(f"📧 Enviando a {recipient}...")
    try:
        r = requests.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": EMAIL_FROM, "to": [recipient], "subject": subject, "html": html}, timeout=30)
        if r.status_code == 200:
            print(f"✅ Email enviado! ID: {r.json().get('id')}")
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
    result = send_email_resend(recipient, subject, generate_email_html(alert_type, spo2, hr, patient_info))
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
def index(): return render_template("index.html")

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
                {"role": "system", "content": "Eres un médico especialista que genera informes clínicos profesionales en HTML. Devuelve siempre HTML válido y completo, sin explicaciones."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=4500
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
    if not RESEND_API_KEY: return jsonify({"error": "RESEND_API_KEY no configurado"}), 500
    patient = d.get("patient_name") or email_config.get("patient_name","Prueba")
    result = send_email_resend(recipient, f"🧪 TEST HumanS - {patient}", generate_email_html('test', 97, 72, {"name": patient}))
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
# MAIN
# ============================================================

if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  {SYSTEM_NAME}
║  Versión: {ALGORITHM_VERSION}
╠══════════════════════════════════════════════════════════════╣
║  Informe PDF mejorado con análisis clínico:
║  - Clasificación eventos clínicos vs artefactos
║  - Estadísticas detalladas (percentiles, desviación)
║  - Interpretación automática de resultados
╚══════════════════════════════════════════════════════════════╝
    """)
    if init_db_pool(): init_database()
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))