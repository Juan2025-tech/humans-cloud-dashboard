#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║                    HumanS - Proxy HTTP Multi-Bridge v5.0                         ║
║                 Sistema de Comunicación Triangular Optimizado                    ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║  • Coordinación inteligente de múltiples bridges                                 ║
║  • Handoff suave basado en RSSI y tendencias                                     ║
║  • Deduplicación y caché inteligente                                             ║
║  • API REST completa + Dashboard en tiempo real                                  ║
║  • Reenvío a Render Cloud con retry y buffering                                  ║
╚══════════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import collections
import requests
import json
import time
import threading
import statistics
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, List, Any
from flask import Flask, request, jsonify, Response
from threading import Lock, Thread
import logging
from queue import Queue, Empty
import signal
import sys

# ══════════════════════════════════════════════════════════════════════════════════
#                              CONFIGURACIÓN DE LOGGING
# ══════════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════════
#                                 CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════════

# Configuración Render Cloud
RENDER_URL = "https://humans-cloud-dashboard.onrender.com/api/data"
API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6"

# Puerto del proxy local
PROXY_PORT = 5050

# Configuración de handoff
HANDOFF_THRESHOLD_DB = 8        # Diferencia mínima de RSSI para handoff (dB)
HANDOFF_HYSTERESIS_TIME = 3.0   # Segundos mínimos antes de cambiar de bridge
BRIDGE_TIMEOUT_SECONDS = 10     # Timeout para considerar un bridge inactivo

# Configuración de datos - AJUSTADO para menos restricción
DUPLICATE_TOLERANCE_SPO2 = 0    # Solo rechazar si es EXACTAMENTE igual
DUPLICATE_TOLERANCE_HR = 0      # Solo rechazar si es EXACTAMENTE igual
DUPLICATE_WINDOW_SIZE = 2       # Solo comparar con últimos 2 registros

# Retry para Render
RENDER_MAX_RETRIES = 3
RENDER_RETRY_DELAY = 1.0        # Segundos entre reintentos

# ══════════════════════════════════════════════════════════════════════════════════
#                              ESTRUCTURAS DE DATOS
# ══════════════════════════════════════════════════════════════════════════════════

@dataclass
class BridgeInfo:
    """Información de un bridge ESP32"""
    bridge_id: str
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    
    # Estadísticas de señal
    current_rssi: int = -100
    avg_rssi: int = -100
    rssi_history: List[int] = field(default_factory=list)
    signal_quality: str = "DESCONOCIDO"
    signal_trend: str = "stable"
    distance: float = 0.0
    
    # Estadísticas de paquetes
    packets_received: int = 0
    packets_forwarded: int = 0
    packets_rejected: int = 0
    ble_packets: int = 0
    
    # Estado
    is_connected: bool = False
    is_active: bool = False
    connection_time: int = 0
    device_mac: str = ""
    
    def update_rssi(self, rssi: int):
        """Actualiza RSSI y mantiene historial"""
        self.rssi_history.append(rssi)
        if len(self.rssi_history) > 20:
            self.rssi_history = self.rssi_history[-20:]
        
        self.current_rssi = rssi
        
        # Calcular promedio ponderado (recientes pesan más)
        if len(self.rssi_history) >= 3:
            weights = [1 + i * 0.5 for i in range(len(self.rssi_history))]
            self.avg_rssi = int(sum(r * w for r, w in zip(self.rssi_history, weights)) / sum(weights))
        else:
            self.avg_rssi = rssi
    
    def get_trend_score(self) -> float:
        """Calcula score de tendencia (-1 a +1)"""
        if len(self.rssi_history) < 5:
            return 0.0
        
        recent = statistics.mean(self.rssi_history[-3:])
        older = statistics.mean(self.rssi_history[-6:-3])
        diff = recent - older
        
        # Normalizar a -1 a +1
        return max(-1, min(1, diff / 10))
    
    def to_dict(self) -> dict:
        """Convierte a diccionario para JSON"""
        return {
            "bridge_id": self.bridge_id,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "uptime": str(datetime.now() - self.first_seen).split('.')[0],
            "current_rssi": self.current_rssi,
            "avg_rssi": self.avg_rssi,
            "signal_quality": self.signal_quality,
            "signal_trend": self.signal_trend,
            "trend_score": round(self.get_trend_score(), 2),
            "distance": round(self.distance, 2),
            "packets_received": self.packets_received,
            "packets_forwarded": self.packets_forwarded,
            "packets_rejected": self.packets_rejected,
            "ble_packets": self.ble_packets,
            "is_connected": self.is_connected,
            "is_active": self.is_active,
            "connection_time": self.connection_time,
            "device_mac": self.device_mac
        }


@dataclass
class HealthData:
    """Datos de salud recibidos"""
    spo2: int
    hr: int
    rssi: int
    distance: float
    bridge_id: str
    timestamp: datetime = field(default_factory=datetime.now)
    forwarded: bool = False
    
    def to_dict(self) -> dict:
        return {
            "spo2": self.spo2,
            "hr": self.hr,
            "rssi": self.rssi,
            "distance": self.distance,
            "bridge_id": self.bridge_id,
            "timestamp": self.timestamp.isoformat(),
            "forwarded": self.forwarded
        }


# ══════════════════════════════════════════════════════════════════════════════════
#                           GESTOR DE BRIDGES
# ══════════════════════════════════════════════════════════════════════════════════

class BridgeManager:
    """Gestiona múltiples bridges y coordina handoffs"""
    
    def __init__(self):
        self.bridges: Dict[str, BridgeInfo] = {}
        self.lock = Lock()
        self.active_bridge_id: Optional[str] = None
        self.last_handoff_time: datetime = datetime.now()
        self.handoff_candidate: Optional[str] = None
        self.handoff_candidate_since: Optional[datetime] = None
    
    def update_bridge(self, bridge_id: str, data: dict) -> BridgeInfo:
        """Actualiza información de un bridge"""
        with self.lock:
            now = datetime.now()
            
            if bridge_id not in self.bridges:
                self.bridges[bridge_id] = BridgeInfo(bridge_id=bridge_id)
                logger.info(f"🆕 Nuevo bridge registrado: {bridge_id}")
            
            bridge = self.bridges[bridge_id]
            bridge.last_seen = now
            bridge.packets_received += 1
            
            # Actualizar datos de señal
            rssi = data.get('rssi', -100)
            bridge.update_rssi(rssi)
            bridge.signal_quality = data.get('signal_quality', 'DESCONOCIDO')
            bridge.signal_trend = data.get('signal_trend', 'stable')
            bridge.distance = data.get('distance', 0.0)
            
            # Actualizar estado de conexión
            bridge.is_connected = data.get('is_connected', False)
            bridge.connection_time = data.get('connection_time', 0)
            bridge.ble_packets = data.get('ble_packets', 0)
            bridge.device_mac = data.get('device_mac', '')
            
            # Evaluar handoff
            self._evaluate_handoff()
            
            # Marcar si es el bridge activo
            bridge.is_active = (bridge_id == self.active_bridge_id)
            
            return bridge
    
    def _evaluate_handoff(self):
        """Evalúa si debe ocurrir un handoff"""
        now = datetime.now()
        
        # Filtrar bridges activos (datos recientes)
        timeout = timedelta(seconds=BRIDGE_TIMEOUT_SECONDS)
        active_bridges = {
            bid: b for bid, b in self.bridges.items()
            if now - b.last_seen < timeout and b.is_connected
        }
        
        if not active_bridges:
            # No hay bridges activos conectados
            if self.active_bridge_id:
                logger.warning(f"⚠️ Bridge activo {self.active_bridge_id} sin datos - buscando alternativa")
            self.active_bridge_id = None
            self.handoff_candidate = None
            return
        
        # Si no hay bridge activo, seleccionar el mejor
        if self.active_bridge_id is None or self.active_bridge_id not in active_bridges:
            best_id = max(active_bridges.keys(), key=lambda x: active_bridges[x].avg_rssi)
            self.active_bridge_id = best_id
            self.bridges[best_id].is_active = True
            logger.info(f"✅ Bridge activo seleccionado: {best_id} (RSSI: {active_bridges[best_id].avg_rssi} dBm)")
            return
        
        current_bridge = active_bridges.get(self.active_bridge_id)
        if not current_bridge:
            return
        
        # Buscar mejor candidato
        best_candidate_id = None
        best_candidate_rssi = current_bridge.avg_rssi
        
        for bid, bridge in active_bridges.items():
            if bid == self.active_bridge_id:
                continue
            
            # Considerar RSSI y tendencia
            effective_rssi = bridge.avg_rssi + (bridge.get_trend_score() * 5)
            current_effective = current_bridge.avg_rssi + (current_bridge.get_trend_score() * 5)
            
            # El candidato debe ser significativamente mejor
            if effective_rssi > current_effective + HANDOFF_THRESHOLD_DB:
                if best_candidate_id is None or effective_rssi > best_candidate_rssi:
                    best_candidate_id = bid
                    best_candidate_rssi = effective_rssi
        
        # Gestión de candidato de handoff con histéresis temporal
        if best_candidate_id:
            if self.handoff_candidate == best_candidate_id:
                # Ya era candidato, verificar tiempo
                if self.handoff_candidate_since and \
                   (now - self.handoff_candidate_since).total_seconds() >= HANDOFF_HYSTERESIS_TIME:
                    # Ejecutar handoff
                    old_bridge = self.active_bridge_id
                    self.active_bridge_id = best_candidate_id
                    
                    # Actualizar estados
                    if old_bridge in self.bridges:
                        self.bridges[old_bridge].is_active = False
                    self.bridges[best_candidate_id].is_active = True
                    
                    logger.info(f"🔄 HANDOFF: {old_bridge} → {best_candidate_id} "
                               f"(RSSI: {self.bridges[old_bridge].avg_rssi if old_bridge in self.bridges else '?'} → "
                               f"{self.bridges[best_candidate_id].avg_rssi} dBm)")
                    
                    self.last_handoff_time = now
                    self.handoff_candidate = None
                    self.handoff_candidate_since = None
            else:
                # Nuevo candidato
                self.handoff_candidate = best_candidate_id
                self.handoff_candidate_since = now
                logger.debug(f"🔍 Candidato handoff: {best_candidate_id} "
                           f"(RSSI: {self.bridges[best_candidate_id].avg_rssi} dBm)")
        else:
            # No hay candidato mejor
            self.handoff_candidate = None
            self.handoff_candidate_since = None
    
    def should_bridge_connect(self, bridge_id: str) -> bool:
        """Determina si un bridge debe intentar conectar al dispositivo BLE"""
        with self.lock:
            # Si no hay bridge activo, todos pueden intentar
            if self.active_bridge_id is None:
                return True
            
            # Si este es el bridge activo, puede conectar
            if bridge_id == self.active_bridge_id:
                return True
            
            # Si este bridge tiene señal significativamente mejor, puede conectar
            if bridge_id in self.bridges and self.active_bridge_id in self.bridges:
                candidate = self.bridges[bridge_id]
                active = self.bridges[self.active_bridge_id]
                
                # Permitir si tiene mejor señal + threshold
                if candidate.avg_rssi > active.avg_rssi + HANDOFF_THRESHOLD_DB:
                    return True
            
            return False
    
    def should_bridge_release(self, bridge_id: str) -> bool:
        """Determina si un bridge debe soltar su conexión"""
        with self.lock:
            # Si no es el bridge activo y hay un activo mejor, soltar
            if self.active_bridge_id and bridge_id != self.active_bridge_id:
                if bridge_id in self.bridges and self.active_bridge_id in self.bridges:
                    this_bridge = self.bridges[bridge_id]
                    active_bridge = self.bridges[self.active_bridge_id]
                    
                    # Soltar si el activo tiene mejor señal
                    if active_bridge.avg_rssi > this_bridge.avg_rssi + 3:
                        return True
            
            return False
    
    def get_active_bridge(self) -> Optional[str]:
        """Retorna el ID del bridge activo"""
        with self.lock:
            return self.active_bridge_id
    
    def get_bridge_info(self, bridge_id: str) -> Optional[BridgeInfo]:
        """Obtiene información de un bridge"""
        with self.lock:
            return self.bridges.get(bridge_id)
    
    def get_all_bridges(self) -> Dict[str, dict]:
        """Retorna información de todos los bridges"""
        with self.lock:
            return {bid: b.to_dict() for bid, b in self.bridges.items()}
    
    def get_status_summary(self) -> dict:
        """Resumen de estado para dashboard"""
        with self.lock:
            now = datetime.now()
            timeout = timedelta(seconds=BRIDGE_TIMEOUT_SECONDS)
            
            online_bridges = [
                bid for bid, b in self.bridges.items()
                if now - b.last_seen < timeout
            ]
            
            return {
                "active_bridge": self.active_bridge_id,
                "total_bridges": len(self.bridges),
                "online_bridges": len(online_bridges),
                "handoff_candidate": self.handoff_candidate,
                "bridges": {bid: b.to_dict() for bid, b in self.bridges.items()}
            }


# ══════════════════════════════════════════════════════════════════════════════════
#                              CACHÉ DE DATOS
# ══════════════════════════════════════════════════════════════════════════════════

class DataCache:
    """Caché para deduplicación y almacenamiento de datos"""
    
    def __init__(self, max_size: int = 200):
        self.cache: collections.deque = collections.deque(maxlen=max_size)
        self.lock = Lock()
        self.last_sent: Optional[HealthData] = None
    
    def is_duplicate(self, spo2: int, hr: int) -> bool:
        """Verifica si los datos son duplicados de los últimos registros"""
        with self.lock:
            # Verificar últimos N registros
            recent = list(self.cache)[-DUPLICATE_WINDOW_SIZE:]
            
            for cached in recent:
                if (abs(cached.spo2 - spo2) <= DUPLICATE_TOLERANCE_SPO2 and
                    abs(cached.hr - hr) <= DUPLICATE_TOLERANCE_HR):
                    return True
            
            return False
    
    def add(self, data: HealthData):
        """Añade datos al caché"""
        with self.lock:
            self.cache.append(data)
            if data.forwarded:
                self.last_sent = data
    
    def get_recent(self, count: int = 20) -> List[dict]:
        """Obtiene los últimos N registros"""
        with self.lock:
            return [d.to_dict() for d in list(self.cache)[-count:]]
    
    def get_stats(self) -> dict:
        """Estadísticas del caché"""
        with self.lock:
            if not self.cache:
                return {"count": 0, "avg_spo2": 0, "avg_hr": 0}
            
            recent = list(self.cache)[-20:]
            return {
                "count": len(self.cache),
                "avg_spo2": round(statistics.mean(d.spo2 for d in recent), 1),
                "avg_hr": round(statistics.mean(d.hr for d in recent), 1),
                "last_timestamp": self.cache[-1].timestamp.isoformat() if self.cache else None
            }


# ══════════════════════════════════════════════════════════════════════════════════
#                           ENVIADOR A RENDER
# ══════════════════════════════════════════════════════════════════════════════════

class RenderSender:
    """Envía datos a Render Cloud con retry y buffering"""
    
    def __init__(self):
        self.queue: Queue = Queue(maxsize=100)
        self.stats = {
            "sent": 0,
            "failed": 0,
            "retries": 0,
            "last_success": None,
            "last_error": None
        }
        self.lock = Lock()
        self.running = True
        
        # Thread de envío
        self.sender_thread = Thread(target=self._sender_loop, daemon=True)
        self.sender_thread.start()
    
    def send(self, data: dict) -> bool:
        """Encola datos para envío"""
        try:
            self.queue.put_nowait(data)
            return True
        except:
            return False
    
    def _sender_loop(self):
        """Loop de envío en background"""
        while self.running:
            try:
                data = self.queue.get(timeout=1.0)
                self._send_with_retry(data)
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error en sender loop: {e}")
    
    def _send_with_retry(self, data: dict) -> bool:
        """Envía con reintentos"""
        headers = {
            "Content-Type": "application/json",
            "x-api-key": API_KEY
        }
        
        for attempt in range(RENDER_MAX_RETRIES):
            try:
                response = requests.post(
                    RENDER_URL,
                    headers=headers,
                    json=data,
                    timeout=5  # Reducido de 10 a 5 segundos
                )
                
                if response.status_code == 200:
                    with self.lock:
                        self.stats["sent"] += 1
                        self.stats["last_success"] = datetime.now().isoformat()
                    logger.info(f"📤 → Render OK (SpO2:{data.get('spo2')}% HR:{data.get('hr')})")
                    return True
                else:
                    logger.warning(f"Render respondió {response.status_code}")
                    
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout al enviar a Render (intento {attempt + 1})")
            except requests.exceptions.RequestException as e:
                logger.error(f"Error de red con Render: {e}")
            
            with self.lock:
                self.stats["retries"] += 1
            
            if attempt < RENDER_MAX_RETRIES - 1:
                time.sleep(RENDER_RETRY_DELAY * (attempt + 1))
        
        with self.lock:
            self.stats["failed"] += 1
            self.stats["last_error"] = datetime.now().isoformat()
        
        return False
    
    def get_stats(self) -> dict:
        with self.lock:
            return dict(self.stats)
    
    def shutdown(self):
        self.running = False


# ══════════════════════════════════════════════════════════════════════════════════
#                            INSTANCIAS GLOBALES
# ══════════════════════════════════════════════════════════════════════════════════

bridge_manager = BridgeManager()
data_cache = DataCache()
render_sender = RenderSender()

# Estadísticas globales
stats = {
    "total_packets_received": 0,
    "total_packets_forwarded": 0,
    "total_packets_rejected": 0,
    "total_duplicates": 0,
    "total_handoffs": 0,
    "uptime_start": time.time(),
    "last_packet_time": None
}

stats_lock = Lock()

# ══════════════════════════════════════════════════════════════════════════════════
#                              APLICACIÓN FLASK
# ══════════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

@app.route("/api/data", methods=["POST"])
def receive_data():
    """
    Endpoint principal: recibe datos de los ESP32 Bridges
    """
    global stats
    
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data"}), 400
        
        bridge_id = data.get('device', 'UNKNOWN')
        spo2 = data.get('spo2')
        hr = data.get('hr')
        rssi = data.get('rssi', -100)
        
        # Actualizar estadísticas
        with stats_lock:
            stats["total_packets_received"] += 1
            stats["last_packet_time"] = datetime.now().isoformat()
        
        # Actualizar bridge manager
        bridge_info = bridge_manager.update_bridge(bridge_id, data)
        active_bridge = bridge_manager.get_active_bridge()
        is_active = (bridge_id == active_bridge)
        
        # Validar datos
        if spo2 is None or hr is None:
            return jsonify({"error": "Missing spo2 or hr"}), 400
        
        if not (60 <= spo2 <= 100):
            return jsonify({"error": f"SpO2 out of range: {spo2}"}), 400
        
        if not (30 <= hr <= 220):
            return jsonify({"error": f"HR out of range: {hr}"}), 400
        
        # Log
        status_char = "★" if is_active else " "
        logger.info(
            f"📥 [{bridge_id}]{status_char} SpO2:{spo2}% HR:{hr} "
            f"RSSI:{rssi}dBm ({data.get('signal_quality', '?')}) "
            f"Dist:{data.get('distance', 0):.1f}m"
        )
        
        # Verificar duplicados
        if data_cache.is_duplicate(spo2, hr):
            with stats_lock:
                stats["total_duplicates"] += 1
            
            return jsonify({
                "status": "duplicate",
                "is_active_bridge": is_active,
                "active_bridge": active_bridge,
                "should_connect": bridge_manager.should_bridge_connect(bridge_id),
                "release_connection": bridge_manager.should_bridge_release(bridge_id)
            }), 200
        
        # Crear registro de datos
        health_data = HealthData(
            spo2=spo2,
            hr=hr,
            rssi=rssi,
            distance=data.get('distance', 0.0),
            bridge_id=bridge_id
        )
        
        # Solo reenviar si es del bridge activo
        if is_active:
            # Preparar datos para Render
            render_data = {
                "spo2": spo2,
                "hr": hr,
                "rssi": rssi,
                "distance": data.get('distance', 0.0),
                "bridge_id": bridge_id,
                "signal_quality": data.get('signal_quality', 'UNKNOWN'),
                "device_mac": data.get('device_mac', ''),
                "timestamp": datetime.now().isoformat()
            }
            
            success = render_sender.send(render_data)
            
            if success:
                health_data.forwarded = True
                bridge_info.packets_forwarded += 1
                
                with stats_lock:
                    stats["total_packets_forwarded"] += 1
                
                logger.info(f"✅ [{bridge_id}] → Render Cloud")
            else:
                logger.warning(f"⚠️ [{bridge_id}] Cola de Render llena")
        else:
            bridge_info.packets_rejected += 1
            with stats_lock:
                stats["total_packets_rejected"] += 1
        
        # Guardar en caché
        data_cache.add(health_data)
        
        return jsonify({
            "status": "ok",
            "forwarded": health_data.forwarded,
            "is_active_bridge": is_active,
            "active_bridge": active_bridge,
            "should_connect": bridge_manager.should_bridge_connect(bridge_id),
            "release_connection": bridge_manager.should_bridge_release(bridge_id)
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Error procesando datos: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/bridge/status", methods=["GET"])
def get_bridge_status():
    """
    Endpoint de consulta de estado para bridges
    Les indica si deben conectar o liberar conexión
    """
    bridge_id = request.args.get('bridge_id', '')
    rssi = request.args.get('rssi', -100, type=int)
    connected = request.args.get('connected', '0') == '1'
    
    if not bridge_id:
        return jsonify({"error": "Missing bridge_id"}), 400
    
    # Actualizar info básica del bridge (sin datos de salud)
    bridge_manager.update_bridge(bridge_id, {
        'rssi': rssi,
        'is_connected': connected
    })
    
    return jsonify({
        "bridge_id": bridge_id,
        "should_connect": bridge_manager.should_bridge_connect(bridge_id),
        "is_active_bridge": bridge_id == bridge_manager.get_active_bridge(),
        "active_bridge": bridge_manager.get_active_bridge(),
        "release_connection": bridge_manager.should_bridge_release(bridge_id)
    })


@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Estadísticas generales del proxy"""
    with stats_lock:
        uptime = time.time() - stats["uptime_start"]
        
        return jsonify({
            **stats,
            "uptime_seconds": round(uptime, 2),
            "uptime_formatted": str(timedelta(seconds=int(uptime))),
            "success_rate": round(
                stats["total_packets_forwarded"] / max(stats["total_packets_received"], 1) * 100,
                2
            ),
            "bridge_summary": bridge_manager.get_status_summary(),
            "render_stats": render_sender.get_stats(),
            "cache_stats": data_cache.get_stats()
        })


@app.route("/api/bridges", methods=["GET"])
def get_bridges():
    """Información detallada de todos los bridges"""
    return jsonify(bridge_manager.get_status_summary())


@app.route("/api/recent", methods=["GET"])
def get_recent_data():
    """Últimos datos recibidos"""
    count = request.args.get('count', 20, type=int)
    return jsonify({
        "data": data_cache.get_recent(count),
        "cache_stats": data_cache.get_stats()
    })


@app.route("/health", methods=["GET"])
def health_check():
    """Health check"""
    return jsonify({
        "status": "healthy",
        "service": "humans-proxy-multi-bridge",
        "version": "5.0",
        "bridges_online": len([
            b for b in bridge_manager.bridges.values()
            if (datetime.now() - b.last_seen).total_seconds() < BRIDGE_TIMEOUT_SECONDS
        ]),
        "active_bridge": bridge_manager.get_active_bridge()
    }), 200


@app.route("/", methods=["GET"])
def dashboard():
    """Dashboard HTML en tiempo real"""
    bridges = bridge_manager.get_all_bridges()
    active = bridge_manager.get_active_bridge()
    uptime = str(timedelta(seconds=int(time.time() - stats["uptime_start"]))).split('.')[0]
    
    with stats_lock:
        local_stats = dict(stats)
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>HumanS Proxy Dashboard v5.0</title>
        <meta http-equiv="refresh" content="3">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            :root {{
                --bg-dark: #0a0a0f;
                --bg-card: #12121a;
                --bg-card-active: #1a1a2e;
                --accent-primary: #00d4aa;
                --accent-secondary: #7c3aed;
                --accent-warning: #f59e0b;
                --accent-danger: #ef4444;
                --text-primary: #e2e8f0;
                --text-secondary: #94a3b8;
                --border-color: #1e293b;
            }}
            
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            
            body {{
                font-family: 'SF Mono', 'Fira Code', monospace;
                background: var(--bg-dark);
                color: var(--text-primary);
                min-height: 100vh;
                padding: 20px;
            }}
            
            .container {{ max-width: 1400px; margin: 0 auto; }}
            
            .header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 20px;
                background: var(--bg-card);
                border-radius: 12px;
                border: 1px solid var(--border-color);
                margin-bottom: 24px;
            }}
            
            .header h1 {{
                font-size: 24px;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 12px;
            }}
            
            .header h1::before {{
                content: '';
                width: 12px;
                height: 12px;
                background: var(--accent-primary);
                border-radius: 50%;
                animation: pulse 2s infinite;
            }}
            
            @keyframes pulse {{
                0%, 100% {{ opacity: 1; transform: scale(1); }}
                50% {{ opacity: 0.5; transform: scale(1.2); }}
            }}
            
            .meta {{ color: var(--text-secondary); font-size: 14px; }}
            
            .stats-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                gap: 16px;
                margin-bottom: 24px;
            }}
            
            .stat-card {{
                background: var(--bg-card);
                padding: 20px;
                border-radius: 12px;
                border: 1px solid var(--border-color);
            }}
            
            .stat-label {{
                font-size: 12px;
                color: var(--text-secondary);
                text-transform: uppercase;
                letter-spacing: 0.5px;
                margin-bottom: 8px;
            }}
            
            .stat-value {{
                font-size: 32px;
                font-weight: 700;
                color: var(--accent-primary);
            }}
            
            .stat-value.warning {{ color: var(--accent-warning); }}
            .stat-value.danger {{ color: var(--accent-danger); }}
            
            .section-title {{
                font-size: 18px;
                font-weight: 600;
                margin-bottom: 16px;
                display: flex;
                align-items: center;
                gap: 8px;
            }}
            
            .bridges-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
                gap: 20px;
                margin-bottom: 24px;
            }}
            
            .bridge-card {{
                background: var(--bg-card);
                border-radius: 12px;
                border: 2px solid var(--border-color);
                overflow: hidden;
                transition: all 0.3s ease;
            }}
            
            .bridge-card.active {{
                border-color: var(--accent-primary);
                background: var(--bg-card-active);
                box-shadow: 0 0 30px rgba(0, 212, 170, 0.15);
            }}
            
            .bridge-header {{
                padding: 16px 20px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                border-bottom: 1px solid var(--border-color);
            }}
            
            .bridge-name {{
                font-size: 16px;
                font-weight: 600;
            }}
            
            .badge {{
                font-size: 11px;
                padding: 4px 10px;
                border-radius: 20px;
                font-weight: 600;
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }}
            
            .badge.active {{
                background: var(--accent-primary);
                color: var(--bg-dark);
            }}
            
            .badge.standby {{
                background: var(--border-color);
                color: var(--text-secondary);
            }}
            
            .badge.offline {{
                background: var(--accent-danger);
                color: white;
            }}
            
            .bridge-body {{ padding: 20px; }}
            
            .metric-row {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                padding: 8px 0;
                border-bottom: 1px solid var(--border-color);
            }}
            
            .metric-row:last-child {{ border-bottom: none; }}
            
            .metric-label {{ color: var(--text-secondary); font-size: 13px; }}
            
            .metric-value {{
                font-weight: 600;
                font-size: 14px;
            }}
            
            .signal-bar {{
                width: 100%;
                height: 8px;
                background: var(--border-color);
                border-radius: 4px;
                margin-top: 12px;
                overflow: hidden;
            }}
            
            .signal-fill {{
                height: 100%;
                border-radius: 4px;
                transition: width 0.5s ease;
            }}
            
            .signal-excellent {{ background: var(--accent-primary); }}
            .signal-good {{ background: #22c55e; }}
            .signal-acceptable {{ background: var(--accent-warning); }}
            .signal-weak {{ background: #f97316; }}
            .signal-critical {{ background: var(--accent-danger); }}
            
            .trend {{
                display: inline-block;
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 11px;
            }}
            
            .trend.improving {{ background: rgba(34, 197, 94, 0.2); color: #22c55e; }}
            .trend.stable {{ background: rgba(148, 163, 184, 0.2); color: var(--text-secondary); }}
            .trend.deteriorating {{ background: rgba(239, 68, 68, 0.2); color: var(--accent-danger); }}
            
            .footer {{
                text-align: center;
                padding: 20px;
                color: var(--text-secondary);
                font-size: 12px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <header class="header">
                <h1>HumanS Proxy v5.0</h1>
                <div class="meta">
                    Uptime: {uptime} | 
                    Bridges: {len(bridges)} | 
                    Activo: {active or 'NINGUNO'}
                </div>
            </header>
            
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Paquetes Recibidos</div>
                    <div class="stat-value">{local_stats['total_packets_received']}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Enviados a Render</div>
                    <div class="stat-value">{local_stats['total_packets_forwarded']}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Duplicados</div>
                    <div class="stat-value warning">{local_stats['total_duplicates']}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Tasa de Éxito</div>
                    <div class="stat-value">{round(local_stats['total_packets_forwarded'] / max(local_stats['total_packets_received'], 1) * 100, 1)}%</div>
                </div>
            </div>
            
            <h2 class="section-title">🌉 Bridges Activos</h2>
            <div class="bridges-grid">
    """
    
    for bridge_id, info in bridges.items():
        is_active = (bridge_id == active)
        is_offline = (datetime.now() - datetime.fromisoformat(info['last_seen'])).total_seconds() > BRIDGE_TIMEOUT_SECONDS
        
        # Determinar clase de tarjeta
        card_class = "active" if is_active else ""
        
        # Determinar badge
        if is_offline:
            badge = '<span class="badge offline">OFFLINE</span>'
        elif is_active:
            badge = '<span class="badge active">ACTIVO</span>'
        else:
            badge = '<span class="badge standby">STANDBY</span>'
        
        # Calcular porcentaje de señal
        rssi = info['current_rssi']
        signal_pct = max(0, min(100, (rssi + 100) * 2))  # -100 = 0%, -50 = 100%
        
        # Clase de señal
        if rssi >= -60:
            signal_class = "signal-excellent"
        elif rssi >= -70:
            signal_class = "signal-good"
        elif rssi >= -80:
            signal_class = "signal-acceptable"
        elif rssi >= -90:
            signal_class = "signal-weak"
        else:
            signal_class = "signal-critical"
        
        # Tendencia
        trend = info.get('signal_trend', 'stable')
        trend_html = f'<span class="trend {trend}">{"↗" if trend == "improving" else "↘" if trend == "deteriorating" else "→"} {trend}</span>'
        
        html += f"""
            <div class="bridge-card {card_class}">
                <div class="bridge-header">
                    <span class="bridge-name">{bridge_id}</span>
                    {badge}
                </div>
                <div class="bridge-body">
                    <div class="metric-row">
                        <span class="metric-label">RSSI</span>
                        <span class="metric-value">{rssi} dBm ({info['signal_quality']})</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">RSSI Promedio</span>
                        <span class="metric-value">{info['avg_rssi']} dBm</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Distancia</span>
                        <span class="metric-value">{info['distance']} m</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Tendencia</span>
                        <span class="metric-value">{trend_html}</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Paquetes</span>
                        <span class="metric-value">📥 {info['packets_received']} | 📤 {info['packets_forwarded']}</span>
                    </div>
                    <div class="metric-row">
                        <span class="metric-label">Último dato</span>
                        <span class="metric-value">{info['last_seen'].split('T')[1].split('.')[0]}</span>
                    </div>
                    
                    <div class="signal-bar">
                        <div class="signal-fill {signal_class}" style="width: {signal_pct}%"></div>
                    </div>
                </div>
            </div>
        """
    
    html += """
            </div>
            
            <footer class="footer">
                HumanS Health Monitoring System | Sistema Triangular de Comunicación
            </footer>
        </div>
    </body>
    </html>
    """
    
    return html


# ══════════════════════════════════════════════════════════════════════════════════
#                                    MAIN
# ══════════════════════════════════════════════════════════════════════════════════

def signal_handler(sig, frame):
    """Maneja señales de terminación"""
    logger.info("Shutting down...")
    render_sender.shutdown()
    sys.exit(0)


def main():
    """Punto de entrada principal"""
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 70)
    logger.info("🟢 HumanS Proxy HTTP Multi-Bridge v5.0")
    logger.info("   Sistema de Comunicación Triangular Optimizado")
    logger.info("=" * 70)
    logger.info(f"Puerto local: {PROXY_PORT}")
    logger.info(f"Render Cloud: {RENDER_URL}")
    logger.info(f"Handoff threshold: {HANDOFF_THRESHOLD_DB} dB")
    logger.info(f"Handoff hysteresis: {HANDOFF_HYSTERESIS_TIME}s")
    
    # Obtener IP local
    import socket
    try:
        hostname = socket.gethostname()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        
        logger.info(f"💡 IP local: {local_ip}")
        logger.info(f"   ESP32 Data URL: http://{local_ip}:{PROXY_PORT}/api/data")
        logger.info(f"   ESP32 Status URL: http://{local_ip}:{PROXY_PORT}/api/bridge/status")
        logger.info(f"   Dashboard: http://{local_ip}:{PROXY_PORT}")
    except Exception as e:
        logger.warning(f"⚠️ No se pudo determinar IP local: {e}")
    
    logger.info("=" * 70)
    logger.info("✅ Proxy iniciado - Esperando bridges...")
    
    app.run(host="0.0.0.0", port=PROXY_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
