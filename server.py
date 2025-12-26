#!/usr/bin/env python3
import asyncio
import threading
import time
import webbrowser
import csv
import os
import json
from datetime import datetime, timezone
from collections import deque
from queue import Queue, Empty
from io import BytesIO

import numpy as np
from bleak import BleakClient, BleakScanner
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
ALGORITHM_VERSION = "1.0.2-clinical"

# --------------------------------------------------
# BLE UUIDs
# --------------------------------------------------
SEND_CHAR_UUID = "49535343-1E4D-4BD9-BA61-23C647249616"
RECV_CHAR_UUID = "49535343-8841-43F4-A8D4-ECBE34729BB3"

# --------------------------------------------------
# CONFIG
# --------------------------------------------------
MAX_HISTORY = 120
spo2_hist = deque(maxlen=MAX_HISTORY)
hr_hist = deque(maxlen=MAX_HISTORY)
data_queue = Queue()
stop_event = threading.Event()

# Contador de paquetes para diagnóstico
packet_count = 0
raw_packet_queue = Queue()  # Cola para diagnóstico de operaciones
raw_packet_throttle = 0  # Contador para reducir frecuencia de envío (1 de cada 5)

# Cálculo de distancia (basado en RSSI) - SISTEMA HÍBRIDO
TX_POWER = -70  # Potencia de transmisión típica del BerryMed
N_FACTOR = 2.4  # Factor de propagación en interiores
RSSI_WINDOW_SIZE = 10
rssi_buffer = deque(maxlen=RSSI_WINDOW_SIZE)
current_distance = 0.0
current_rssi = None

# SISTEMA DE RECONEXIÓN INTELIGENTE (para actualizar RSSI en macOS)
CONNECTION_DURATION = 20  # segundos conectado antes de reconectar
RECONNECTION_DELAY = 0.5  # segundos de pausa para scan de RSSI
rssi_update_count = 0  # Contador de actualizaciones de RSSI

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
    ts = datetime.now(timezone.utc).isoformat()
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([ts, spo2, hr])
    with open(JSON_PATH, "a") as f:
        f.write(json.dumps({"timestamp_iso": ts, "spo2": spo2, "hr": hr}) + "\n")

# --------------------------------------------------
# FLASK
# --------------------------------------------------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

@app.route("/")
def index():
    return render_template("index10.html")

# --------------------------------------------------
# BLE DECODING (BERRY PROTOCOL V1.5)
# --------------------------------------------------
def calculate_distance(rssi):
    """Calcula distancia en metros basada en RSSI"""
    if rssi is None or rssi >= 0:
        return 0.0
    return round(10**((TX_POWER - rssi) / (10 * N_FACTOR)), 2)

def decode_berry_packet(data: bytes):
    """
    Decodifica paquete según Berry Protocol v1.5
    Retorna diccionario con todos los campos técnicos
    """
    if len(data) != 20:
        return None
    
    # Verificar encabezado (Byte0=0xFF, Byte1=0xAA)
    if data[0] != 0xFF or data[1] != 0xAA:
        return None
    
    # Extraer todos los campos según el protocolo
    packet_info = {
        "valid": True,
        "pkt_index": data[2],
        "status": data[3],
        "spo2_avg": data[4] if data[4] != 127 else None,
        "spo2_real": data[5] if data[5] != 127 else None,
        "pulse_avg": data[6] if data[6] != 255 else None,
        "pulse_real": data[7] if data[7] != 255 else None,
        "rr_interval": (data[9] << 8) | data[8],  # High byte + Low byte
        "perfu_index": data[10] if data[10] != 0 else None,
        "perfu_index_real": data[11] if data[11] != 0 else None,
        "pleth_wave": data[12] if data[12] != 0 else None,
        "battery": data[17],
        "freq": data[18],
        "checksum": data[19]
    }
    
    # Validar checksum
    calculated_checksum = sum(data[:19]) % 256
    packet_info["checksum_valid"] = (calculated_checksum == data[19])
    
    # Decodificar status flags
    packet_info["sensor_off"] = bool(data[3] & 0x01)
    packet_info["no_finger"] = bool(data[3] & 0x02)
    packet_info["no_pulse"] = bool(data[3] & 0x04)
    packet_info["pulse_beat"] = bool(data[3] & 0x08)
    
    return packet_info

def decode_packet_for_monitoring(data: bytes):
    """Decodifica solo para monitorización clínica (valores promediados)"""
    if len(data) != 20:
        return None

    hr_samples = [data[3], data[8], data[13], data[18]]
    spo2_samples = [data[4], data[9], data[14], data[19]]

    hr_valid = [h for h in hr_samples if 45 <= h <= 180]
    spo2_valid = [s for s in spo2_samples if 85 <= s <= 100]

    if not hr_valid or not spo2_valid:
        return None

    return int(np.mean(spo2_valid)), int(np.mean(hr_valid))

def handle_notification(sender, data):
    global packet_count, raw_packet_throttle, current_distance
    packet_count += 1
    
    # THROTTLE REDUCIDO: Solo enviar 1 de cada 5 paquetes al diagnóstico (50ms en vez de 10ms)
    raw_packet_throttle += 1
    should_send_diagnostic = (raw_packet_throttle >= 5)
    
    if should_send_diagnostic:
        raw_packet_throttle = 0  # Reset contador
        
        # Enviar diagnóstico simplificado (solo contador + distancia)
        raw_payload = {
            "count": packet_count,
            "distance": current_distance,
            "rssi": current_rssi
        }
        raw_packet_queue.put(raw_payload)
    
    # Procesamiento para monitorización clínica (método antiguo)
    decoded = decode_packet_for_monitoring(data)
    if not decoded:
        return

    spo2, hr = decoded
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

    data_queue.put(payload)

# --------------------------------------------------
# QUEUE PROCESSORS
# --------------------------------------------------
def process_queue():
    while not stop_event.is_set():
        try:
            data = data_queue.get(timeout=0.5)
            save_data(data["spo2"], data["hr"])
            socketio.emit("update", data)
        except Empty:
            continue

def process_raw_queue():
    """Procesa y envía datos de diagnóstico de operaciones"""
    while not stop_event.is_set():
        try:
            raw_data = raw_packet_queue.get(timeout=0.5)
            socketio.emit("raw_update", raw_data)
        except Empty:
            continue

# --------------------------------------------------
# BLE LOOP (SISTEMA HÍBRIDO CON RECONEXIÓN INTELIGENTE)
# --------------------------------------------------
async def ble_connect():
    global current_distance, current_rssi, rssi_update_count
    
    target_address = None  # Guardar dirección para reconexiones rápidas
    
    while not stop_event.is_set():
        try:
            # FASE 1: Escanear para obtener dispositivo y RSSI actualizado
            print(f"[BLE] {'Escaneando' if not target_address else 'Actualizando RSSI'}...")
            
            target_device = None
            target_rssi = None
            
            def detection_callback(device, advertisement_data):
                nonlocal target_device, target_rssi
                # Si ya tenemos una dirección guardada, buscar específicamente ese dispositivo
                if target_address:
                    if device.address == target_address:
                        target_device = device
                        target_rssi = advertisement_data.rssi
                # Si es primera vez, buscar cualquier BerryMed
                elif device.name and "BerryMed" in device.name:
                    target_device = device
                    target_rssi = advertisement_data.rssi
            
            scanner = BleakScanner(detection_callback=detection_callback)
            await scanner.start()
            
            # Scan más corto si ya conocemos el dispositivo (optimización)
            scan_duration = 2 if target_address else 5
            await asyncio.sleep(scan_duration)
            await scanner.stop()

            if not target_device:
                if target_address:
                    # Dispositivo conocido no encontrado, resetear y buscar de nuevo
                    print("[BLE] ⚠️  Dispositivo perdido, buscando nuevamente...")
                    target_address = None
                else:
                    print("[BLE] Dispositivo no encontrado, reintentando...")
                await asyncio.sleep(3)
                continue

            # Guardar dirección para próximas reconexiones
            target_address = target_device.address
            
            # Actualizar RSSI y distancia
            current_rssi = target_rssi
            rssi_buffer.append(current_rssi)
            avg_rssi = sum(rssi_buffer) / len(rssi_buffer)
            current_distance = calculate_distance(avg_rssi)
            rssi_update_count += 1
            
            if rssi_update_count == 1:
                print(f"[BLE] Dispositivo: {target_device.name} ({target_device.address})")
                print(f"[BLE] Sistema híbrido activado (actualización cada {CONNECTION_DURATION}s)")
            
            print(f"[BLE] RSSI: {current_rssi} dBm | Distancia: {current_distance}m | Actualización #{rssi_update_count}")
            
            # FASE 2: Conectar y recibir datos vitales
            async with BleakClient(target_device.address, timeout=15.0) as client_ble:
                await client_ble.start_notify(SEND_CHAR_UUID, handle_notification)
                await client_ble.write_gatt_char(RECV_CHAR_UUID, bytes([0xF1]), True)
                
                if rssi_update_count == 1:
                    print("[BLE] ✅ Conectado - Monitorizando constantemente")
                
                # Mantener conexión durante CONNECTION_DURATION segundos
                connection_start = time.time()
                while client_ble.is_connected and not stop_event.is_set():
                    elapsed = time.time() - connection_start
                    if elapsed >= CONNECTION_DURATION:
                        print(f"[BLE] Desconectando para actualizar RSSI...")
                        break
                    await asyncio.sleep(0.5)
            
            # Pequeña pausa antes de reconectar (permite que el dispositivo vuelva a advertising)
            await asyncio.sleep(RECONNECTION_DELAY)

        except asyncio.TimeoutError:
            print(f"[BLE] Timeout en conexión, reintentando...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"[BLE] Error: {e}")
            # Si hay error repetido, resetear dirección guardada
            target_address = None
            current_distance = 0.0
            current_rssi = None
            await asyncio.sleep(5)

def start_ble():
    asyncio.run(ble_connect())

# --------------------------------------------------
# CLINICAL ANALYSIS
# --------------------------------------------------
def classify_spo2_episodes(spo2, hr, threshold=92):
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
    if not os.path.exists(JSON_PATH):
        return []
    
    data = []
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
    return data

def process_data_for_analysis():
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
# PROMPT
# --------------------------------------------------
def generate_llm_prompt(summary, patient):
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
# API ENDPOINT
# --------------------------------------------------
@app.route("/api/report/pdf", methods=["POST"])
def api_report_pdf():
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
def open_browser():
    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:5000")

if __name__ == "__main__":
    print(f"\n{SYSTEM_NAME}")
    print(f"Versión: {ALGORITHM_VERSION}")
    print(f"Modelo LLM: {LLM_MODEL}\n")
    
    threading.Thread(target=process_queue, daemon=True).start()
    threading.Thread(target=process_raw_queue, daemon=True).start()
    threading.Thread(target=start_ble, daemon=True).start()
    threading.Thread(target=open_browser, daemon=True).start()
    
    socketio.run(app, host="127.0.0.1", port=5000, debug=False)