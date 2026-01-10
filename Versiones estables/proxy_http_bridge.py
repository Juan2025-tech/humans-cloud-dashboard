#!/usr/bin/env python3
"""
Proxy HTTP→HTTPS para ESP32 Bridge
Recibe datos del ESP32 (HTTP) y los reenvía a Render (HTTPS)
"""

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# URL de tu backend HTTPS en Render
TARGET_URL = "https://humans-cloud-dashboard.onrender.com/api/data"
API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6"

@app.route("/api/data", methods=["POST"])
def bridge():
    """Recibe del ESP32 y reenvía a Render"""
    try:
        data = request.json
        
        if not data:
            print("⚠️  Petición vacía recibida")
            return jsonify({"error": "No JSON data received"}), 400

        print(f"📥 Recibido desde ESP32: SpO2={data.get('spo2')}, HR={data.get('hr')}")

        # Enviar a Render con headers
        headers = {
            "Content-Type": "application/json",
            "x-api-key": API_KEY
        }

        response = requests.post(
            TARGET_URL, 
            json=data, 
            headers=headers,
            timeout=10  # Timeout de 10 segundos
        )

        print(f"📤 Respuesta de Render: {response.status_code} | {response.text[:100]}")

        # Retornar la respuesta de Render al ESP32
        return jsonify({
            "proxy_status": "ok",
            "render_status": response.status_code,
            "render_response": response.json() if response.status_code == 200 else None
        }), response.status_code

    except requests.exceptions.Timeout:
        print("❌ Timeout conectando con Render")
        return jsonify({"error": "Render timeout"}), 504
    
    except requests.exceptions.ConnectionError:
        print("❌ Error de conexión con Render")
        return jsonify({"error": "Cannot connect to Render"}), 503
    
    except Exception as e:
        print(f"❌ Error en proxy: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    """Health check del proxy"""
    return jsonify({"status": "proxy_running", "target": TARGET_URL}), 200

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Proxy HTTP→HTTPS Bridge Iniciado")
    print("=" * 60)
    print(f"📍 Escuchando en: http://0.0.0.0:5050/api/data")
    print(f"🎯 Target Render: {TARGET_URL}")
    print(f"🔑 API Key configurada: {API_KEY[:8]}...{API_KEY[-4:]}")
    print("=" * 60)
    
    app.run(host="0.0.0.0", port=5050, debug=False)
