/*
 * HumanS ESP32 Smart Bridge v5.1 - ESCANEO BLE CORREGIDO
 * 
 * CAMBIOS vs v5.0:
 * - Escaneo BLE síncrono (más confiable)
 * - Debug detallado del escaneo
 * - Manejo mejorado de callbacks
 */

#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ═══════════════════════════════════════════════════════════════════════════════
// ║                         CONFIGURACIÓN PRINCIPAL                             ║
// ═══════════════════════════════════════════════════════════════════════════════

// ⚠️ CAMBIAR SOLO ESTO EN CADA ESP32 ⚠️
#define BRIDGE_ID "ESP32_LAB"

// Configuración WiFi
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";
//const char* WIFI_SSID = "MOVISTAR-WIFI6-6EF0_EXT";
//const char* WIFI_PASSWORD = "EVNgw3etaYWRWsa5P7uB";
//const char* WIFI_SSID = "iPhone de Walter";
//const char* WIFI_PASSWORD = "h3mQ-kEJQ-G19B-5NER";


// Configuración Proxy (Mac Mini o Mac Book)
const char* PROXY_URL = "http://192.168.1.50:5050/api/data";
const char* PROXY_STATUS_URL = "http://192.168.1.50:5050/api/bridge/status";
//const char* PROXY_URL = "http://172.20.10.9:5050/api/data";
//const char* PROXY_STATUS_URL = "http://172.20.10.9:5050/api/bridge/status";

// ═══════════════════════════════════════════════════════════════════════════════
// ║                        CONFIGURACIÓN BLE                                    ║
// ═══════════════════════════════════════════════════════════════════════════════

// Filtro de nombre del dispositivo HumanS (case insensitive)
#define DEVICE_NAME_FILTER "BerryMed"

// UUIDs del servicio BLE HumanS
static BLEUUID serviceUUID("49535343-FE7D-4AE5-8FA9-9FAFD205E455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

// ═══════════════════════════════════════════════════════════════════════════════
// ║                     PARÁMETROS DE SEÑAL                                     ║
// ═══════════════════════════════════════════════════════════════════════════════

const int TX_POWER = -95;
const float N_FACTOR = 2.0;

const int RSSI_EXCELLENT = -60;
const int RSSI_GOOD = -70;
const int RSSI_ACCEPTABLE = -80;
const int RSSI_WEAK = -88;
const int RSSI_CRITICAL = -94;

const int RSSI_MIN_CONNECT = -92;
const int RSSI_FORCE_DISCONNECT = -96;

// ═══════════════════════════════════════════════════════════════════════════════
// ║                          INTERVALOS DE TIEMPO                               ║
// ═══════════════════════════════════════════════════════════════════════════════

const int SCAN_DURATION = 5;                           // Duración escaneo (segundos)
const unsigned long SCAN_INTERVAL = 3000;              // Intervalo entre escaneos (ms)
const unsigned long RSSI_UPDATE_INTERVAL = 2000;       // Actualizar RSSI (ms)
const unsigned long CONNECTION_TIMEOUT = 8000;         // Timeout conexión (ms)
const unsigned long DATA_TIMEOUT = 15000;              // Timeout sin datos (ms)
const unsigned long POST_INTERVAL = 2000;              // Intervalo POST (ms)
const unsigned long STATUS_CHECK_INTERVAL = 3000;      // Check estado proxy (ms)
const unsigned long STATS_INTERVAL = 60000;            // Estadísticas (ms)

// ═══════════════════════════════════════════════════════════════════════════════
// ║                          VARIABLES GLOBALES                                 ║
// ═══════════════════════════════════════════════════════════════════════════════

BLEClient* pClient = nullptr;
BLERemoteCharacteristic* pRemoteChar = nullptr;
BLERemoteCharacteristic* pWriteChar = nullptr;
BLEScan* pBLEScan = nullptr;

// Estado
bool deviceConnected = false;
bool isConnecting = false;
bool shouldConnect = true;
bool isActiveBridge = false;

// Datos
int currentRSSI = 0;
int avgRSSI = 0;
float currentDistance = 0.0;
String deviceMAC = "";

int pendingSpo2 = -1;
int pendingHR = -1;

// Estadísticas
int packetsSent = 0;
int blePacketsReceived = 0;
int connectionAttempts = 0;
int reconnections = 0;
int consecutiveFailures = 0;

// Timestamps
unsigned long connectionStartTime = 0;
unsigned long lastDataTime = 0;
unsigned long lastRSSIUpdate = 0;
unsigned long lastScanTime = 0;
unsigned long lastPostTime = 0;
unsigned long lastStatusCheck = 0;

// Historial RSSI
const int RSSI_HISTORY_SIZE = 10;
int rssiHistory[RSSI_HISTORY_SIZE] = {0};
int rssiHistoryIndex = 0;
int rssiHistoryCount = 0;

// Dispositivo encontrado en escaneo
BLEAdvertisedDevice* foundDevice = nullptr;

// ═══════════════════════════════════════════════════════════════════════════════
// ║                       FUNCIONES DE UTILIDAD                                 ║
// ═══════════════════════════════════════════════════════════════════════════════

float calculateDistance(int rssi) {
  if (rssi >= 0 || rssi < -120) return 99.9;
  return constrain(pow(10.0, (TX_POWER - rssi) / (10.0 * N_FACTOR)), 0.1, 99.9);
}

const char* getSignalQuality(int rssi) {
  if (rssi >= RSSI_EXCELLENT) return "EXCELENTE";
  if (rssi >= RSSI_GOOD) return "BUENA";
  if (rssi >= RSSI_ACCEPTABLE) return "ACEPTABLE";
  if (rssi >= RSSI_WEAK) return "DEBIL";
  if (rssi >= RSSI_CRITICAL) return "CRITICA";
  return "PERDIDA";
}

void updateRSSIHistory(int rssi) {
  rssiHistory[rssiHistoryIndex] = rssi;
  rssiHistoryIndex = (rssiHistoryIndex + 1) % RSSI_HISTORY_SIZE;
  if (rssiHistoryCount < RSSI_HISTORY_SIZE) rssiHistoryCount++;
  
  // Calcular promedio
  int sum = 0;
  for (int i = 0; i < rssiHistoryCount; i++) {
    sum += rssiHistory[i];
  }
  avgRSSI = sum / rssiHistoryCount;
}

String getSignalTrend() {
  if (rssiHistoryCount < 4) return "stable";
  
  int recent = (rssiHistory[(rssiHistoryIndex - 1 + RSSI_HISTORY_SIZE) % RSSI_HISTORY_SIZE] +
                rssiHistory[(rssiHistoryIndex - 2 + RSSI_HISTORY_SIZE) % RSSI_HISTORY_SIZE]) / 2;
  int older = (rssiHistory[(rssiHistoryIndex - 3 + RSSI_HISTORY_SIZE) % RSSI_HISTORY_SIZE] +
               rssiHistory[(rssiHistoryIndex - 4 + RSSI_HISTORY_SIZE) % RSSI_HISTORY_SIZE]) / 2;
  
  if (recent > older + 3) return "improving";
  if (recent < older - 5) return "deteriorating";
  return "stable";
}

// ═══════════════════════════════════════════════════════════════════════════════
// ║                          CALLBACKS BLE                                      ║
// ═══════════════════════════════════════════════════════════════════════════════

// Callback de notificaciones BLE - RECIBE DATOS DE SALUD
void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic, 
                    uint8_t* pData, size_t length, bool isNotify) {
  
  Serial.printf("[BLE] 📦 Notificación recibida: %d bytes\n", length);
  
  if (length != 20) {
    Serial.printf("[BLE] ⚠️ Paquete inválido: %d bytes (esperado 20)\n", length);
    return;
  }
  
  // Debug: mostrar bytes raw
  Serial.print("[BLE] RAW: ");
  for (int i = 0; i < length; i++) {
    Serial.printf("%02X ", pData[i]);
  }
  Serial.println();
  
  // Extraer 4 muestras del paquete HumanS
  int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};
  
  int hr_sum = 0, hr_count = 0;
  int spo2_sum = 0, spo2_count = 0;
  
  for (int h : hr_samples) {
    if (h >= 30 && h <= 220) {
      hr_sum += h;
      hr_count++;
    }
  }
  
  for (int s : spo2_samples) {
    if (s >= 60 && s <= 100) {
      spo2_sum += s;
      spo2_count++;
    }
  }
  
  if (hr_count == 0 || spo2_count == 0) {
    Serial.println("[BLE] ⚠️ Datos fuera de rango");
    return;
  }
  
  pendingHR = hr_sum / hr_count;
  pendingSpo2 = spo2_sum / spo2_count;
  
  blePacketsReceived++;
  lastDataTime = millis();
  
  Serial.printf("[BLE] 💓 SpO2:%d%% HR:%d bpm | RSSI:%d (%s) | %.1fm\n",
                pendingSpo2, pendingHR,
                currentRSSI, getSignalQuality(currentRSSI),
                currentDistance);
}

// Callbacks de conexión BLE
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient) override {
    deviceConnected = true;
    isConnecting = false;
    consecutiveFailures = 0;
    connectionStartTime = millis();
    lastDataTime = millis();
    
    Serial.println("[BLE] ════════════════════════════════════════");
    Serial.printf("[BLE] ✅ CONECTADO EXITOSAMENTE\n");
    Serial.printf("[BLE]    Intentos: %d | Reconexiones: %d\n", 
                  connectionAttempts, reconnections);
    Serial.println("[BLE] ════════════════════════════════════════");
  }
  
  void onDisconnect(BLEClient* pclient) override {
    bool wasConnected = deviceConnected;
    deviceConnected = false;
    isConnecting = false;
    
    if (wasConnected) {
      reconnections++;
      Serial.println("[BLE] ════════════════════════════════════════");
      Serial.println("[BLE] 🔴 DESCONECTADO");
      Serial.println("[BLE] ════════════════════════════════════════");
    }
  }
};

// Callback de escaneo BLE
class MyScanCallback : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) override {
    String name = advertisedDevice.getName().c_str();
    
    // Mostrar TODOS los dispositivos BLE detectados
    if (name.length() > 0) {
      Serial.printf("[SCAN] 📡 Detectado: '%s' | RSSI: %d dBm\n", 
                    name.c_str(), advertisedDevice.getRSSI());
    }
    
    // Buscar coincidencia con filtro (case insensitive)
    String nameLower = name;
    nameLower.toLowerCase();
    
    String filter = DEVICE_NAME_FILTER;
    filter.toLowerCase();
    
    if (nameLower.indexOf(filter) != -1) {
      int rssi = advertisedDevice.getRSSI();
      
      Serial.println("[SCAN] ════════════════════════════════════════");
      Serial.printf("[SCAN] 🎯 ¡DISPOSITIVO HUMANS ENCONTRADO!\n");
      Serial.printf("[SCAN]    Nombre: %s\n", name.c_str());
      Serial.printf("[SCAN]    MAC: %s\n", advertisedDevice.getAddress().toString().c_str());
      Serial.printf("[SCAN]    RSSI: %d dBm (%s)\n", rssi, getSignalQuality(rssi));
      Serial.printf("[SCAN]    Distancia: %.1f m\n", calculateDistance(rssi));
      Serial.println("[SCAN] ════════════════════════════════════════");
      
      // Guardar dispositivo encontrado
      if (foundDevice != nullptr) {
        delete foundDevice;
      }
      foundDevice = new BLEAdvertisedDevice(advertisedDevice);
      deviceMAC = advertisedDevice.getAddress().toString().c_str();
      
      // Detener escaneo
      pBLEScan->stop();
    }
  }
};

// ═══════════════════════════════════════════════════════════════════════════════
// ║                        GESTIÓN DE CONEXIÓN                                  ║
// ═══════════════════════════════════════════════════════════════════════════════

void cleanupConnection() {
  Serial.println("[BLE] 🧹 Limpiando conexión anterior...");
  
  if (pClient != nullptr) {
    if (pClient->isConnected()) {
      pClient->disconnect();
      delay(100);
    }
    delete pClient;
    pClient = nullptr;
  }
  pRemoteChar = nullptr;
  pWriteChar = nullptr;
  deviceConnected = false;
  isConnecting = false;
}

void forceDisconnect(const char* reason) {
  Serial.printf("[BLE] 🔌 Desconexión forzada: %s\n", reason);
  cleanupConnection();
}

bool connectToDevice() {
  if (deviceConnected || isConnecting) {
    Serial.println("[BLE] ⚠️ Ya conectado o conectando");
    return false;
  }
  
  if (foundDevice == nullptr) {
    Serial.println("[BLE] ⚠️ No hay dispositivo para conectar");
    return false;
  }
  
  int rssi = foundDevice->getRSSI();
  if (rssi < RSSI_MIN_CONNECT) {
    Serial.printf("[BLE] ⛔ Señal muy débil: %d dBm (mínimo: %d)\n", rssi, RSSI_MIN_CONNECT);
    return false;
  }
  
  if (!shouldConnect) {
    Serial.println("[BLE] ⏸️ Proxy indica que no conectemos");
    return false;
  }
  
  isConnecting = true;
  connectionAttempts++;
  
  Serial.println("\n[BLE] ════════════════════════════════════════");
  Serial.printf("[BLE] 🔌 INTENTANDO CONEXIÓN #%d\n", connectionAttempts);
  Serial.printf("[BLE]    Dispositivo: %s\n", foundDevice->getName().c_str());
  Serial.printf("[BLE]    MAC: %s\n", foundDevice->getAddress().toString().c_str());
  Serial.printf("[BLE]    RSSI: %d dBm (%s)\n", rssi, getSignalQuality(rssi));
  Serial.println("[BLE] ════════════════════════════════════════");
  
  cleanupConnection();
  
  pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());
  
  Serial.println("[BLE] Conectando...");
  unsigned long startTime = millis();
  
  if (!pClient->connect(foundDevice)) {
    Serial.println("[BLE] ❌ Falló la conexión al dispositivo");
    isConnecting = false;
    consecutiveFailures++;
    cleanupConnection();
    return false;
  }
  
  if (millis() - startTime > CONNECTION_TIMEOUT) {
    Serial.println("[BLE] ⏱️ Timeout de conexión");
    consecutiveFailures++;
    cleanupConnection();
    return false;
  }
  
  Serial.println("[BLE] ✓ Conexión establecida, buscando servicio...");
  
  // Obtener servicio
  BLERemoteService* pService = pClient->getService(serviceUUID);
  if (!pService) {
    Serial.println("[BLE] ❌ Servicio BLE no encontrado");
    Serial.printf("[BLE]    UUID buscado: %s\n", serviceUUID.toString().c_str());
    consecutiveFailures++;
    cleanupConnection();
    return false;
  }
  Serial.println("[BLE] ✓ Servicio encontrado");
  
  // Obtener característica de notificación
  pRemoteChar = pService->getCharacteristic(charUUID_notify);
  if (!pRemoteChar) {
    Serial.println("[BLE] ❌ Característica de notificación no encontrada");
    consecutiveFailures++;
    cleanupConnection();
    return false;
  }
  Serial.println("[BLE] ✓ Característica de notificación encontrada");
  
  if (!pRemoteChar->canNotify()) {
    Serial.println("[BLE] ❌ La característica no soporta notificaciones");
    consecutiveFailures++;
    cleanupConnection();
    return false;
  }
  
  // Registrar para notificaciones
  pRemoteChar->registerForNotify(notifyCallback);
  Serial.println("[BLE] ✓ Registrado para notificaciones");
  
  // Obtener característica de escritura y enviar comando 0xF1
  pWriteChar = pService->getCharacteristic(charUUID_write);
  if (pWriteChar && pWriteChar->canWrite()) {
    uint8_t cmd[] = {0xF1};
    pWriteChar->writeValue(cmd, 1, true);
    Serial.println("[BLE] ✓ Comando 0xF1 enviado (activar streaming)");
  } else {
    Serial.println("[BLE] ⚠️ No se pudo enviar comando 0xF1");
  }
  
  currentRSSI = rssi;
  updateRSSIHistory(rssi);
  currentDistance = calculateDistance(rssi);
  lastRSSIUpdate = millis();
  
  Serial.println("[BLE] ════════════════════════════════════════");
  Serial.println("[BLE] ✅ CONEXIÓN COMPLETA - Esperando datos...");
  Serial.println("[BLE] ════════════════════════════════════════\n");
  
  return true;
}

// ═══════════════════════════════════════════════════════════════════════════════
// ║                        ESCANEO BLE                                          ║
// ═══════════════════════════════════════════════════════════════════════════════

void performScan() {
  if (deviceConnected || isConnecting) {
    return;
  }
  
  Serial.println("\n[SCAN] ════════════════════════════════════════");
  Serial.println("[SCAN] 🔍 INICIANDO ESCANEO BLE...");
  Serial.printf("[SCAN]    Duración: %d segundos\n", SCAN_DURATION);
  Serial.printf("[SCAN]    Filtro: '%s'\n", DEVICE_NAME_FILTER);
  Serial.println("[SCAN] ════════════════════════════════════════");
  
  // Limpiar dispositivo anterior
  if (foundDevice != nullptr) {
    delete foundDevice;
    foundDevice = nullptr;
  }
  
  // Configurar escaneo
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);
  
  // Escaneo SÍNCRONO (bloquea hasta terminar)
  BLEScanResults* pResults = pBLEScan->start(SCAN_DURATION, false);
  
  int deviceCount = 0;
  if (pResults != nullptr) {
    deviceCount = pResults->getCount();
  }
  Serial.printf("[SCAN] Escaneo completado. Dispositivos encontrados: %d\n", deviceCount);
  
  if (foundDevice != nullptr) {
    Serial.println("[SCAN] ✅ Dispositivo HumanS listo para conectar");
  } else {
    Serial.println("[SCAN] ❌ Dispositivo HumanS NO encontrado");
    Serial.println("[SCAN]    Verifica que el wearable esté encendido y en rango");
  }
  
  // Limpiar resultados
  pBLEScan->clearResults();
  
  lastScanTime = millis();
}

// ═══════════════════════════════════════════════════════════════════════════════
// ║                        COMUNICACIÓN CON PROXY                               ║
// ═══════════════════════════════════════════════════════════════════════════════

bool sendDataToProxy() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[HTTP] ⚠️ WiFi no conectado");
    return false;
  }
  
  if (pendingSpo2 < 0 || pendingHR < 0) {
    return false;
  }
  
  HTTPClient http;
  http.setTimeout(5000);
  http.begin(PROXY_URL);
  http.addHeader("Content-Type", "application/json");
  
  StaticJsonDocument<512> doc;
  doc["device"] = BRIDGE_ID;
  doc["spo2"] = pendingSpo2;
  doc["hr"] = pendingHR;
  doc["rssi"] = currentRSSI;
  doc["avg_rssi"] = avgRSSI;
  doc["distance"] = currentDistance;
  doc["signal_quality"] = getSignalQuality(currentRSSI);
  doc["packet_count"] = packetsSent;
  doc["ble_packets"] = blePacketsReceived;
  doc["is_connected"] = deviceConnected;
  doc["connection_time"] = deviceConnected ? (millis() - connectionStartTime) / 1000 : 0;
  doc["signal_trend"] = getSignalTrend();
  doc["device_mac"] = deviceMAC;
  
  String jsonPayload;
  serializeJson(doc, jsonPayload);
  
  int httpCode = http.POST(jsonPayload);
  
  bool success = false;
  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<256> respDoc;
    if (deserializeJson(respDoc, response) == DeserializationError::Ok) {
      isActiveBridge = respDoc["is_active_bridge"] | false;
      shouldConnect = respDoc["should_connect"] | true;
      
      if (respDoc.containsKey("release_connection") && respDoc["release_connection"].as<bool>()) {
        Serial.println("[PROXY] 📤 Solicitud de liberar conexión");
        if (deviceConnected) {
          forceDisconnect("Handoff solicitado por proxy");
        }
      }
    }
    
    packetsSent++;
    pendingSpo2 = -1;
    pendingHR = -1;
    success = true;
    
    if (isActiveBridge) {
      Serial.printf("[HTTP] ✅ #%d enviado → ACTIVO\n", packetsSent);
    } else {
      Serial.printf("[HTTP] ✓ #%d enviado → standby\n", packetsSent);
    }
  } else {
    Serial.printf("[HTTP] ❌ Error: %d\n", httpCode);
  }
  
  http.end();
  return success;
}

void checkProxyStatus() {
  if (WiFi.status() != WL_CONNECTED) return;
  if (millis() - lastStatusCheck < STATUS_CHECK_INTERVAL) return;
  
  lastStatusCheck = millis();
  
  HTTPClient http;
  http.setTimeout(3000);
  
  String url = String(PROXY_STATUS_URL) + 
               "?bridge_id=" + BRIDGE_ID + 
               "&rssi=" + String(currentRSSI) + 
               "&connected=" + String(deviceConnected ? "1" : "0");
  
  http.begin(url);
  int httpCode = http.GET();
  
  if (httpCode == 200) {
    String response = http.getString();
    StaticJsonDocument<256> doc;
    if (deserializeJson(doc, response) == DeserializationError::Ok) {
      shouldConnect = doc["should_connect"] | true;
      isActiveBridge = doc["is_active_bridge"] | false;
      
      if (!shouldConnect && deviceConnected) {
        Serial.println("[PROXY] 🔄 Handoff: soltando conexión");
        forceDisconnect("Proxy indica mejor bridge disponible");
      }
    }
  }
  
  http.end();
}

// ═══════════════════════════════════════════════════════════════════════════════
// ║                              SETUP                                          ║
// ═══════════════════════════════════════════════════════════════════════════════

void setup() {
  Serial.begin(115200);
  delay(2000);
  
  Serial.println("\n\n");
  Serial.println("╔══════════════════════════════════════════════════════════════════╗");
  Serial.printf("║      HumanS Smart Bridge v5.1 [%s]", BRIDGE_ID);
  for (int i = strlen(BRIDGE_ID); i < 12; i++) Serial.print(" ");
  Serial.println("              ║");
  Serial.println("║      ESCANEO BLE CORREGIDO                                       ║");
  Serial.println("╚══════════════════════════════════════════════════════════════════╝\n");
  
  // ─────────────────── WiFi ───────────────────
  Serial.println("┌─────────────────────────────────────────┐");
  Serial.println("│ [1/2] Inicializando WiFi...             │");
  Serial.println("└─────────────────────────────────────────┘");
  
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  
  Serial.printf("[WiFi] Conectando a '%s'", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[WiFi] ❌ Fallo - Reiniciando en 5s...");
    delay(5000);
    ESP.restart();
  }
  
  Serial.println("\n[WiFi] ✅ Conectado");
  Serial.printf("[WiFi] IP: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("[WiFi] Señal: %d dBm\n", WiFi.RSSI());
  Serial.printf("[WiFi] Proxy: %s\n\n", PROXY_URL);
  
  // ─────────────────── BLE ───────────────────
  Serial.println("┌─────────────────────────────────────────┐");
  Serial.println("│ [2/2] Inicializando BLE...              │");
  Serial.println("└─────────────────────────────────────────┘");
  
  BLEDevice::init("");
  
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new MyScanCallback());
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);
  
  Serial.printf("[BLE] Filtro de nombre: '%s'\n", DEVICE_NAME_FILTER);
  Serial.printf("[BLE] RSSI mínimo para conectar: %d dBm\n", RSSI_MIN_CONNECT);
  Serial.printf("[BLE] Duración escaneo: %d segundos\n\n", SCAN_DURATION);
  
  Serial.println("╔══════════════════════════════════════════════════════════════════╗");
  Serial.println("║                    ✅ Bridge listo                               ║");
  Serial.println("║         Iniciando primer escaneo BLE...                          ║");
  Serial.println("╚══════════════════════════════════════════════════════════════════╝\n");
  
  // Primer escaneo
  delay(1000);
  performScan();
}

// ═══════════════════════════════════════════════════════════════════════════════
// ║                              LOOP                                           ║
// ═══════════════════════════════════════════════════════════════════════════════

void loop() {
  unsigned long now = millis();
  
  // ─────────────── Verificar conexión física ───────────────
  if (deviceConnected && pClient && !pClient->isConnected()) {
    Serial.println("\n[BLE] ⚠️ Conexión física perdida");
    cleanupConnection();
  }
  
  // ─────────────── Actualizar RSSI ───────────────
  if (deviceConnected && pClient && (now - lastRSSIUpdate > RSSI_UPDATE_INTERVAL)) {
    int newRSSI = pClient->getRssi();
    
    if (newRSSI != 0 && newRSSI < 0) {
      currentRSSI = newRSSI;
      updateRSSIHistory(newRSSI);
      currentDistance = calculateDistance(newRSSI);
      
      if (newRSSI <= RSSI_FORCE_DISCONNECT) {
        forceDisconnect("Señal crítica");
      }
    }
    
    lastRSSIUpdate = now;
  }
  
  // ─────────────── Timeout de datos ───────────────
  if (deviceConnected && (now - lastDataTime > DATA_TIMEOUT)) {
    Serial.println("[BLE] ⏱️ Timeout: sin datos por 15s");
    forceDisconnect("Timeout sin datos");
  }
  
  // ─────────────── Escanear si no conectado ───────────────
  if (!deviceConnected && !isConnecting && (now - lastScanTime > SCAN_INTERVAL)) {
    performScan();
  }
  
  // ─────────────── Intentar conexión si hay dispositivo ───────────────
  if (!deviceConnected && !isConnecting && foundDevice != nullptr) {
    delay(500);  // Pequeña pausa antes de conectar
    connectToDevice();
  }
  
  // ─────────────── Consultar estado al proxy ───────────────
  checkProxyStatus();
  
  // ─────────────── Enviar datos pendientes ───────────────
  if (pendingSpo2 > 0 && pendingHR > 0 && (now - lastPostTime > POST_INTERVAL)) {
    sendDataToProxy();
    lastPostTime = now;
  }
  
  // ─────────────── Reconexión WiFi ───────────────
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] ⚠️ Reconectando...");
    WiFi.reconnect();
    delay(2000);
  }
  
  // ─────────────── Estadísticas periódicas ───────────────
  static unsigned long lastStats = 0;
  if (now - lastStats > STATS_INTERVAL) {
    Serial.println("\n┌────────────────────────────────────────────────────┐");
    Serial.printf("│ [%s] ESTADÍSTICAS\n", BRIDGE_ID);
    Serial.println("├────────────────────────────────────────────────────┤");
    Serial.printf("│ Estado: %s %s\n", 
                  deviceConnected ? "🟢 CONECTADO" : "🔴 DESCONECTADO",
                  isActiveBridge ? "(ACTIVO)" : "(standby)");
    Serial.printf("│ RSSI: %d dBm (avg: %d) | %s\n", 
                  currentRSSI, avgRSSI, getSignalQuality(currentRSSI));
    Serial.printf("│ Distancia: %.1f m | Tendencia: %s\n", 
                  currentDistance, getSignalTrend().c_str());
    Serial.printf("│ Paquetes: HTTP=%d | BLE=%d\n", 
                  packetsSent, blePacketsReceived);
    Serial.printf("│ Conexiones: %d | Reconexiones: %d | Fallos: %d\n",
                  connectionAttempts, reconnections, consecutiveFailures);
    
    if (deviceConnected) {
      unsigned long connTime = (now - connectionStartTime) / 1000;
      Serial.printf("│ Tiempo conectado: %lu segundos\n", connTime);
    }
    
    Serial.println("└────────────────────────────────────────────────────┘\n");
    lastStats = now;
  }
  
  delay(50);
}
