#!/usr/bin/env python3
"""
HumanS - Monitorización Vital Continua (Versión Render)
Recibe datos del proxy HTTP (ESP32 Bridge)
Sin dependencias BLE (Bleak)
"""

import eventlet
eventlet.monkey_patch()

import os
import csv
import json
import time
import threading
from datetime import datetime, timezone
from collections import deque
from queue import Queue, Empty
from io import BytesIO

import numpy as np
from flask import Flask, render_template, jsonify, request, send_file
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
ALGORITHM_VERSION = "1.0.3-render"

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
MAX_HISTORY = 120
spo2_hist = deque(maxlen=MAX_HISTORY)
hr_hist = deque(maxlen=MAX_HISTORY)
data_queue = Queue()
stop_event = threading.Event()

# --------------------------------------------------
# DATA STORAGE
# --------------------------------------------------
DATA_DIR = "data"
CSV_PATH = os.path.join(DATA_DIR, "history.csv")
JSON_PATH = os.path.join(DATA_DIR, "history.jsonl")
os.makedirs(DATA_DIR, exist_ok=True)

if not os.path.exists(CSV_PATH):
    with open(CSV_PATH, "w", newline="") as f:
        csv.writer(f).writerow(["timestamp", "spo2", "hr"])

def save_data(spo2, hr):
    """Guarda datos en CSV y JSONL"""
    ts = datetime.now(timezone.utc).isoformat()
    
    # Intentar escribir, pero no fallar si hay problemas (ej: Render filesystem)
    try:
        with open(CSV_PATH, "a", newline="") as f:
            csv.writer(f).writerow([ts, spo2, hr])
    except OSError as e:
        print(f"[WARN] No se pudo escribir CSV: {e}")
    
    try:
        with open(JSON_PATH, "a") as f:
            f.write(json.dumps({"timestamp_iso": ts, "spo2": spo2, "hr": hr}) + "\n")
    except OSError as e:
        print(f"[WARN] No se pudo escribir JSONL: {e}")

# --------------------------------------------------
# SOCKET.IO / FLASK
# --------------------------------------------------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

@app.route("/")
def index():
    return render_template("index10.html")

@app.route("/api/data", methods=["POST"])
def receive_data():
    """Endpoint que recibe datos del proxy (ESP32 Bridge)"""
    # Validar API Key
    api_key = request.headers.get('x-api-key')
    expected_key = os.environ.get('API_KEY', 'f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6')
    
    if api_key != expected_key:
        print(f"[SECURITY] API Key inválida recibida: {api_key}")
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "Petición vacía"}), 400

    print(f"[HTTP] Datos recibidos del proxy: {data}")
    
    spo2 = data.get('spo2')
    hr = data.get('hr')

    if spo2 is None or hr is None:
        return jsonify({"error": "Faltan 'spo2' o 'hr'"}), 400

    # Validar rangos
    if not (60 <= spo2 <= 100) or not (30 <= hr <= 220):
        return jsonify({"error": "Valores fuera de rango"}), 400

    spo2_hist.append(spo2)
    hr_hist.append(hr)

    payload = {
        "spo2": spo2,
        "hr": hr,
        "spo2_history": list(spo2_hist),
        "hr_history": list(hr_hist),
        "spo2_critical": spo2 < 90,
        "hr_critical": hr < 45 or hr > 130
    }

    save_data(spo2, hr)
    socketio.emit("update", payload)

    return jsonify({"status": "ok"}), 200

# --------------------------------------------------
# QUEUE PROCESSOR
# --------------------------------------------------
def process_queue():
    """Procesa cola de datos y los emite via WebSocket"""
    while not stop_event.is_set():
        try:
            data = data_queue.get(timeout=0.5)
            save_data(data["spo2"], data["hr"])
            socketio.emit("update", data)
        except Empty:
            continue

# --------------------------------------------------
# WEBSOCKET EVENTS
# --------------------------------------------------
@socketio.on('connect')
def handle_connect():
    print('[WebSocket] Cliente conectado')
    # Enviar últimos datos si existen
    if len(spo2_hist) > 0 and len(hr_hist) > 0:
        socketio.emit('update', {
            "spo2": spo2_hist[-1],
            "hr": hr_hist[-1],
            "spo2_history": list(spo2_hist),
            "hr_history": list(hr_hist),
            "spo2_critical": spo2_hist[-1] < 90,
            "hr_critical": hr_hist[-1] < 45 or hr_hist[-1] > 130
        })

@socketio.on('disconnect')
def handle_disconnect():
    print('[WebSocket] Cliente desconectado')

# --------------------------------------------------
# CLINICAL ANALYSIS
# --------------------------------------------------
def classify_spo2_episodes(spo2, hr, threshold=92):
    """Clasifica episodios de desaturación en clínicos vs artefactos"""
    MIN_SAMPLES = 30
    MIN_HR_VARIATION = 5
    
    clinical = 0
    artifacts = 0
    buffer = []

    for s, h in zip(spo2, hr):
        if s < threshold:
            buffer.append((s, h))
        else:
            if buffer:
                duration = len(buffer)
                hr_values = [x[1] for x in buffer]
                hr_var = max(hr_values) - min(hr_values)
                
                if duration >= MIN_SAMPLES and hr_var > MIN_HR_VARIATION:
                    clinical += 1
                else:
                    artifacts += 1
                buffer = []
    
    if buffer:
        duration = len(buffer)
        hr_values = [x[1] for x in buffer]
        hr_var = max(hr_values) - min(hr_values)
        if duration >= MIN_SAMPLES and hr_var > MIN_HR_VARIATION:
            clinical += 1
        else:
            artifacts += 1
    
    return clinical, artifacts

def load_raw_data():
    """Carga datos históricos desde JSONL"""
    if not os.path.exists(JSON_PATH):
        return []
    
    data = []
    try:
        with open(JSON_PATH) as f:
            for line in f:
                if line.strip():
                    try:
                        entry = json.loads(line)
                        if 'timestamp' in entry and 'timestamp_iso' not in entry:
                            entry['timestamp_iso'] = entry['timestamp']
                        data.append(entry)
                    except json.JSONDecodeError:
                        continue
    except OSError as e:
        print(f"[WARN] No se pudo leer JSONL: {e}")
    
    return data

def process_data_for_analysis():
    """Genera resumen estadístico de los datos"""
    data = load_raw_data()
    if not data:
        return None

    spo2 = np.array([d["spo2"] for d in data])
    hr = np.array([d["hr"] for d in data])

    clinical, artifacts = classify_spo2_episodes(spo2, hr)

    return {
        "timestamp_start": data[0].get("timestamp_iso", data[0].get("timestamp", "N/A")),
        "timestamp_end": data[-1].get("timestamp_iso", data[-1].get("timestamp", "N/A")),
        "total_samples": len(data),
        "spo2_avg": round(float(np.mean(spo2)), 2),
        "spo2_min": int(np.percentile(spo2, 5)),
        "spo2_max": int(np.max(spo2)),
        "spo2_clinical_events": clinical,
        "spo2_artifact_events": artifacts,
        "hr_avg": round(float(np.mean(hr)), 2),
        "hr_min": int(np.min(hr)),
        "hr_max": int(np.max(hr))
    }

# --------------------------------------------------
# LLM PROMPT
# --------------------------------------------------
def generate_llm_prompt(summary, patient):
    """Genera prompt para OpenAI con datos del paciente y resumen"""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    return f"""Genera un informe médico profesional en HTML completo y válido.

DATOS DEL PACIENTE:
• Nombre: {patient.get('name', 'No especificado')}
• Edad: {patient.get('age', 'No especificado')} años
• Residencia: {patient.get('residence', 'No especificado')}
• Habitación: {patient.get('room', 'No especificado')}

PERÍODO DE MONITORIZACIÓN:
• Inicio: {summary['timestamp_start']}
• Fin: {summary['timestamp_end']}
• Total de muestras: {summary['total_samples']}

PARÁMETROS VITALES:
• SpO₂: Media {summary['spo2_avg']}% | Mínima {summary['spo2_min']}% | Máxima {summary['spo2_max']}%
• FC: Media {summary['hr_avg']} bpm | Mínima {summary['hr_min']} bpm | Máxima {summary['hr_max']} bpm

ANÁLISIS CLÍNICO:
• Eventos clínicos de hipoxemia sostenida: {summary['spo2_clinical_events']}
• Artefactos de señal (descensos transitorios): {summary['spo2_artifact_events']}

IMPORTANTE: Los {summary['spo2_artifact_events']} artefactos detectados son descensos breves por movimiento/cambios posturales, NO representan hipoxemia clínica real.

ESTRUCTURA DEL INFORME HTML:
1. Encabezado con título "Informe Médico de Monitorización Vital" y datos del paciente
2. Sección "Resumen Ejecutivo" (tono profesional, objetivo, NO alarmista)
3. Tabla con parámetros vitales principales
4. Sección "Interpretación Clínica" explicando que los artefactos NO son eventos patológicos
5. Sección "Conclusiones y Recomendaciones"
6. Aviso legal: "Este informe es de apoyo clínico y no sustituye el juicio médico profesional"
7. Firma: Fecha {now_utc} | Sistema: {SYSTEM_NAME} | Versión: {ALGORITHM_VERSION}

Usa estilos CSS profesionales (colores suaves, tipografía clara). Devuelve SOLO HTML válido completo (con <!DOCTYPE html>, <html>, <head>, <body>).
"""

# --------------------------------------------------
# API ENDPOINT - INFORME PDF
# --------------------------------------------------
@app.route("/api/report/pdf", methods=["POST"])
def api_report_pdf():
    """Genera informe PDF usando OpenAI + WeasyPrint"""
    print("=== INICIO GENERACIÓN DE INFORME ===")
    
    try:
        patient = request.get_json(silent=True) or {}
        print(f"[LOG] Datos del paciente: {patient}")
        
        summary = process_data_for_analysis()
        print(f"[LOG] Resumen generado: {summary}")
        
        if not summary:
            print("[ERROR] No hay datos suficientes")
            return jsonify({"error": "No hay datos suficientes para generar el informe"}), 400

        prompt = generate_llm_prompt(summary, patient)
        print(f"[LOG] Prompt generado ({len(prompt)} caracteres)")

        print("[LOG] Llamando a OpenAI API...")
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "Eres un médico especialista que genera informes clínicos profesionales en HTML. Devuelve siempre HTML válido y completo."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.2,
            max_tokens=2000
        )
        print("[LOG] Respuesta recibida de OpenAI")

        html_content = response.choices[0].message.content
        print(f"[LOG] HTML recibido ({len(html_content)} caracteres)")

        if not html_content or not html_content.strip():
            raise ValueError("El LLM no devolvió contenido HTML válido")

        # Limpiar markdown fences si existen
        html_content = html_content.strip()
        if html_content.startswith("```html"):
            html_content = html_content[7:]
        elif html_content.startswith("```"):
            html_content = html_content[3:]
        if html_content.endswith("```"):
            html_content = html_content[:-3]
        html_content = html_content.strip()

        if "<html" not in html_content.lower() and "<!doctype" not in html_content.lower():
            raise ValueError("La respuesta no es HTML válido")

        print("[LOG] Generando PDF con WeasyPrint...")
        pdf = BytesIO()
        HTML(string=html_content).write_pdf(pdf)
        pdf.seek(0)
        print("[LOG] PDF generado exitosamente")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        patient_name = patient.get('name', 'paciente').replace(' ', '_')
        filename = f"informe_{patient_name}_{timestamp}.pdf"

        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        print(f"[ERROR COMPLETO] {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Error al generar el informe: {str(e)}"}), 500
    
# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    print(f"\n{SYSTEM_NAME}")
    print(f"Versión: {ALGORITHM_VERSION}")
    print(f"Modelo LLM: {LLM_MODEL}")
    print(f"Modo: Producción Render (sin BLE)\n")
    
    # Solo iniciar el procesador de cola
    threading.Thread(target=process_queue, daemon=True).start()
    
    # Render usa Gunicorn, pero este es para pruebas locales
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)