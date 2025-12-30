#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua (Versión PostgreSQL + Email Configurable)
Recibe datos del proxy HTTP (ESP32 Bridge), los almacena en PostgreSQL
y envía alertas por email cuando los valores están fuera de los rangos críticos.

NUEVO: Email de notificación configurable desde el frontend.

Basado en el Protocolo Berry v1.5:
- Byte4: SpO2Sat (Saturación de oxígeno promedio) - Rango válido: 35-100%
- Byte6: PulseRate (Frecuencia cardíaca promedio) - Rango válido: 25-250 bpm
"""

import os
import time
import threading
import smtplib
from datetime import datetime, timezone, timedelta
from collections import deque
from queue import Queue, Empty
from io import BytesIO, StringIO
import csv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np

# PostgreSQL - importar con manejo de error
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    print("⚠️  psycopg2 no instalado. Ejecuta: pip install psycopg2-binary")
    POSTGRES_AVAILABLE = False
    psycopg2 = None
    pool = None
    RealDictCursor = None

from flask import Flask, render_template, jsonify, request, send_file, Response
from flask_socketio import SocketIO
from weasyprint import HTML
from dotenv import load_dotenv
from openai import OpenAI

# --------------------------------------------------
# ENV / OPENAI
# --------------------------------------------------
load_dotenv(".env")
client = OpenAI()
LLM_MODEL = "gpt-4o-mini"

SYSTEM_NAME = "HumanS – Monitorización Vital Continua"
ALGORITHM_VERSION = "1.3.0-configurable-email"

# --------------------------------------------------
# CONFIGURACIÓN DE EMAIL (valores por defecto)
# --------------------------------------------------
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))

# Email destinatario CONFIGURABLE (puede cambiar en runtime)
email_config = {
    "email_to": os.environ.get("EMAIL_TO", ""),
    "patient_name": "",
    "patient_room": "",
    "patient_residence": ""
}

# --------------------------------------------------
# UMBRALES CLÍNICOS (según protocolo Berry v1.5)
# --------------------------------------------------
CRITICAL_SPO2 = 92
CRITICAL_HR_LOW = 60
CRITICAL_HR_HIGH = 150
EMAIL_COOLDOWN = 300  # 5 minutos entre alertas

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
MAX_HISTORY = 120
spo2_hist = deque(maxlen=MAX_HISTORY)
hr_hist = deque(maxlen=MAX_HISTORY)
data_queue = Queue()
stop_event = threading.Event()

# Diagnóstico BLE
packet_count = 0
current_distance = 0.0
current_rssi = None
last_packet_time = None

# Control de alertas
last_spo2_alert_time = 0
last_hr_alert_time = 0
last_spo2_critical = False
last_hr_critical = False

# --------------------------------------------------
# POSTGRESQL CONNECTION
# --------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
db_pool = None

def init_db_pool():
    """Inicializa el pool de conexiones a PostgreSQL"""
    global db_pool
    
    if not POSTGRES_AVAILABLE:
        print("⚠️  PostgreSQL no disponible - usando almacenamiento en memoria")
        return False
    
    if not DATABASE_URL:
        print("⚠️  DATABASE_URL no configurada - usando almacenamiento en memoria")
        return False
    
    try:
        connection_string = DATABASE_URL
        if connection_string.startswith("postgres://"):
            connection_string = connection_string.replace("postgres://", "postgresql://", 1)
        
        db_pool = pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=connection_string
        )
        print("✅ Pool de conexiones PostgreSQL inicializado")
        return True
        
    except Exception as e:
        print(f"❌ Error inicializando pool PostgreSQL: {e}")
        return False

def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    return None

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def init_database():
    """Crea las tablas necesarias en PostgreSQL"""
    if not DATABASE_URL:
        return False
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            # Tabla de signos vitales
            cur.execute("""
                CREATE TABLE IF NOT EXISTS vital_signs (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    spo2 INTEGER NOT NULL,
                    hr INTEGER NOT NULL,
                    spo2_critical BOOLEAN DEFAULT FALSE,
                    hr_critical BOOLEAN DEFAULT FALSE,
                    distance FLOAT,
                    rssi INTEGER,
                    patient_id VARCHAR(100),
                    session_id VARCHAR(100)
                );
            """)
            
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_vital_signs_timestamp 
                ON vital_signs(timestamp DESC);
            """)
            
            # Tabla de alertas
            cur.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ DEFAULT NOW(),
                    alert_type VARCHAR(50) NOT NULL,
                    spo2 INTEGER,
                    hr INTEGER,
                    message TEXT,
                    email_sent BOOLEAN DEFAULT FALSE,
                    email_to VARCHAR(255),
                    patient_id VARCHAR(100)
                );
            """)
            
            # Tabla de configuración de email
            cur.execute("""
                CREATE TABLE IF NOT EXISTS email_config (
                    id SERIAL PRIMARY KEY,
                    email_to VARCHAR(255) NOT NULL,
                    patient_name VARCHAR(255),
                    patient_room VARCHAR(100),
                    patient_residence VARCHAR(255),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            
            conn.commit()
            print("✅ Tablas PostgreSQL creadas/verificadas")
            
            # Cargar configuración de email guardada
            load_email_config_from_db()
            
            return True
            
    except Exception as e:
        print(f"❌ Error creando tablas: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def load_email_config_from_db():
    """Carga la última configuración de email desde la BD"""
    global email_config
    
    if not db_pool:
        return
    
    conn = get_db_connection()
    if not conn:
        return
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT email_to, patient_name, patient_room, patient_residence
                FROM email_config
                ORDER BY updated_at DESC
                LIMIT 1;
            """)
            result = cur.fetchone()
            
            if result:
                email_config["email_to"] = result["email_to"] or ""
                email_config["patient_name"] = result["patient_name"] or ""
                email_config["patient_room"] = result["patient_room"] or ""
                email_config["patient_residence"] = result["patient_residence"] or ""
                print(f"✅ Configuración de email cargada: {email_config['email_to']}")
                
    except Exception as e:
        print(f"⚠️  No se pudo cargar config de email: {e}")
    finally:
        release_db_connection(conn)

def save_email_config_to_db(email_to, patient_name="", patient_room="", patient_residence=""):
    """Guarda la configuración de email en la BD"""
    if not db_pool:
        return True  # Sin BD, solo guardar en memoria
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO email_config (email_to, patient_name, patient_room, patient_residence)
                VALUES (%s, %s, %s, %s);
            """, (email_to, patient_name, patient_room, patient_residence))
            conn.commit()
            return True
    except Exception as e:
        print(f"❌ Error guardando config email: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

# --------------------------------------------------
# DATA STORAGE FUNCTIONS
# --------------------------------------------------
def save_vital_sign(spo2, hr, spo2_critical=False, hr_critical=False, 
                    distance=None, rssi=None, patient_id=None, session_id=None):
    if not db_pool:
        return True
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO vital_signs 
                (spo2, hr, spo2_critical, hr_critical, distance, rssi, patient_id, session_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id;
            """, (spo2, hr, spo2_critical, hr_critical, distance, rssi, patient_id, session_id))
            conn.commit()
            return True
    except Exception as e:
        print(f"❌ Error guardando en PostgreSQL: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def save_alert(alert_type, spo2, hr, message, email_sent=False, email_to=None, patient_id=None):
    if not db_pool:
        return True
    
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO alerts 
                (alert_type, spo2, hr, message, email_sent, email_to, patient_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s);
            """, (alert_type, spo2, hr, message, email_sent, email_to, patient_id))
            conn.commit()
            return True
    except Exception as e:
        print(f"❌ Error guardando alerta: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def get_vital_signs(limit=1000, hours=24, patient_id=None):
    if not db_pool:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT id, timestamp, spo2, hr, spo2_critical, hr_critical, 
                       distance, rssi, patient_id, session_id
                FROM vital_signs
                WHERE timestamp > NOW() - INTERVAL '%s hours'
            """
            params = [hours]
            
            if patient_id:
                query += " AND patient_id = %s"
                params.append(patient_id)
            
            query += " ORDER BY timestamp DESC LIMIT %s"
            params.append(limit)
            
            cur.execute(query, params)
            return cur.fetchall()
    except Exception as e:
        print(f"❌ Error consultando PostgreSQL: {e}")
        return []
    finally:
        release_db_connection(conn)

def get_statistics(hours=24, patient_id=None):
    if not db_pool:
        if len(spo2_hist) == 0:
            return None
        return {
            "total_samples": len(spo2_hist),
            "spo2_avg": round(sum(spo2_hist) / len(spo2_hist), 2),
            "spo2_min": min(spo2_hist),
            "spo2_max": max(spo2_hist),
            "hr_avg": round(sum(hr_hist) / len(hr_hist), 2) if hr_hist else 0,
            "hr_min": min(hr_hist) if hr_hist else 0,
            "hr_max": max(hr_hist) if hr_hist else 0,
            "spo2_critical_count": sum(1 for s in spo2_hist if s < CRITICAL_SPO2),
            "hr_critical_count": sum(1 for h in hr_hist if h < CRITICAL_HR_LOW or h > CRITICAL_HR_HIGH)
        }
    
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT 
                    COUNT(*) as total_samples,
                    ROUND(AVG(spo2)::numeric, 2) as spo2_avg,
                    MIN(spo2) as spo2_min,
                    MAX(spo2) as spo2_max,
                    ROUND(AVG(hr)::numeric, 2) as hr_avg,
                    MIN(hr) as hr_min,
                    MAX(hr) as hr_max,
                    SUM(CASE WHEN spo2_critical THEN 1 ELSE 0 END) as spo2_critical_count,
                    SUM(CASE WHEN hr_critical THEN 1 ELSE 0 END) as hr_critical_count,
                    MIN(timestamp) as first_reading,
                    MAX(timestamp) as last_reading
                FROM vital_signs
                WHERE timestamp > NOW() - INTERVAL '%s hours'
            """
            params = [hours]
            
            if patient_id:
                query += " AND patient_id = %s"
                params.append(patient_id)
            
            cur.execute(query, params)
            result = cur.fetchone()
            
            if result and result['total_samples'] > 0:
                return dict(result)
            return None
    except Exception as e:
        print(f"❌ Error obteniendo estadísticas: {e}")
        return None
    finally:
        release_db_connection(conn)

def get_alerts_history(limit=100, hours=24):
    if not db_pool:
        return []
    
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, timestamp, alert_type, spo2, hr, email_sent, email_to, patient_id
                FROM alerts
                WHERE timestamp > NOW() - INTERVAL '%s hours'
                ORDER BY timestamp DESC
                LIMIT %s;
            """, (hours, limit))
            return cur.fetchall()
    except Exception as e:
        print(f"❌ Error consultando alertas: {e}")
        return []
    finally:
        release_db_connection(conn)

# --------------------------------------------------
# EMAIL FUNCTIONS
# --------------------------------------------------
def generate_email_html(alert_type: str, spo2: int, hr: int, stats: dict, patient_info: dict) -> str:
    """Genera el HTML del email con tabla de valores promedios"""
    
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%d/%m/%Y %H:%M:%S UTC")
    
    # Determinar tipo de alerta
    if alert_type == 'spo2':
        alert_title = "⚠️ ALERTA: Saturación de Oxígeno Baja"
        alert_color = "#dc3545"
        alert_description = f"Se ha detectado un valor de SpO₂ por debajo del umbral crítico ({CRITICAL_SPO2}%)."
        critical_value = f"SpO₂: {spo2}%"
    else:
        if hr < CRITICAL_HR_LOW:
            alert_title = "⚠️ ALERTA: Bradicardia Detectada"
            alert_description = f"Se ha detectado frecuencia cardíaca por debajo de {CRITICAL_HR_LOW} bpm."
        else:
            alert_title = "⚠️ ALERTA: Taquicardia Detectada"
            alert_description = f"Se ha detectado frecuencia cardíaca por encima de {CRITICAL_HR_HIGH} bpm."
        alert_color = "#dc3545"
        critical_value = f"HR: {hr} bpm"
    
    # Valores de estadísticas
    spo2_avg = stats.get('spo2_avg', spo2) if stats else spo2
    spo2_min = stats.get('spo2_min', spo2) if stats else spo2
    spo2_max = stats.get('spo2_max', spo2) if stats else spo2
    hr_avg = stats.get('hr_avg', hr) if stats else hr
    hr_min = stats.get('hr_min', hr) if stats else hr
    hr_max = stats.get('hr_max', hr) if stats else hr
    total_samples = stats.get('total_samples', 0) if stats else 0
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{
                font-family: 'Segoe UI', Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .header {{
                background: linear-gradient(135deg, {alert_color} 0%, #c82333 100%);
                color: white;
                padding: 20px;
                border-radius: 10px 10px 0 0;
                text-align: center;
            }}
            .header h1 {{
                margin: 0;
                font-size: 24px;
            }}
            .content {{
                background: #f8f9fa;
                padding: 20px;
                border: 1px solid #dee2e6;
            }}
            .alert-box {{
                background: #fff3cd;
                border: 2px solid #ffc107;
                border-radius: 8px;
                padding: 15px;
                margin: 15px 0;
                text-align: center;
            }}
            .critical-value {{
                font-size: 48px;
                font-weight: bold;
                color: {alert_color};
                margin: 10px 0;
            }}
            .patient-info {{
                background: white;
                border-radius: 8px;
                padding: 15px;
                margin: 15px 0;
                border-left: 4px solid #007bff;
            }}
            .patient-info h3 {{
                margin: 0 0 10px 0;
                color: #007bff;
            }}
            .stats-table {{
                width: 100%;
                border-collapse: collapse;
                margin: 15px 0;
                background: white;
                border-radius: 8px;
                overflow: hidden;
            }}
            .stats-table th {{
                background: #343a40;
                color: white;
                padding: 12px;
                text-align: left;
            }}
            .stats-table td {{
                padding: 10px 12px;
                border-bottom: 1px solid #dee2e6;
            }}
            .stats-table tr:nth-child(even) {{
                background: #f8f9fa;
            }}
            .stats-table .value {{
                font-weight: bold;
                font-family: 'Courier New', monospace;
                font-size: 16px;
            }}
            .stats-table .critical {{
                color: {alert_color};
                font-weight: bold;
            }}
            .stats-table .normal {{
                color: #28a745;
            }}
            .footer {{
                background: #343a40;
                color: #adb5bd;
                padding: 15px;
                border-radius: 0 0 10px 10px;
                font-size: 12px;
                text-align: center;
            }}
            .timestamp {{
                color: #6c757d;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>{alert_title}</h1>
            <p class="timestamp">📅 {timestamp_str}</p>
        </div>
        
        <div class="content">
            <div class="alert-box">
                <p><strong>{alert_description}</strong></p>
                <div class="critical-value">{critical_value}</div>
            </div>
            
            <div class="patient-info">
                <h3>👤 Datos del Paciente</h3>
                <p><strong>Nombre:</strong> {patient_info.get('name', 'No especificado')}</p>
                <p><strong>Residencia:</strong> {patient_info.get('residence', 'No especificado')}</p>
                <p><strong>Habitación:</strong> {patient_info.get('room', 'No especificado')}</p>
            </div>
            
            <h3>📊 Valores Actuales y Estadísticas de la Sesión</h3>
            <table class="stats-table">
                <thead>
                    <tr>
                        <th>Parámetro</th>
                        <th>Valor Actual</th>
                        <th>Promedio</th>
                        <th>Mínimo</th>
                        <th>Máximo</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><strong>🫁 SpO₂</strong></td>
                        <td class="value {'critical' if spo2 < CRITICAL_SPO2 else 'normal'}">{spo2}%</td>
                        <td class="value">{spo2_avg}%</td>
                        <td class="value">{spo2_min}%</td>
                        <td class="value">{spo2_max}%</td>
                    </tr>
                    <tr>
                        <td><strong>❤️ Frecuencia Cardíaca</strong></td>
                        <td class="value {'critical' if hr < CRITICAL_HR_LOW or hr > CRITICAL_HR_HIGH else 'normal'}">{hr} bpm</td>
                        <td class="value">{hr_avg} bpm</td>
                        <td class="value">{hr_min} bpm</td>
                        <td class="value">{hr_max} bpm</td>
                    </tr>
                </tbody>
            </table>
            
            <table class="stats-table">
                <thead>
                    <tr>
                        <th colspan="2">📈 Resumen de la Sesión</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Total de muestras</td>
                        <td class="value">{total_samples}</td>
                    </tr>
                    <tr>
                        <td>Eventos críticos SpO₂ (< {CRITICAL_SPO2}%)</td>
                        <td class="value">{stats.get('spo2_critical_count', 0) if stats else 0}</td>
                    </tr>
                    <tr>
                        <td>Eventos críticos HR</td>
                        <td class="value">{stats.get('hr_critical_count', 0) if stats else 0}</td>
                    </tr>
                </tbody>
            </table>
            
            <div style="background: #e7f3ff; padding: 12px; border-radius: 6px; margin-top: 15px;">
                <strong>⚕️ Acción recomendada:</strong><br>
                Verificar el estado del paciente y considerar intervención según protocolo establecido.
            </div>
        </div>
        
        <div class="footer">
            <p><strong>{SYSTEM_NAME}</strong></p>
            <p>Versión: {ALGORITHM_VERSION} | Base de datos: PostgreSQL</p>
            <p>Este es un mensaje automático del sistema de monitorización.</p>
        </div>
    </body>
    </html>
    """
    
    return html

def generate_email_text(alert_type: str, spo2: int, hr: int, stats: dict, patient_info: dict) -> str:
    """Genera versión texto plano del email"""
    
    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%d/%m/%Y %H:%M:%S UTC")
    
    if alert_type == 'spo2':
        alert_title = "ALERTA: Saturación de Oxígeno Baja"
    else:
        alert_title = "ALERTA: Frecuencia Cardíaca Fuera de Rango"
    
    spo2_avg = stats.get('spo2_avg', spo2) if stats else spo2
    hr_avg = stats.get('hr_avg', hr) if stats else hr
    
    text = f"""
{alert_title}
{'='*50}

Fecha y hora: {timestamp_str}

VALORES CRÍTICOS DETECTADOS:
- SpO₂: {spo2}%
- HR: {hr} bpm

DATOS DEL PACIENTE:
- Nombre: {patient_info.get('name', 'No especificado')}
- Residencia: {patient_info.get('residence', 'No especificado')}
- Habitación: {patient_info.get('room', 'No especificado')}

ESTADÍSTICAS DE LA SESIÓN:
- SpO₂ promedio: {spo2_avg}%
- HR promedio: {hr_avg} bpm
- Total muestras: {stats.get('total_samples', 0) if stats else 0}

Acción recomendada: Verificar estado del paciente.

--
{SYSTEM_NAME}
Versión: {ALGORITHM_VERSION}
"""
    return text

def send_alert_email(recipient: str, subject: str, html_content: str, text_content: str):
    """Envía email de alerta"""
    if not EMAIL_FROM or not EMAIL_PASS:
        print("⚠️  Credenciales de email no configuradas")
        return False
    
    if not recipient:
        print("⚠️  No hay destinatario configurado")
        return False
    
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = recipient
        
        msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        server.quit()
        
        print(f"✅ Email enviado a {recipient}")
        return True
        
    except Exception as e:
        print(f"❌ Error enviando email: {e}")
        return False

def send_alert_email_thread(alert_type: str, spo2: int, hr: int):
    """Envía email en thread separado"""
    def _send():
        recipient = email_config.get("email_to", "")
        if not recipient:
            print("⚠️  No hay email destinatario configurado")
            return
        
        # Obtener estadísticas
        stats = get_statistics(hours=24)
        
        # Info del paciente
        patient_info = {
            "name": email_config.get("patient_name", "No especificado"),
            "room": email_config.get("patient_room", "No especificado"),
            "residence": email_config.get("patient_residence", "No especificado")
        }
        
        # Generar contenido
        if alert_type == 'spo2':
            subject = f"⚠️ ALERTA CRÍTICA – SpO₂ Bajo ({spo2}%) - {patient_info['name']}"
        else:
            condition = "Bradicardia" if hr < CRITICAL_HR_LOW else "Taquicardia"
            subject = f"⚠️ ALERTA CRÍTICA – {condition} ({hr} bpm) - {patient_info['name']}"
        
        html_content = generate_email_html(alert_type, spo2, hr, stats, patient_info)
        text_content = generate_email_text(alert_type, spo2, hr, stats, patient_info)
        
        success = send_alert_email(recipient, subject, html_content, text_content)
        
        # Guardar registro de alerta
        save_alert(alert_type, spo2, hr, subject, email_sent=success, 
                   email_to=recipient, patient_id=patient_info['name'])
        
        # Notificar al frontend
        if success:
            socketio.emit('alert_sent', {
                'type': alert_type,
                'message': f'Email enviado a {recipient}'
            })
    
    threading.Thread(target=_send, daemon=True).start()

def check_and_send_alerts(spo2: int, hr: int):
    """Verifica valores y envía alertas si es necesario"""
    global last_spo2_alert_time, last_hr_alert_time
    global last_spo2_critical, last_hr_critical
    
    current_time = time.time()
    
    spo2_critical = spo2 < CRITICAL_SPO2
    if spo2_critical and not last_spo2_critical:
        if current_time - last_spo2_alert_time >= EMAIL_COOLDOWN:
            print(f"[ALERTA] SpO2 crítico: {spo2}%")
            send_alert_email_thread('spo2', spo2, hr)
            last_spo2_alert_time = current_time
    
    hr_critical = (hr < CRITICAL_HR_LOW) or (hr > CRITICAL_HR_HIGH)
    if hr_critical and not last_hr_critical:
        if current_time - last_hr_alert_time >= EMAIL_COOLDOWN:
            print(f"[ALERTA] HR crítico: {hr} bpm")
            send_alert_email_thread('hr', spo2, hr)
            last_hr_alert_time = current_time
    
    last_spo2_critical = spo2_critical
    last_hr_critical = hr_critical
    
    return spo2_critical, hr_critical

# --------------------------------------------------
# FLASK APP
# --------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "vinculocare-dev")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

@app.route("/")
def index():
    return render_template("index11.html")

@app.route("/api/data", methods=["POST"])
def receive_data():
    """Endpoint principal para recibir datos"""
    global packet_count, current_distance, current_rssi, last_packet_time
    
    api_key = request.headers.get('x-api-key')
    expected_key = os.environ.get('API_KEY', 'f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6')
    
    if api_key != expected_key:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Petición vacía"}), 400

    spo2 = data.get('spo2')
    hr = data.get('hr')

    if spo2 is None or hr is None:
        return jsonify({"error": "Faltan 'spo2' o 'hr'"}), 400

    if not (35 <= spo2 <= 100) or not (25 <= hr <= 250):
        return jsonify({"error": "Valores fuera de rango"}), 400

    spo2_hist.append(spo2)
    hr_hist.append(hr)

    if 'distance' in data:
        current_distance = data['distance']
    if 'rssi' in data:
        current_rssi = data['rssi']
    
    packet_count += 1
    last_packet_time = datetime.now()

    spo2_critical, hr_critical = check_and_send_alerts(spo2, hr)

    save_vital_sign(
        spo2=spo2, hr=hr,
        spo2_critical=spo2_critical, hr_critical=hr_critical,
        distance=current_distance, rssi=current_rssi,
        patient_id=data.get('patient_id'), session_id=data.get('session_id')
    )

    payload = {
        "spo2": spo2, "hr": hr,
        "spo2_history": list(spo2_hist),
        "hr_history": list(hr_hist),
        "spo2_critical": spo2_critical,
        "hr_critical": hr_critical
    }

    diagnostics_payload = {
        "count": packet_count,
        "distance": current_distance,
        "rssi": current_rssi
    }

    socketio.emit("update", payload)
    socketio.emit("raw_update", diagnostics_payload)

    return jsonify({"status": "ok", "alerts_sent": spo2_critical or hr_critical}), 200

# --------------------------------------------------
# EMAIL CONFIGURATION ENDPOINTS
# --------------------------------------------------
@app.route("/api/email/config", methods=["GET"])
def get_email_config():
    """Obtiene la configuración actual de email"""
    return jsonify({
        "email_to": email_config.get("email_to", ""),
        "patient_name": email_config.get("patient_name", ""),
        "patient_room": email_config.get("patient_room", ""),
        "patient_residence": email_config.get("patient_residence", ""),
        "email_from_configured": bool(EMAIL_FROM and EMAIL_PASS),
        "thresholds": {
            "spo2_critical": CRITICAL_SPO2,
            "hr_low": CRITICAL_HR_LOW,
            "hr_high": CRITICAL_HR_HIGH
        }
    })

@app.route("/api/email/config", methods=["POST"])
def set_email_config():
    """Configura el email destinatario"""
    data = request.get_json()
    
    if not data:
        return jsonify({"error": "Datos vacíos"}), 400
    
    new_email = data.get("email_to", "").strip()
    
    if not new_email or "@" not in new_email:
        return jsonify({"error": "Email inválido"}), 400
    
    # Actualizar configuración en memoria
    email_config["email_to"] = new_email
    email_config["patient_name"] = data.get("patient_name", "")
    email_config["patient_room"] = data.get("patient_room", "")
    email_config["patient_residence"] = data.get("patient_residence", "")
    
    # Guardar en BD
    save_email_config_to_db(
        new_email,
        email_config["patient_name"],
        email_config["patient_room"],
        email_config["patient_residence"]
    )
    
    print(f"✅ Email configurado: {new_email}")
    
    return jsonify({
        "status": "ok",
        "message": f"Email configurado: {new_email}",
        "email_to": new_email
    })

@app.route("/api/email/test", methods=["POST"])
def test_email_endpoint():
    """Envía email de prueba"""
    data = request.get_json() or {}
    
    recipient = data.get("email_to") or email_config.get("email_to", "")
    
    if not recipient:
        return jsonify({"error": "No hay email configurado"}), 400
    
    patient_name = data.get("patient_name") or email_config.get("patient_name", "Paciente de prueba")
    
    # Generar email de prueba
    stats = get_statistics(hours=24) or {
        "spo2_avg": 97, "spo2_min": 95, "spo2_max": 99,
        "hr_avg": 72, "hr_min": 65, "hr_max": 85,
        "total_samples": 100, "spo2_critical_count": 0, "hr_critical_count": 0
    }
    
    patient_info = {
        "name": patient_name,
        "room": email_config.get("patient_room", "N/A"),
        "residence": email_config.get("patient_residence", "N/A")
    }
    
    subject = f"🧪 TEST - Sistema de Alertas HumanS - {patient_name}"
    html_content = generate_email_html('test', 97, 72, stats, patient_info)
    text_content = f"""
TEST - Sistema de Alertas HumanS
================================

Este es un mensaje de prueba del sistema de alertas.

Paciente: {patient_name}
Email configurado: {recipient}

Si recibes este mensaje, el sistema de alertas está funcionando correctamente.

--
{SYSTEM_NAME}
Versión: {ALGORITHM_VERSION}
"""
    
    success = send_alert_email(recipient, subject, html_content, text_content)
    
    if success:
        return jsonify({"status": "ok", "message": f"Email de prueba enviado a {recipient}"})
    else:
        return jsonify({"error": "No se pudo enviar el email"}), 500

# --------------------------------------------------
# OTHER API ENDPOINTS
# --------------------------------------------------
@app.route("/api/history", methods=["GET"])
def get_history():
    hours = request.args.get('hours', 24, type=int)
    limit = request.args.get('limit', 1000, type=int)
    patient_id = request.args.get('patient_id')
    
    data = get_vital_signs(limit=limit, hours=hours, patient_id=patient_id)
    
    for row in data:
        if row.get('timestamp'):
            row['timestamp'] = row['timestamp'].isoformat()
    
    return jsonify({"status": "ok", "count": len(data), "data": data})

@app.route("/api/statistics", methods=["GET"])
def get_stats():
    hours = request.args.get('hours', 24, type=int)
    patient_id = request.args.get('patient_id')
    
    stats = get_statistics(hours=hours, patient_id=patient_id)
    
    if not stats:
        return jsonify({"status": "error", "message": "No hay datos suficientes"}), 404
    
    if stats.get('first_reading'):
        stats['first_reading'] = stats['first_reading'].isoformat()
    if stats.get('last_reading'):
        stats['last_reading'] = stats['last_reading'].isoformat()
    
    return jsonify({"status": "ok", "statistics": stats})

@app.route("/api/alerts/history", methods=["GET"])
def get_alerts():
    hours = request.args.get('hours', 24, type=int)
    limit = request.args.get('limit', 100, type=int)
    
    alerts = get_alerts_history(limit=limit, hours=hours)
    
    for alert in alerts:
        if alert.get('timestamp'):
            alert['timestamp'] = alert['timestamp'].isoformat()
    
    return jsonify({"status": "ok", "count": len(alerts), "alerts": alerts})

@app.route("/api/export/csv", methods=["GET"])
def export_csv():
    hours = request.args.get('hours', 24, type=int)
    patient_id = request.args.get('patient_id')
    
    data = get_vital_signs(limit=10000, hours=hours, patient_id=patient_id)
    
    if not data:
        return jsonify({"error": "No hay datos"}), 404
    
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['timestamp', 'spo2', 'hr', 'spo2_critical', 'hr_critical'])
    
    for row in data:
        writer.writerow([
            row.get('timestamp', '').isoformat() if row.get('timestamp') else '',
            row.get('spo2', ''), row.get('hr', ''),
            row.get('spo2_critical', ''), row.get('hr_critical', '')
        ])
    
    output.seek(0)
    filename = f"vital_signs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

@app.route("/api/diagnostics", methods=["GET"])
def get_diagnostics():
    db_status = "connected" if db_pool else "not_configured"
    
    return jsonify({
        "packet_count": packet_count,
        "distance": current_distance,
        "rssi": current_rssi,
        "last_packet": last_packet_time.isoformat() if last_packet_time else None,
        "uptime_seconds": time.time() - app.config.get('start_time', time.time()),
        "database": {"type": "PostgreSQL", "status": db_status},
        "email_config": {
            "enabled": bool(EMAIL_FROM and EMAIL_PASS),
            "recipient": email_config.get("email_to", ""),
            "cooldown_seconds": EMAIL_COOLDOWN
        },
        "thresholds": {
            "spo2_critical": CRITICAL_SPO2,
            "hr_low": CRITICAL_HR_LOW,
            "hr_high": CRITICAL_HR_HIGH
        }
    })

# --------------------------------------------------
# WEBSOCKET EVENTS
# --------------------------------------------------
@socketio.on('connect')
def handle_connect():
    print('[WebSocket] Cliente conectado')
    
    if len(spo2_hist) > 0 and len(hr_hist) > 0:
        socketio.emit('update', {
            "spo2": spo2_hist[-1], "hr": hr_hist[-1],
            "spo2_history": list(spo2_hist), "hr_history": list(hr_hist),
            "spo2_critical": spo2_hist[-1] < CRITICAL_SPO2,
            "hr_critical": hr_hist[-1] < CRITICAL_HR_LOW or hr_hist[-1] > CRITICAL_HR_HIGH
        })
    
    socketio.emit('raw_update', {
        "count": packet_count,
        "distance": current_distance,
        "rssi": current_rssi
    })

@socketio.on('disconnect')
def handle_disconnect():
    print('[WebSocket] Cliente desconectado')

# --------------------------------------------------
# PDF REPORT (simplified)
# --------------------------------------------------
def process_data_for_analysis():
    stats = get_statistics(hours=24)
    
    if stats and stats.get('total_samples', 0) > 0:
        return {
            "timestamp_start": stats.get('first_reading', 'N/A'),
            "timestamp_end": stats.get('last_reading', 'N/A'),
            "total_samples": stats['total_samples'],
            "spo2_avg": float(stats['spo2_avg']) if stats['spo2_avg'] else 0,
            "spo2_min": stats['spo2_min'],
            "spo2_max": stats['spo2_max'],
            "spo2_clinical_events": stats.get('spo2_critical_count', 0),
            "hr_avg": float(stats['hr_avg']) if stats['hr_avg'] else 0,
            "hr_min": stats['hr_min'],
            "hr_max": stats['hr_max']
        }
    
    if len(spo2_hist) == 0:
        return None
    
    return {
        "timestamp_start": "Sesión actual",
        "timestamp_end": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(spo2_hist),
        "spo2_avg": round(sum(spo2_hist) / len(spo2_hist), 2),
        "spo2_min": min(spo2_hist),
        "spo2_max": max(spo2_hist),
        "spo2_clinical_events": sum(1 for s in spo2_hist if s < CRITICAL_SPO2),
        "hr_avg": round(sum(hr_hist) / len(hr_hist), 2) if hr_hist else 0,
        "hr_min": min(hr_hist) if hr_hist else 0,
        "hr_max": max(hr_hist) if hr_hist else 0
    }

@app.route("/api/report/pdf", methods=["POST"])
def api_report_pdf():
    try:
        patient = request.get_json(silent=True) or {}
        summary = process_data_for_analysis()
        
        if not summary:
            return jsonify({"error": "No hay datos suficientes"}), 400

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        prompt = f"""Genera un informe médico profesional en HTML.

DATOS DEL PACIENTE:
• Nombre: {patient.get('name', 'No especificado')}
• Edad: {patient.get('age', 'No especificado')} años
• Residencia: {patient.get('residence', 'No especificado')}
• Habitación: {patient.get('room', 'No especificado')}

PERÍODO: {summary['timestamp_start']} - {summary['timestamp_end']}
MUESTRAS: {summary['total_samples']}

PARÁMETROS:
• SpO₂: Media {summary['spo2_avg']}% | Mín {summary['spo2_min']}% | Máx {summary['spo2_max']}%
• FC: Media {summary['hr_avg']} bpm | Mín {summary['hr_min']} bpm | Máx {summary['hr_max']} bpm

EVENTOS CRÍTICOS: {summary['spo2_clinical_events']}

Genera HTML completo con estilos profesionales. Firma: {now_utc} | {SYSTEM_NAME} | {ALGORITHM_VERSION}
"""

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": "Genera informes médicos HTML profesionales."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=2000
        )

        html_content = response.choices[0].message.content.strip()
        
        if html_content.startswith("```"):
            html_content = html_content.split("```")[1]
            if html_content.startswith("html"):
                html_content = html_content[4:]
        html_content = html_content.strip()

        pdf = BytesIO()
        HTML(string=html_content).write_pdf(pdf)
        pdf.seek(0)

        filename = f"informe_{patient.get('name', 'paciente')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)

    except Exception as e:
        print(f"[ERROR] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------
# QUEUE PROCESSOR
# --------------------------------------------------
def process_queue():
    while not stop_event.is_set():
        try:
            data = data_queue.get(timeout=0.5)
            socketio.emit("update", data)
        except Empty:
            continue

# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  {SYSTEM_NAME}
║  Versión: {ALGORITHM_VERSION}
╠══════════════════════════════════════════════════════════════╣
║  BASE DE DATOS: {'✅ Configurada' if DATABASE_URL else '❌ No configurada'}
║  EMAIL REMITENTE: {'✅ Configurado' if EMAIL_FROM and EMAIL_PASS else '❌ No configurado'}
║  EMAIL DESTINATARIO: {email_config.get('email_to') or '⚠️  Pendiente de configurar en UI'}
╠══════════════════════════════════════════════════════════════╣
║  UMBRALES: SpO2 < {CRITICAL_SPO2}% | HR < {CRITICAL_HR_LOW} o > {CRITICAL_HR_HIGH} bpm
╚══════════════════════════════════════════════════════════════╝
    """)
    
    if init_db_pool():
        init_database()
    
    app.config['start_time'] = time.time()
    threading.Thread(target=process_queue, daemon=True).start()
    
    port = int(os.environ.get("PORT", 5050))
    socketio.run(app, host="0.0.0.0", port=port)
