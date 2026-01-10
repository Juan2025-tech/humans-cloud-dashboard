#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua
======================================
Versión: 1.6.0-resend-api

CAMBIOS:
- Usa Resend API (HTTP) en lugar de SMTP (bloqueado por Render free tier)
- WebSocket emit directo
- Eventlet compatible
"""

# ============================================================
# CRÍTICO: Monkey patch ANTES de cualquier otro import
# ============================================================
import eventlet
eventlet.monkey_patch()

import os
import time
import requests
import json
from datetime import datetime, timezone
from collections import deque
from io import BytesIO, StringIO
import csv

import numpy as np

# PostgreSQL
try:
    import psycopg2
    from psycopg2 import pool
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
    psycopg2 = None
    pool = None
    RealDictCursor = None

from flask import Flask, render_template, jsonify, request, send_file, Response
from flask_socketio import SocketIO
from weasyprint import HTML
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(".env")
client = OpenAI()
LLM_MODEL = "gpt-4o-mini"

SYSTEM_NAME = "HumanS – Monitorización Vital Continua"
ALGORITHM_VERSION = "1.6.0-resend-api"

# Email - Resend API
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

def init_db_pool():
    global db_pool
    if not POSTGRES_AVAILABLE or not DATABASE_URL:
        return False
    try:
        conn_str = DATABASE_URL.replace("postgres://", "postgresql://", 1) if DATABASE_URL.startswith("postgres://") else DATABASE_URL
        db_pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=conn_str)
        print("✅ PostgreSQL conectado")
        return True
    except Exception as e:
        print(f"❌ PostgreSQL: {e}")
        return False

def get_db_connection():
    return db_pool.getconn() if db_pool else None

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

def get_statistics(hours=24):
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

def check_email_config():
    issues = []
    if not RESEND_API_KEY: issues.append("RESEND_API_KEY no configurado")
    return {"configured": len(issues)==0, "issues": issues, "provider": "Resend API (HTTP)"}

def generate_email_html(alert_type, spo2, hr, patient_info):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")
    color = "#17a2b8" if alert_type == 'test' else "#dc3545"
    title = "🧪 TEST" if alert_type == 'test' else f"⚠️ ALERTA: {'SpO₂ Bajo' if alert_type == 'spo2' else 'HR Anormal'}"
    return f"""<!DOCTYPE html><html><body style="font-family:Arial;max-width:600px;margin:auto;padding:20px;">
    <div style="background:{color};color:white;padding:20px;border-radius:10px 10px 0 0;text-align:center;"><h1>{title}</h1></div>
    <div style="background:#f8f9fa;padding:20px;border-radius:0 0 10px 10px;">
    <div style="font-size:42px;font-weight:bold;color:{color};text-align:center;margin:20px;">SpO₂: {spo2}% | HR: {hr} bpm</div>
    <p><strong>Paciente:</strong> {patient_info.get('name','N/A')} | <strong>Hab:</strong> {patient_info.get('room','N/A')}</p>
    <p style="font-size:11px;color:#888;text-align:center;">{SYSTEM_NAME} v{ALGORITHM_VERSION} | {now}</p>
    </div></body></html>"""

def send_email_resend(recipient, subject, html):
    if not RESEND_API_KEY:
        print("❌ RESEND_API_KEY no configurado")
        return {"success": False, "error": "API key no configurado"}
    print(f"📧 Enviando via Resend a {recipient}...")
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

def send_alert_async(alert_type, spo2, hr):
    def _send():
        recipient = email_config.get("email_to")
        if not recipient: return
        patient_info = {"name": email_config.get("patient_name","N/A"), "room": email_config.get("patient_room","N/A")}
        subject = f"⚠️ ALERTA HumanS - {alert_type.upper()} - {patient_info['name']}"
        result = send_email_resend(recipient, subject, generate_email_html(alert_type, spo2, hr, patient_info))
        save_alert(alert_type, spo2, hr, subject, result["success"], recipient, patient_info["name"])
        if result["success"]: socketio.emit('alert_sent', {'type': alert_type, 'message': f'Email enviado a {recipient}'})
    eventlet.spawn(_send)

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
        if spo2_crit and not last_spo2_critical and now - last_spo2_alert_time > EMAIL_COOLDOWN:
            send_alert_async('spo2', spo2, hr)
            last_spo2_alert_time = now
        if hr_crit and not last_hr_critical and now - last_hr_alert_time > EMAIL_COOLDOWN:
            send_alert_async('hr', spo2, hr)
            last_hr_alert_time = now
        last_spo2_critical, last_hr_critical = spo2_crit, hr_crit
        
        data = {"spo2": spo2, "hr": hr, "spo2_history": list(spo2_hist), "hr_history": list(hr_hist),
                "spo2_critical": spo2_crit, "hr_critical": hr_crit}
        socketio.emit('update', data)
        socketio.emit('raw_update', {"count": packet_count, "distance": current_distance, "rssi": current_rssi})
        return jsonify({"status": "ok", "packet": packet_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    return jsonify({"status": "ok", "email_to": email})

@app.route("/api/email/diagnose", methods=["GET"])
def diagnose_email():
    return jsonify({"config": check_email_config(), "recipient": email_config.get("email_to",""),
                   "note": "Resend API usa HTTP, no SMTP. Compatible con Render free tier."})

@app.route("/api/email/test", methods=["POST"])
def test_email():
    d = request.get_json() or {}
    recipient = d.get("email_to") or email_config.get("email_to")
    if not recipient: return jsonify({"error": "No hay email configurado"}), 400
    if not RESEND_API_KEY: return jsonify({"error": "RESEND_API_KEY no configurado", "hint": "Añade RESEND_API_KEY en Render Environment Variables"}), 500
    patient = d.get("patient_name") or email_config.get("patient_name","Prueba")
    result = send_email_resend(recipient, f"🧪 TEST HumanS - {patient}", generate_email_html('test', 97, 72, {"name": patient}))
    if result["success"]: return jsonify({"status": "ok", "message": f"✅ Email enviado a {recipient}"})
    return jsonify({"error": result.get("error")}), 500

@app.route("/api/diagnostics", methods=["GET"])
def diagnostics():
    return jsonify({"packet_count": packet_count, "distance": current_distance, "rssi": current_rssi,
                   "last_packet": last_packet_time.isoformat() if last_packet_time else None,
                   "database": "connected" if db_pool else "no", "email": check_email_config(), "version": ALGORITHM_VERSION})

@app.route("/api/data/statistics", methods=["GET"])
def stats(): return jsonify(get_statistics() or {"error": "No data"})

@app.route("/api/report/pdf", methods=["POST"])
def pdf_report():
    try:
        patient = request.get_json(silent=True) or {}
        stats = get_statistics()
        if not stats: return jsonify({"error": "No hay datos"}), 400
        prompt = f"Genera informe médico HTML. Paciente: {patient.get('name','N/A')}, SpO₂: {stats.get('spo2_avg',0)}%, HR: {stats.get('hr_avg',0)} bpm"
        r = client.chat.completions.create(model=LLM_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=2000)
        html = r.choices[0].message.content.strip()
        if html.startswith("```"): html = html.split("```")[1].replace("html","",1)
        pdf = BytesIO()
        HTML(string=html).write_pdf(pdf)
        pdf.seek(0)
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=f"informe_{datetime.now().strftime('%Y%m%d')}.pdf")
    except Exception as e: return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def on_connect():
    print(f'[WS] Cliente conectado ({len(spo2_hist)} datos)')
    if spo2_hist:
        socketio.emit('update', {"spo2": spo2_hist[-1], "hr": hr_hist[-1], "spo2_history": list(spo2_hist),
                                "hr_history": list(hr_hist), "spo2_critical": spo2_hist[-1] < CRITICAL_SPO2,
                                "hr_critical": hr_hist[-1] < CRITICAL_HR_LOW or hr_hist[-1] > CRITICAL_HR_HIGH})
    socketio.emit('raw_update', {"count": packet_count, "distance": current_distance, "rssi": current_rssi})

@socketio.on('disconnect')
def on_disconnect(): print('[WS] Cliente desconectado')

if __name__ == "__main__":
    print(f"\n{'='*60}\n{SYSTEM_NAME} v{ALGORITHM_VERSION}\n{'='*60}")
    print(f"DATABASE: {'✅' if DATABASE_URL else '❌'}")
    print(f"RESEND_API_KEY: {'✅' if RESEND_API_KEY else '❌ No configurado'}")
    print(f"EMAIL_FROM: {EMAIL_FROM}")
    print(f"\n⚠️  Render FREE bloquea SMTP → Usamos Resend API (HTTP)\n{'='*60}\n")
    if init_db_pool(): init_database()
    socketio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))