//Bridge puente_ble_wifi2.3.ino
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <BLEDevice.h>

// ==========================================================
// CONFIGURACIÓN
// ==========================================================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";
//const char* WIFI_SSID = "MOVISTAR-WIFI6-6EF0_EXT";
//const char* WIFI_PASSWORD = "EVNgw3etaYWRWsa5P7uB";


// URL del proxy en tu Mac mini o Raspberry Pi
// ⚠️ Cambia esta IP por la tuya (ipconfig getifaddr en0)
const char* SERVER_URL = "http://192.168.1.43:5050/api/data";

static BLEAddress* pServerAddress = new BLEAddress("00:A0:50:36:FF:DE");

static BLEUUID serviceUUID("49535343-fe7d-4ae5-8fa9-9fafd205e455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

volatile bool deviceConnected = false;
volatile bool isConnecting = false;

volatile int pendingSpo2 = -1;
volatile int pendingHR   = -1;

unsigned long lastPostMs = 0;
const unsigned long POST_INTERVAL = 2000;

// ==========================================================
// FUNCIÓN: Enviar datos al proxy HTTP local
// ==========================================================
bool sendDataToServerHTTP(int spo2, int hr) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[HTTP] WiFi no conectado");
    return false;
  }

  HTTPClient http;
  http.begin(SERVER_URL);  // HTTP puro

  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<100> doc;
  doc["spo2"] = spo2;
  doc["hr"]   = hr;

  String jsonPayload;
  serializeJson(doc, jsonPayload);

  Serial.print("POST → Proxy: ");
  Serial.println(jsonPayload);

  int httpCode = http.POST(jsonPayload);
  Serial.printf("HTTP code: %d\n", httpCode);

  if (httpCode > 0) {
    Serial.print("Respuesta Proxy: ");
    Serial.println(http.getString());
  } else {
    Serial.printf("Error POST: %s\n", http.errorToString(httpCode).c_str());
  }

  http.end();
  return (httpCode > 0);
}

// ==========================================================
// CALLBACK BLE — paquetes de 20 bytes
// ==========================================================
static void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic,
                           uint8_t* pData, size_t length, bool isNotify) {
  if (length != 20) return;

  int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

  int hr_sum = 0, hr_count = 0;
  int spo2_sum = 0, spo2_count = 0;

  for (int h : hr_samples)  if (h >= 30 && h <= 220) { hr_sum += h; hr_count++; }
  for (int s : spo2_samples) if (s >= 60 && s <= 100) { spo2_sum += s; spo2_count++; }

  if (hr_count == 0 || spo2_count == 0) return;

  int hr = hr_sum / hr_count;
  int spo2 = spo2_sum / spo2_count;

  pendingHR = hr;
  pendingSpo2 = spo2;

  Serial.printf("[BLE] SpO2=%d HR=%d\n", spo2, hr);
}

// ==========================================================
// CLIENT CALLBACK BLE
// ==========================================================
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient) override {
    deviceConnected = true;
    isConnecting = false;
    Serial.println("BLE conectado");
  }
  void onDisconnect(BLEClient* pclient) override {
    deviceConnected = false;
    isConnecting = false;
    Serial.println("BLE desconectado");
  }
};

// ==========================================================
// CONECTAR BLE
// ==========================================================
void connectToDevice() {
  if (deviceConnected || isConnecting) return;

  isConnecting = true;
  Serial.print("Conectando a BLE: ");
  Serial.println(pServerAddress->toString().c_str());

  BLEClient* pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());

  if (!pClient->connect(*pServerAddress)) {
    Serial.println("Error conectando BLE");
    isConnecting = false;
    return;
  }

  BLERemoteService* pRemoteService = pClient->getService(serviceUUID);
  if (!pRemoteService) { Serial.println("Servicio BLE no encontrado"); return; }

  BLERemoteCharacteristic* pNotifyChar = pRemoteService->getCharacteristic(charUUID_notify);
  if (!pNotifyChar) { Serial.println("Notify no encontrado"); return; }

  pNotifyChar->registerForNotify(notifyCallback);

  BLERemoteCharacteristic* pWriteChar = pRemoteService->getCharacteristic(charUUID_write);
  if (pWriteChar && pWriteChar->canWrite()) {
    uint8_t cmd[] = {0xF1};
    pWriteChar->writeValue(cmd, 1, true);
    Serial.println("Comando de inicio enviado");
  }
}

// ==========================================================
// SETUP
// ==========================================================
void setup() {
  Serial.begin(115200);
  WiFi.setSleep(false);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.println("\nWiFi conectado!");
  Serial.print("IP: ");
  Serial.println(WiFi.localIP());

  BLEDevice::init("");
}

// ==========================================================
// LOOP
// ==========================================================
void loop() {
  if (!deviceConnected) connectToDevice();

  unsigned long now = millis();

  if (pendingSpo2 > 0 && pendingHR > 0 &&
      now - lastPostMs >= POST_INTERVAL) {

    sendDataToServerHTTP(pendingSpo2, pendingHR);

    pendingSpo2 = -1;
    pendingHR = -1;

    lastPostMs = now;
  }

  delay(10);
}
