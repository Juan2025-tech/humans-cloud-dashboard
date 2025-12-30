#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua
======================================
Versión: 1.5.0-websocket-fix

CORRECCIONES:
- Emit directo de WebSocket (sin cola)
- Eventlet monkey_patch al inicio
- Timeout en SMTP
- Logging mejorado
"""

# ============================================================
# CRÍTICO: Monkey patch ANTES de cualquier otro import
# ============================================================
import eventlet
eventlet.monkey_patch()

import os
import time
import threading
import smtplib
import socket
from datetime import datetime, timezone, timedelta
from collections import deque
from io import BytesIO, StringIO
import csv
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import numpy as np

# PostgreSQL
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    print("⚠️  psycopg2 no instalado")
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
ALGORITHM_VERSION = "1.5.0-websocket-fix"

# --------------------------------------------------
# CONFIGURACIÓN DE EMAIL
# --------------------------------------------------
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_PASS = os.environ.get("EMAIL_PASS", "")
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_TIMEOUT = 15

email_config = {
    "email_to": os.environ.get("EMAIL_TO", ""),
    "patient_name": "",
    "patient_room": "",
    "patient_residence": ""
}

# --------------------------------------------------
# UMBRALES CLÍNICOS
# --------------------------------------------------
CRITICAL_SPO2 = 92
CRITICAL_HR_LOW = 60
CRITICAL_HR_HIGH = 150
EMAIL_COOLDOWN = 300

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
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

# --------------------------------------------------
# FLASK APP (inicializar antes de usarlo)
# --------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'humans-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --------------------------------------------------
# POSTGRESQL
# --------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
db_pool = None

def init_db_pool():
    global db_pool
    if not POSTGRES_AVAILABLE or not DATABASE_URL:
        print("⚠️  PostgreSQL no disponible")
        return False
    try:
        conn_str = DATABASE_URL
        if conn_str.startswith("postgres://"):
            conn_str = conn_str.replace("postgres://", "postgresql://", 1)
        db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=conn_str)
        print("✅ PostgreSQL conectado")
        return True
    except Exception as e:
        print(f"❌ Error PostgreSQL: {e}")
        return False

def get_db_connection():
    return db_pool.getconn() if db_pool else None

def release_db_connection(conn):
    if db_pool and conn:
        db_pool.putconn(conn)

def init_database():
    if not DATABASE_URL:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
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
                CREATE INDEX IF NOT EXISTS idx_vital_signs_ts ON vital_signs(timestamp DESC);
                
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
            print("✅ Tablas verificadas")
            load_email_config_from_db()
            return True
    except Exception as e:
        print(f"❌ Error tablas: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def load_email_config_from_db():
    global email_config
    if not db_pool:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM email_config ORDER BY updated_at DESC LIMIT 1;")
            result = cur.fetchone()
            if result:
                email_config["email_to"] = result.get("email_to", "")
                email_config["patient_name"] = result.get("patient_name", "")
                email_config["patient_room"] = result.get("patient_room", "")
                email_config["patient_residence"] = result.get("patient_residence", "")
                print(f"✅ Email cargado: {email_config['email_to']}")
    except Exception as e:
        print(f"⚠️  Error cargando email: {e}")
    finally:
        release_db_connection(conn)

def save_email_config_to_db(email_to, patient_name="", patient_room="", patient_residence=""):
    if not db_pool:
        return True
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO email_config (email_to, patient_name, patient_room, patient_residence) VALUES (%s, %s, %s, %s);",
                (email_to, patient_name, patient_room, patient_residence)
            )
            conn.commit()
            return True
    except Exception as e:
        print(f"❌ Error guardando email config: {e}")
        conn.rollback()
        return False
    finally:
        release_db_connection(conn)

def save_vital_sign(spo2, hr, spo2_critical=False, hr_critical=False, distance=None, rssi=None, patient_id=None, session_id=None):
    if not db_pool:
        return True
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO vital_signs (spo2, hr, spo2_critical, hr_critical, distance, rssi, patient_id, session_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s);",
                (spo2, hr, spo2_critical, hr_critical, distance, rssi, patient_id, session_id)
            )
            conn.commit()
            return True
    except:
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
            cur.execute(
                "INSERT INTO alerts (alert_type, spo2, hr, message, email_sent, email_to, patient_id) VALUES (%s,%s,%s,%s,%s,%s,%s);",
                (alert_type, spo2, hr, message, email_sent, email_to, patient_id)
            )
            conn.commit()
            return True
    except:
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
            query = "SELECT * FROM vital_signs WHERE timestamp > NOW() - INTERVAL '%s hours'"
            params = [hours]
            if patient_id:
                query += " AND patient_id = %s"
                params.append(patient_id)
            query += " ORDER BY timestamp DESC LIMIT %s"
            params.append(limit)
            cur.execute(query, params)
            return cur.fetchall()
    except:
        return []
    finally:
        release_db_connection(conn)

def get_statistics(hours=24, patient_id=None):
    if not db_pool:
        if len(spo2_hist) == 0:
            return None
        return {
            "total_samples": len(spo2_hist),
            "spo2_avg": round(sum(spo2_hist)/len(spo2_hist), 2),
            "spo2_min": min(spo2_hist),
            "spo2_max": max(spo2_hist),
            "hr_avg": round(sum(hr_hist)/len(hr_hist), 2) if hr_hist else 0,
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
                SELECT COUNT(*) as total_samples,
                    ROUND(AVG(spo2)::numeric, 2) as spo2_avg,
                    MIN(spo2) as spo2_min, MAX(spo2) as spo2_max,
                    ROUND(AVG(hr)::numeric, 2) as hr_avg,
                    MIN(hr) as hr_min, MAX(hr) as hr_max,
                    SUM(CASE WHEN spo2_critical THEN 1 ELSE 0 END) as spo2_critical_count,
                    SUM(CASE WHEN hr_critical THEN 1 ELSE 0 END) as hr_critical_count,
                    MIN(timestamp) as first_reading,
                    MAX(timestamp) as last_reading
                FROM vital_signs WHERE timestamp > NOW() - INTERVAL '%s hours'
            """
            params = [hours]
            if patient_id:
                query += " AND patient_id = %s"
                params.append(patient_id)
            cur.execute(query, params)
            result = cur.fetchone()
            return dict(result) if result and result['total_samples'] > 0 else None
    except:
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
            cur.execute(
                "SELECT * FROM alerts WHERE timestamp > NOW() - INTERVAL '%s hours' ORDER BY timestamp DESC LIMIT %s;",
                (hours, limit)
            )
            return cur.fetchall()
    except:
        return []
    finally:
        release_db_connection(conn)

# --------------------------------------------------
# EMAIL FUNCTIONS
# --------------------------------------------------
def check_email_config():
    issues = []
    if not EMAIL_FROM:
        issues.append("EMAIL_FROM no configurado")
    if not EMAIL_PASS:
        issues.append("EMAIL_PASS no configurado")
    elif len(EMAIL_PASS) < 10:
        issues.append("EMAIL_PASS muy corto")
    return {
        "configured": len(issues) == 0,
        "issues": issues,
        "email_from": EMAIL_FROM[:8] + "***" if EMAIL_FROM and len(EMAIL_FROM) > 8 else EMAIL_FROM,
        "smtp_server": SMTP_SERVER,
        "smtp_port": SMTP_PORT,
        "pass_configured": bool(EMAIL_PASS),
        "pass_length": len(EMAIL_PASS) if EMAIL_PASS else 0
    }

def generate_email_html(alert_type, spo2, hr, stats, patient_info):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")
    if alert_type == 'test':
        title, color = "🧪 TEST - Sistema HumanS", "#17a2b8"
    elif alert_type == 'spo2':
        title, color = "⚠️ ALERTA: SpO₂ Bajo", "#dc3545"
    else:
        title, color = "⚠️ ALERTA: HR Anormal", "#dc3545"
    
    return f"""<!DOCTYPE html><html><body style="font-family:Arial;max-width:600px;margin:auto;padding:20px;">
    <div style="background:{color};color:white;padding:20px;border-radius:10px 10px 0 0;text-align:center;">
        <h1 style="margin:0;">{title}</h1>
    </div>
    <div style="background:#f8f9fa;padding:20px;border-radius:0 0 10px 10px;">
        <div style="font-size:42px;font-weight:bold;color:{color};text-align:center;margin:20px 0;">
            SpO₂: {spo2}% | HR: {hr} bpm
        </div>
        <p><strong>Paciente:</strong> {patient_info.get('name','N/A')}</p>
        <p style="font-size:11px;color:#666;text-align:center;">{SYSTEM_NAME} v{ALGORITHM_VERSION} | {now}</p>
    </div></body></html>"""

def send_email(recipient, subject, html_content, text_content=""):
    result = {"success": False, "error": None}
    if not EMAIL_FROM or not EMAIL_PASS:
        result["error"] = "Credenciales no configuradas"
        return result
    if not recipient:
        result["error"] = "No hay destinatario"
        return result
    
    print(f"📧 Enviando email a {recipient}...")
    server = None
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'], msg['From'], msg['To'] = subject, EMAIL_FROM, recipient
        if text_content:
            msg.attach(MIMEText(text_content, 'plain', 'utf-8'))
        msg.attach(MIMEText(html_content, 'html', 'utf-8'))
        
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=SMTP_TIMEOUT)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=SMTP_TIMEOUT)
            server.starttls()
        
        server.login(EMAIL_FROM, EMAIL_PASS)
        server.sendmail(EMAIL_FROM, [recipient], msg.as_string())
        result["success"] = True
        print(f"✅ Email enviado a {recipient}")
    except socket.timeout:
        result["error"] = f"Timeout ({SMTP_TIMEOUT}s)"
        print(f"❌ {result['error']}")
    except smtplib.SMTPAuthenticationError as e:
        result["error"] = f"Auth error: {e.smtp_code}"
        print(f"❌ {result['error']}")
    except Exception as e:
        result["error"] = str(e)
        print(f"❌ {result['error']}")
    finally:
        if server:
            try: server.quit()
            except: pass
    return result

def send_alert_email_async(alert_type, spo2, hr):
    def _send():
        recipient = email_config.get("email_to", "")
        if not recipient:
            print("⚠️  No hay email destinatario configurado")
            return
        stats = get_statistics(hours=24)
        patient_info = {"name": email_config.get("patient_name", "N/A")}
        subject = f"⚠️ ALERTA HumanS - {alert_type.upper()} - {patient_info['name']}"
        html = generate_email_html(alert_type, spo2, hr, stats, patient_info)
        result = send_email(recipient, subject, html)
        save_alert(alert_type, spo2, hr, subject, email_sent=result["success"], email_to=recipient)
        if result["success"]:
            socketio.emit('alert_sent', {'type': alert_type, 'message': f'Email enviado a {recipient}'})
    eventlet.spawn(_send)  # Usar eventlet.spawn en lugar de threading

# --------------------------------------------------
# ROUTES
# --------------------------------------------------
@app.route("/")
def index():
    return render_template("index11.html")

@app.route("/api/data", methods=["POST"])
def receive_data():
    global packet_count, current_distance, current_rssi, last_packet_time
    global last_spo2_critical, last_hr_critical, last_spo2_alert_time, last_hr_alert_time
    
    try:
        payload = request.get_json(force=True)
        spo2 = payload.get("spo2")
        hr = payload.get("hr")
        distance = payload.get("distance", 0.0)
        rssi = payload.get("rssi")
        
        if spo2 is None or hr is None:
            return jsonify({"error": "Missing spo2 or hr"}), 400
        
        if not (35 <= spo2 <= 100) or not (25 <= hr <= 250):
            return jsonify({"error": "Out of range"}), 400
        
        packet_count += 1
        current_distance = distance
        current_rssi = rssi
        last_packet_time = datetime.now(timezone.utc)
        
        spo2_hist.append(spo2)
        hr_hist.append(hr)
        
        spo2_critical = spo2 < CRITICAL_SPO2
        hr_critical = hr < CRITICAL_HR_LOW or hr > CRITICAL_HR_HIGH
        
        # Guardar en DB (async)
        eventlet.spawn(save_vital_sign, spo2, hr, spo2_critical, hr_critical, distance, rssi,
                       email_config.get("patient_name"))
        
        # Alertas
        current_time = time.time()
        if spo2_critical and not last_spo2_critical and current_time - last_spo2_alert_time > EMAIL_COOLDOWN:
            send_alert_email_async('spo2', spo2, hr)
            last_spo2_alert_time = current_time
        if hr_critical and not last_hr_critical and current_time - last_hr_alert_time > EMAIL_COOLDOWN:
            send_alert_email_async('hr', spo2, hr)
            last_hr_alert_time = current_time
        
        last_spo2_critical = spo2_critical
        last_hr_critical = hr_critical
        
        # *** EMITIR DIRECTAMENTE AL WEBSOCKET ***
        data = {
            "spo2": spo2, "hr": hr,
            "spo2_history": list(spo2_hist), "hr_history": list(hr_hist),
            "spo2_critical": spo2_critical, "hr_critical": hr_critical
        }
        
        # Emitir a todos los clientes conectados
        socketio.emit('update', data)
        socketio.emit('raw_update', {"count": packet_count, "distance": distance, "rssi": rssi})
        
        print(f"📊 Paquete #{packet_count}: SpO2={spo2}% HR={hr}bpm → WebSocket emitido")
        
        return jsonify({"status": "ok", "packet": packet_count})
        
    except Exception as e:
        print(f"[ERROR] /api/data: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/email/config", methods=["GET"])
def get_email_config_endpoint():
    diag = check_email_config()
    return jsonify({
        "email_to": email_config.get("email_to", ""),
        "patient_name": email_config.get("patient_name", ""),
        "patient_room": email_config.get("patient_room", ""),
        "patient_residence": email_config.get("patient_residence", ""),
        "smtp_configured": diag["configured"],
        "smtp_issues": diag["issues"],
        "thresholds": {"spo2_critical": CRITICAL_SPO2, "hr_low": CRITICAL_HR_LOW, "hr_high": CRITICAL_HR_HIGH}
    })

@app.route("/api/email/config", methods=["POST"])
def set_email_config():
    global email_config
    try:
        data = request.get_json()
        new_email = data.get("email_to", "").strip()
        if not new_email or "@" not in new_email:
            return jsonify({"error": "Email inválido"}), 400
        
        email_config["email_to"] = new_email
        email_config["patient_name"] = data.get("patient_name", "")
        email_config["patient_room"] = data.get("patient_room", "")
        email_config["patient_residence"] = data.get("patient_residence", "")
        
        save_email_config_to_db(new_email, email_config["patient_name"],
                                email_config["patient_room"], email_config["patient_residence"])
        print(f"✅ Email configurado: {new_email}")
        return jsonify({"status": "ok", "email_to": new_email})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/email/diagnose", methods=["GET"])
def diagnose_email():
    config_check = check_email_config()
    return jsonify({"config": config_check, "recipient": email_config.get("email_to", "")})

@app.route("/api/email/test", methods=["POST"])
def test_email_endpoint():
    data = request.get_json() or {}
    recipient = data.get("email_to") or email_config.get("email_to", "")
    if not recipient:
        return jsonify({"error": "No hay email configurado"}), 400
    
    config_check = check_email_config()
    if not config_check["configured"]:
        return jsonify({"error": "Config incompleta", "issues": config_check["issues"]}), 500
    
    patient_name = data.get("patient_name") or email_config.get("patient_name", "Prueba")
    subject = f"🧪 TEST - HumanS - {patient_name}"
    html = generate_email_html('test', 97, 72, {}, {"name": patient_name})
    result = send_email(recipient, subject, html)
    
    if result["success"]:
        return jsonify({"status": "ok", "message": f"Email enviado a {recipient}"})
    return jsonify({"error": result.get("error")}), 500

@app.route("/api/diagnostics", methods=["GET"])
def get_diagnostics():
    return jsonify({
        "packet_count": packet_count,
        "distance": current_distance,
        "rssi": current_rssi,
        "last_packet": last_packet_time.isoformat() if last_packet_time else None,
        "database": {"status": "connected" if db_pool else "not_configured"},
        "email_config": check_email_config(),
        "version": ALGORITHM_VERSION
    })

@app.route("/api/data/history", methods=["GET"])
def get_history():
    hours = request.args.get('hours', 24, type=int)
    data = get_vital_signs(limit=1000, hours=hours)
    for row in data:
        if row.get('timestamp'):
            row['timestamp'] = row['timestamp'].isoformat()
    return jsonify({"status": "ok", "count": len(data), "data": data})

@app.route("/api/data/statistics", methods=["GET"])
def get_stats():
    stats = get_statistics(hours=24)
    if not stats:
        return jsonify({"error": "No hay datos"}), 404
    if stats.get('first_reading'):
        stats['first_reading'] = stats['first_reading'].isoformat()
    if stats.get('last_reading'):
        stats['last_reading'] = stats['last_reading'].isoformat()
    return jsonify({"status": "ok", "statistics": stats})

@app.route("/api/export/csv", methods=["GET"])
def export_csv():
    data = get_vital_signs(limit=10000, hours=24)
    if not data:
        return jsonify({"error": "No hay datos"}), 404
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['timestamp', 'spo2', 'hr', 'spo2_critical', 'hr_critical'])
    for row in data:
        writer.writerow([row.get('timestamp',''), row.get('spo2',''), row.get('hr',''),
                        row.get('spo2_critical',''), row.get('hr_critical','')])
    output.seek(0)
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=vital_signs.csv'})

# PDF Report
@app.route("/api/report/pdf", methods=["POST"])
def api_report_pdf():
    try:
        patient = request.get_json(silent=True) or {}
        stats = get_statistics(hours=24)
        if not stats:
            return jsonify({"error": "No hay datos"}), 400
        
        prompt = f"""Genera informe médico HTML profesional.
Paciente: {patient.get('name','N/A')} | Edad: {patient.get('age','N/A')}
SpO₂: Media {stats.get('spo2_avg',0)}% | HR: Media {stats.get('hr_avg',0)} bpm
Muestras: {stats.get('total_samples',0)}
"""
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000
        )
        html_content = response.choices[0].message.content.strip()
        if html_content.startswith("```"):
            html_content = html_content.split("```")[1].replace("html","",1)
        
        pdf = BytesIO()
        HTML(string=html_content).write_pdf(pdf)
        pdf.seek(0)
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, 
                        download_name=f"informe_{datetime.now().strftime('%Y%m%d')}.pdf")
    except Exception as e:
        print(f"[ERROR] PDF: {e}")
        return jsonify({"error": str(e)}), 500

# --------------------------------------------------
# WEBSOCKET EVENTS
# --------------------------------------------------
@socketio.on('connect')
def handle_connect():
    print(f'[WebSocket] Cliente conectado (total datos: {len(spo2_hist)})')
    # Enviar estado actual al cliente que se conecta
    if len(spo2_hist) > 0:
        socketio.emit('update', {
            "spo2": spo2_hist[-1], "hr": hr_hist[-1],
            "spo2_history": list(spo2_hist), "hr_history": list(hr_hist),
            "spo2_critical": spo2_hist[-1] < CRITICAL_SPO2,
            "hr_critical": hr_hist[-1] < CRITICAL_HR_LOW or hr_hist[-1] > CRITICAL_HR_HIGH
        })
    socketio.emit('raw_update', {"count": packet_count, "distance": current_distance, "rssi": current_rssi})

@socketio.on('disconnect')
def handle_disconnect():
    print('[WebSocket] Cliente desconectado')

# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  {SYSTEM_NAME}
║  Versión: {ALGORITHM_VERSION}
╠══════════════════════════════════════════════════════════════╣
║  DATABASE: {'✅ OK' if DATABASE_URL else '❌ No'}
║  EMAIL:    {'✅ OK' if EMAIL_FROM and EMAIL_PASS else '❌ No'}
╚══════════════════════════════════════════════════════════════╝
    """)
    
    if init_db_pool():
        init_database()
    
    port = int(os.environ.get("PORT", 5050))
    socketio.run(app, host="0.0.0.0", port=port)