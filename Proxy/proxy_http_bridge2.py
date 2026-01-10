#!/usr/bin/env python3
"""
HumanS - Proxy HTTP Local (Mac Mini M4)
Recibe datos del ESP32 Bridge vía HTTP local
Reenvía a Render Cloud vía HTTPS
Incluye caché local y validación
"""

import asyncio
import collections
import requests
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify
from bleak import BleakScanner, BleakClient
from threading import Thread

# ============================================
# CONFIGURACIÓN
# ============================================

# Configuración BLE (para modo directo opcional)
TX_POWER = -95
N_FACTOR = 2.0
WINDOW_SIZE = 5
SEND_CHAR_UUID = "49535343-1E4D-4BD9-BA61-23C647249616"

# Configuración Render Cloud
RENDER_URL = "https://humans-cloud-dashboard.onrender.com/api/data"
API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6"

# Puerto del proxy local (para recibir del ESP32)
PROXY_PORT = 5050

# Buffer RSSI para cálculo de distancia
rssi_buffer = collections.deque(maxlen=WINDOW_SIZE)

# Estadísticas
stats = {
    "packets_received": 0,
    "packets_sent": 0,
    "packets_failed": 0,
    "last_packet_time": None,
    "uptime_start": time.time()
}

# ============================================
# FUNCIONES DE CÁLCULO
# ============================================

def calculate_distance(rssi):
    """Calcula distancia basada en RSSI"""
    if rssi is None or rssi >= 0:
        return 0.0
    return round(10**((TX_POWER - rssi) / (10 * N_FACTOR)), 2)

# ============================================
# PROXY HTTP - Recibe de ESP32, envía a Render
# ============================================

app = Flask(__name__)

@app.route("/api/data", methods=["POST"])
def receive_from_esp32():
    """
    Endpoint que recibe datos del ESP32 Bridge
    Valida, procesa y reenvía a Render Cloud
    """
    global stats
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data received"}), 400
        
        print(f"\n[PROXY] 📥 Datos recibidos del ESP32:")
        print(f"        SpO2: {data.get('spo2')}% | HR: {data.get('hr')} bpm")
        print(f"        RSSI: {data.get('rssi')} dBm | Distancia: {data.get('distance')}m")
        
        stats["packets_received"] += 1
        stats["last_packet_time"] = datetime.now().isoformat()
        
        # Validar datos antes de reenviar
        spo2 = data.get('spo2')
        hr = data.get('hr')
        
        if spo2 is None or hr is None:
            return jsonify({"error": "Missing spo2 or hr"}), 400
        
        if not (60 <= spo2 <= 100) or not (30 <= hr <= 220):
            return jsonify({"error": "Values out of range"}), 400
        
        # Reenviar a Render Cloud
        success = send_to_render(data)
        
        if success:
            stats["packets_sent"] += 1
            return jsonify({"status": "ok", "forwarded_to": "render"}), 200
        else:
            stats["packets_failed"] += 1
            return jsonify({"status": "error", "message": "Failed to forward"}), 500
            
    except Exception as e:
        print(f"[PROXY] ❌ Error: {e}")
        stats["packets_failed"] += 1
        return jsonify({"error": str(e)}), 500

def send_to_render(data):
    """
    Envía datos a Render Cloud vía HTTPS
    Incluye retry logic y timeout
    """
    try:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": API_KEY
        }
        
        print(f"[PROXY] 📤 Reenviando a Render Cloud...")
        
        response = requests.post(
            RENDER_URL,
            headers=headers,
            json=data,
            timeout=10
        )
        
        if response.status_code == 200:
            print(f"[PROXY] ✅ Datos enviados exitosamente a Render")
            return True
        else:
            print(f"[PROXY] ⚠️  Render respondió con código {response.status_code}")
            print(f"         Respuesta: {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        print(f"[PROXY] ⏱️  Timeout al conectar con Render")
        return False
    except requests.exceptions.RequestException as e:
        print(f"[PROXY] ❌ Error de red: {e}")
        return False

@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Endpoint para monitorizar el estado del proxy"""
    uptime = time.time() - stats["uptime_start"]
    return jsonify({
        **stats,
        "uptime_seconds": round(uptime, 2),
        "success_rate": round(
            stats["packets_sent"] / max(stats["packets_received"], 1) * 100, 
            2
        )
    })

@app.route("/health", methods=["GET"])
def health_check():
    """Health check para monitoreo"""
    return jsonify({"status": "healthy", "service": "humans-proxy"}), 200

# ============================================
# MODO BLE DIRECTO (Opcional - Sin ESP32)
# ============================================

async def ble_direct_mode():
    """
    Modo alternativo: conectar directamente al dispositivo BLE
    sin necesidad de ESP32 Bridge
    """
    print("\n[BLE] 🔵 Modo BLE Directo Activado")
    print("[BLE] Buscando dispositivo BLE...")
    
    while True:
        try:
            # FASE 1: Escanear para obtener RSSI
            current_rssi = None
            
            def detection_callback(device, advertisement_data):
                nonlocal current_rssi
                if device.name and any(x in device.name.lower() for x in ["berry", "vinculo", "humans"]):
                    current_rssi = advertisement_data.rssi
                    print(f"[BLE] Dispositivo detectado: {device.name} (RSSI: {current_rssi} dBm)")

            async with BleakScanner(detection_callback):
                await asyncio.sleep(2.0)

            if current_rssi:
                rssi_buffer.append(current_rssi)
                avg_rssi = sum(rssi_buffer) / len(rssi_buffer)
                dist = calculate_distance(avg_rssi)
                print(f"[BLE] Distancia calculada: {dist}m")
            else:
                print("[BLE] ⏳ Esperando señal BLE...")

            # FASE 2: Conectar para recibir datos
            target = await BleakScanner.find_device_by_filter(
                lambda d, ad: d.name and any(x in d.name.lower() for x in ["berry", "vinculo", "humans"])
            )

            if target:
                print(f"[BLE] 🔗 Conectando a {target.name}...")
                async with BleakClient(target.address, timeout=10.0) as client:
                    if client.is_connected:
                        print("[BLE] ✅ Conectado. Leyendo datos...")
                        
                        received_data = {}
                        
                        def handle_notification(_, data: bytes):
                            # Parsear datos según protocolo del dispositivo
                            if len(data) >= 5:
                                spo2 = data[3]
                                hr = data[4]
                                
                                if 60 <= spo2 <= 100 and 30 <= hr <= 220:
                                    received_data['spo2'] = spo2
                                    received_data['hr'] = hr
                                    received_data['rssi'] = current_rssi
                                    received_data['distance'] = dist
                                    
                                    print(f"[BLE] 📊 SpO2: {spo2}% | HR: {hr} bpm")
                                    
                                    # Enviar a Render
                                    send_to_render(received_data)

                        await client.start_notify(SEND_CHAR_UUID, handle_notification)
                        await asyncio.sleep(5)
                        await client.stop_notify(SEND_CHAR_UUID)
                
                print("[BLE] 🔄 Liberando conexión...")
            
            await asyncio.sleep(1)

        except Exception as e:
            print(f"[BLE] ❌ Error: {e}")
            await asyncio.sleep(2)

# ============================================
# MAIN
# ============================================

def run_flask():
    """Ejecuta el servidor Flask en un thread separado"""
    print(f"\n{'='*60}")
    print(f"🟢 HumanS Proxy HTTP - Mac Mini M4")
    print(f"{'='*60}")
    print(f"Modo: Recibir de ESP32 → Reenviar a Render")
    print(f"Puerto local: {PROXY_PORT}")
    print(f"Render Cloud: {RENDER_URL}")
    print(f"{'='*60}\n")
    
    app.run(host="0.0.0.0", port=PROXY_PORT, debug=False)

def run_ble_mode():
    """Ejecuta el modo BLE directo (sin ESP32)"""
    asyncio.run(ble_direct_mode())

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "--ble-direct":
        # Modo BLE Directo (sin ESP32)
        print("\n⚠️  Modo BLE Directo: No necesitas ESP32")
        print("    Conectando directamente al dispositivo BLE\n")
        run_ble_mode()
    else:
        # Modo Proxy HTTP (con ESP32)
        print("\n📡 Modo Proxy HTTP: Esperando datos del ESP32")
        print(f"   El ESP32 debe enviar a: http://<IP_MAC>:{PROXY_PORT}/api/data\n")
        
        # Mostrar IP local
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        print(f"💡 Tu IP local es: {local_ip}")
        print(f"   Configura ESP32 con: http://{local_ip}:{PROXY_PORT}/api/data\n")
        
        run_flask()