#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ============================================
// CONFIGURACIÓN WIFI
// ============================================
const char* ssid = "MOVISTAR-WIFI6-1A50";
const char* password = "AKpQXTCehuWJuokKL2du";

// ============================================
// CONFIGURACIÓN PROXY LOCAL (Mac Mini)
// ============================================
const char* proxyUrl = "http://192.168.1.43:5050/api/data";

// ============================================
// CONFIGURACIÓN BLE - OPCIÓN 1: Por MAC Address (MÁS CONFIABLE)
// ============================================
// Descomenta si conoces la MAC del dispositivo:
#define USE_MAC_ADDRESS
static BLEAddress deviceAddress("00:A0:50:36:FF:DE");  // ⬅️ PONER TU MAC AQUÍ

// ============================================
// CONFIGURACIÓN BLE - OPCIÓN 2: Por Nombre (BACKUP)
// ============================================
#ifndef USE_MAC_ADDRESS
  #define USE_NAME_FILTER
  #define DEVICE_NAME_FILTER "humans"  // ⬅️ TODO EN MINÚSCULAS
#endif

// ============================================
// UUIDs del servicio BLE
// ============================================
static BLEUUID serviceUUID("49535343-FE7D-4AE5-8FA9-9FAFD205E455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

// ============================================
// CÁLCULO DE DISTANCIA (basado en RSSI)
// ============================================
const int TX_POWER = -95;
const float N_FACTOR = 2.0;

BLEClient* pClient = nullptr;
BLERemoteCharacteristic* pRemoteChar = nullptr;
BLERemoteCharacteristic* pWriteChar = nullptr;
bool deviceConnected = false;
bool isConnecting = false;
int packetCount = 0;
int lastRSSI = 0;
float lastDistance = 0.0;

// Variables para almacenar datos pendientes
int pendingSpo2 = -1;
int pendingHR = -1;
unsigned long lastPostMs = 0;
const unsigned long POST_INTERVAL = 2000;  // Enviar cada 2 segundos

unsigned long lastReconnectTime = 0;
const unsigned long RECONNECT_INTERVAL = 30000;  // Reconectar cada 30s

// ============================================
// CALLBACK NOTIFICACIONES BLE - 20 BYTES
// ============================================
void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic, 
                    uint8_t* pData, size_t length, bool isNotify) {
  
  if (length != 20) return;  // El protocolo HUMANS usa paquetes de 20 bytes

  // Extraer 4 muestras de HR y SpO2
  int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

  int hr_sum = 0, hr_count = 0;
  int spo2_sum = 0, spo2_count = 0;

  // Promediar solo valores válidos
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

  if (hr_count == 0 || spo2_count == 0) return;

  int hr = hr_sum / hr_count;
  int spo2 = spo2_sum / spo2_count;

  // Guardar para envío posterior
  pendingHR = hr;
  pendingSpo2 = spo2;

  Serial.printf("[BLE] SpO2: %d%%, HR: %d bpm, RSSI: %d dBm\n", spo2, hr, lastRSSI);
}

// ============================================
// CALLBACK DE CONEXIÓN BLE
// ============================================
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient) override {
    deviceConnected = true;
    isConnecting = false;
    Serial.println("[BLE] ✓ Conectado exitosamente");
  }
  
  void onDisconnect(BLEClient* pclient) override {
    deviceConnected = false;
    isConnecting = false;
    Serial.println("[BLE] ✗ Desconectado");
  }
};

// ============================================
// ENVIAR DATOS AL PROXY LOCAL
// ============================================
void sendDataToProxy(int spo2, int hr, float distance, int rssi) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[HTTP] WiFi desconectado");
    return;
  }
  
  HTTPClient http;
  http.begin(proxyUrl);
  http.addHeader("Content-Type", "application/json");
  
  StaticJsonDocument<256> doc;
  doc["spo2"] = spo2;
  doc["hr"] = hr;
  doc["distance"] = distance;
  doc["rssi"] = rssi;
  doc["packet_count"] = packetCount;
  doc["device"] = "ESP32";
  
  String jsonPayload;
  serializeJson(doc, jsonPayload);
  
  int httpCode = http.POST(jsonPayload);
  
  if (httpCode > 0) {
    if (httpCode == 200) {
      Serial.printf("[HTTP] ✓ Enviado a Proxy (Paquete #%d)\n", packetCount);
      packetCount++;
    } else {
      Serial.printf("[HTTP] Error: %d - %s\n", httpCode, http.getString().c_str());
    }
  } else {
    Serial.printf("[HTTP] Error conexión: %s\n", http.errorToString(httpCode).c_str());
  }
  
  http.end();
}

// ============================================
// CONECTAR A DISPOSITIVO BLE
// ============================================
bool connectToDevice(BLEAdvertisedDevice* device = nullptr) {
  if (deviceConnected || isConnecting) {
    return false;
  }

  isConnecting = true;

#ifdef USE_MAC_ADDRESS
  Serial.print("[BLE] Conectando por MAC: ");
  Serial.println(deviceAddress.toString().c_str());
  
  pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());
  
  if (!pClient->connect(deviceAddress)) {
    Serial.println("[BLE] ✗ Error al conectar por MAC");
    isConnecting = false;
    return false;
  }
#else
  if (device == nullptr) {
    isConnecting = false;
    return false;
  }
  
  Serial.printf("[BLE] Conectando a %s...\n", device->getName().c_str());
  lastRSSI = device->getRSSI();
  
  pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());
  
  if (!pClient->connect(device)) {
    Serial.println("[BLE] ✗ Error al conectar");
    isConnecting = false;
    return false;
  }
#endif
  
  // Obtener servicio
  BLERemoteService* pRemoteService = pClient->getService(serviceUUID);
  if (!pRemoteService) {
    Serial.println("[BLE] ✗ Servicio no encontrado");
    pClient->disconnect();
    isConnecting = false;
    return false;
  }
  
  // Obtener característica de notificación
  pRemoteChar = pRemoteService->getCharacteristic(charUUID_notify);
  if (!pRemoteChar) {
    Serial.println("[BLE] ✗ Característica Notify no encontrada");
    pClient->disconnect();
    isConnecting = false;
    return false;
  }
  
  // Registrar callback
  if (pRemoteChar->canNotify()) {
    pRemoteChar->registerForNotify(notifyCallback);
    Serial.println("[BLE] ✓ Notificaciones registradas");
  } else {
    Serial.println("[BLE] ✗ No soporta notificaciones");
    pClient->disconnect();
    isConnecting = false;
    return false;
  }
  
  // CRÍTICO: Enviar comando de inicialización 0xF1
  pWriteChar = pRemoteService->getCharacteristic(charUUID_write);
  if (pWriteChar && pWriteChar->canWrite()) {
    uint8_t cmd[] = {0xF1};
    pWriteChar->writeValue(cmd, 1, true);
    Serial.println("[BLE] ✓ Comando de inicio 0xF1 enviado");
  } else {
    Serial.println("[BLE] ⚠ No se pudo enviar comando de inicio");
  }
  
  lastReconnectTime = millis();
  return true;
}

// ============================================
// CALLBACK ESCANEO BLE (Solo si usas nombre)
// ============================================
#ifdef USE_NAME_FILTER
class MyAdvertisedDeviceCallbacks : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) {
    String name = advertisedDevice.getName().c_str();
    name.toLowerCase();  // Convertir a minúsculas
    
    String filter = String(DEVICE_NAME_FILTER);
    filter.toLowerCase();  // Asegurar que el filtro también esté en minúsculas
    
    if (name.indexOf(filter) != -1 && !deviceConnected && !isConnecting) {
      Serial.printf("[BLE] 🔍 Dispositivo encontrado: %s (RSSI: %d dBm)\n", 
                    advertisedDevice.getName().c_str(), 
                    advertisedDevice.getRSSI());
      
      BLEDevice::getScan()->stop();
      connectToDevice(new BLEAdvertisedDevice(advertisedDevice));
    }
  }
};
#endif

// ============================================
// SETUP
// ============================================
void setup() {
  Serial.begin(115200);
  delay(1000);
  
  Serial.println("\n╔════════════════════════════════════════════════╗");
  Serial.println("║   ESP32 BLE Bridge v2.5 → Proxy (Mac Mini)   ║");
  Serial.println("╚════════════════════════════════════════════════╝");
  
  // Configurar WiFi sin sleep
  WiFi.setSleep(false);
  
  // Conectar a WiFi
  Serial.printf("\n[WiFi] Conectando a %s...\n", ssid);
  WiFi.begin(ssid, password);
  
  int wifiTimeout = 0;
  while (WiFi.status() != WL_CONNECTED && wifiTimeout < 20) {
    delay(500);
    Serial.print(".");
    wifiTimeout++;
  }
  
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[WiFi] ✗ No se pudo conectar");
    Serial.println("[WiFi] Reintentando en 10 segundos...");
    delay(10000);
    ESP.restart();
  }
  
  Serial.println("\n[WiFi] ✓ Conectado");
  Serial.printf("[WiFi] IP: %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("[WiFi] Enviando a: %s\n", proxyUrl);
  
  // Inicializar BLE
  Serial.println("\n[BLE] Inicializando...");
  BLEDevice::init("");
  
#ifdef USE_MAC_ADDRESS
  Serial.println("[BLE] Modo: Conexión directa por MAC Address");
  connectToDevice();
#else
  Serial.println("[BLE] Modo: Búsqueda por nombre");
  Serial.printf("[BLE] Buscando: '%s'\n", DEVICE_NAME_FILTER);
  
  BLEScan* pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new MyAdvertisedDeviceCallbacks());
  pBLEScan->setActiveScan(true);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99);
  
  Serial.println("[BLE] ✓ Escaneando dispositivos...\n");
  pBLEScan->start(0, false);  // Escaneo continuo
#endif
}

// ============================================
// LOOP
// ============================================
void loop() {
  // Reconectar si está desconectado
  if (!deviceConnected && !isConnecting) {
#ifdef USE_MAC_ADDRESS
    connectToDevice();
    delay(5000);  // Esperar antes de reintentar
#else
    // El scanner se encarga de buscar automáticamente
    delay(1000);
#endif
  }
  
  // Enviar datos si hay pendientes
  unsigned long now = millis();
  if (pendingSpo2 > 0 && pendingHR > 0 && 
      now - lastPostMs >= POST_INTERVAL) {
    
    // Calcular distancia
    if (lastRSSI != 0) {
      lastDistance = pow(10, ((float)TX_POWER - lastRSSI) / (10.0 * N_FACTOR));
      lastDistance = round(lastDistance * 100.0) / 100.0;
    }
    
    sendDataToProxy(pendingSpo2, pendingHR, lastDistance, lastRSSI);
    
    pendingSpo2 = -1;
    pendingHR = -1;
    lastPostMs = now;
  }
  
  // Reconexión periódica para actualizar RSSI
  if (deviceConnected && (millis() - lastReconnectTime > RECONNECT_INTERVAL)) {
    Serial.println("\n[BLE] 🔄 Reconexión programada para actualizar RSSI...");
    pClient->disconnect();
    deviceConnected = false;
    delay(2000);
#ifndef USE_MAC_ADDRESS
    BLEDevice::getScan()->start(5, false);
#endif
  }
  
  // Actualizar RSSI si estamos conectados
  if (deviceConnected && pClient) {
    static unsigned long lastRSSIUpdate = 0;
    if (millis() - lastRSSIUpdate > 5000) {
      lastRSSI = pClient->getRssi();
      lastRSSIUpdate = millis();
    }
  }
  
  delay(10);
}