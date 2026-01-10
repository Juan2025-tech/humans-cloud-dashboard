# proxy_to_thingsboard.py
from flask import Flask, request, jsonify
import requests
import time
import os

app = Flask(__name__)

# CONFIGURACIÓN - Edita según tu entorno
THINGSBOARD_HOST = os.environ.get("THINGSBOARD_HOST", "demo.thingsboard.io")  # o tu IP
DEVICE_ACCESS_TOKEN = os.environ.get("THINGSBOARD_TOKEN", "xrE7pOHT8MXYMcMwsV1Q")
# El endpoint HTTP para telemetry
THINGSBOARD_URL = f"https://{THINGSBOARD_HOST}/api/v1/{DEVICE_ACCESS_TOKEN}/telemetry"

# Puerto donde escucha este proxy (local)
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", 5050))

@app.route("/api/data", methods=["POST"])
def bridge():
    try:
        data = request.get_json(force=True)
        print("📥 Recibido desde ESP32:", data)

        # Añade timestamp en ms (opcional)
        ts_ms = int(time.time() * 1000)
        # Puedes enviar como { "ts": <ms>, "values": {...} } o simplemente añadir timestamp en el payload.
        # Aquí enviaremos un payload plano con campo 'timestamp' para mayor compatibilidad con tu dashboard.
        payload = data.copy() if isinstance(data, dict) else {"value": data}
        payload["timestamp"] = ts_ms

        print("📤 Enviando a ThingsBoard:", payload)

        headers = {"Content-Type": "application/json"}

        # POST a ThingsBoard Telemetry API
        r = requests.post(THINGSBOARD_URL, json=payload, headers=headers, timeout=10)

        print("🔁 Resp ThingsBoard:", r.status_code, r.text)
        if r.status_code >= 200 and r.status_code < 300:
            return jsonify({"proxy_status": "ok", "thingsboard_status": r.status_code}), 200
        else:
            return jsonify({"proxy_status": "error", "thingsboard_status": r.status_code, "response": r.text}), 502

    except Exception as e:
        print("❌ Error en proxy:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    print(f"🚀 Proxy HTTP→ThingsBoard listo en http://{LISTEN_HOST}:{LISTEN_PORT}/api/data")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT)
