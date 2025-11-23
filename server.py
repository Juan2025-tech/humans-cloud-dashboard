import os
import smtplib
import csv
from datetime import datetime
from email.mime.text import MIMEText
from collections import deque
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO

# --- Configuración ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'clave-secreta-local')
socketio = SocketIO(app, cors_allowed_origins="*")  # Para WebSocket desde cualquier origen

# --- Estado y Umbrales ---
MAX_HISTORY = 120
spo2_hist = deque(maxlen=MAX_HISTORY)
hr_hist = deque(maxlen=MAX_HISTORY)
last_data_packet = {}

CRITICAL_SPO2 = 92
CRITICAL_HR_LOW = 60
CRITICAL_HR_HIGH = 150

# --- API_KEY para ESP32 ---
API_KEY = os.environ.get("API_KEY", "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6")

# --- Configuración de Email y CSV ---
EMAIL_TO = "jperez@intecestudio.com"
EMAIL_FROM = os.environ.get("GMAIL_USER")
EMAIL_PASS = os.environ.get("GMAIL_PASS")

DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "history.csv")
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp_iso", "spo2", "hr", "spo2_critical", "hr_critical"])

# --- Funciones Auxiliares ---
def save_csv_row(spo2, hr, spo2_critical, hr_critical):
    ts = datetime.utcnow().isoformat()
    try:
        with open(CSV_PATH, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([ts, spo2, hr, int(spo2_critical), int(hr_critical)])
    except Exception as e:
        print(f"ERROR al guardar en CSV: {e}")

def send_alert_email(subject, message):
    if not EMAIL_FROM or not EMAIL_PASS:
        print("ADVERTENCIA: Faltan GMAIL_USER o GMAIL_PASS. No se enviará email.")
        return
    try:
        msg = MIMEText(message)
        msg['Subject'] = subject
        msg['From'] = EMAIL_FROM
        msg['To'] = EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_FROM, EMAIL_PASS)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        print(f"Email de alerta enviado a {EMAIL_TO}.")
    except Exception as e:
        print(f"ERROR al enviar email: {e}")

# --- Rutas de la App ---
@app.route('/api/data', methods=['POST'])
def receive_data():
    global last_data_packet
    # Validar API_KEY
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json()
    if not data: 
        return jsonify({"error": "Petición vacía"}), 400

    print(f"Datos recibidos del ESP32: {data}")
    spo2 = data.get('spo2')
    hr = data.get('hr')

    if spo2 is None or hr is None: 
        return jsonify({"error": "Faltan 'spo2' o 'hr'"}), 400

    spo2_hist.append(spo2)
    hr_hist.append(hr)

    spo2_crit = spo2 < CRITICAL_SPO2
    hr_crit = (hr < CRITICAL_HR_LOW) or (hr > CRITICAL_HR_HIGH)

    new_data_packet = {
        'spo2': spo2, 'hr': hr,
        'spo2_history': list(spo2_hist), 'hr_history': list(hr_hist),
        'spo2_critical': spo2_crit, 'hr_critical': hr_crit
    }

    socketio.emit('update', new_data_packet)
    save_csv_row(spo2, hr, spo2_crit, hr_crit)

    if spo2_crit and not last_data_packet.get('spo2_critical', False):
        send_alert_email("⚠ ALERTA CRÍTICA — SpO2 Baja", f"SpO₂ crítico: {spo2}%\nHR: {hr} bpm")
    if hr_crit and not last_data_packet.get('hr_critical', False):
        send_alert_email("⚠ ALERTA CRÍTICA — HR Fuera de Rango", f"HR crítico: {hr} bpm\nSpO₂: {spo2}%")

    last_data_packet = new_data_packet
    return jsonify({"status": "ok"}), 200

@app.route('/')
def index():
    return render_template('index.html')

# --- Eventos de WebSocket ---
@socketio.on('connect')
def handle_connect():
    print('Cliente web conectado.')
    if last_data_packet: socketio.emit('update', last_data_packet)

@socketio.on('disconnect')
def handle_disconnect():
    print('Cliente web desconectado.')

# --- Ejecución local ---
if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=True)

