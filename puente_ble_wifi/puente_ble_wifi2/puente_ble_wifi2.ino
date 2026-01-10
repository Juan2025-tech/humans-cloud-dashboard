#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <BLEDevice.h>
#include <ArduinoJson.h>

// ======================= CONFIGURACIÓN =======================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com/api/data";
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

static BLEAddress* pServerAddress = new BLEAddress("00:A0:50:36:FF:DE"); // Cambia por tu MAC

static BLEUUID serviceUUID("49535343-fe7d-4ae5-8fa9-9fafd205e455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

static boolean deviceConnected = false;
static boolean isConnecting = false;

// --- Función para enviar datos al servidor ---
void sendDataToServer(int spo2, int hr) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi desconectado. No se pueden enviar los datos.");
    return;
  }

  WiFiClientSecure client;
  client.setInsecure(); // Ignorar SSL, necesario para Render
  HTTPClient https;
  https.begin(client, SERVER_URL);
  https.addHeader("Content-Type", "application/json");
  https.addHeader("x-api-key", API_KEY);

  StaticJsonDocument<100> doc;
  doc["spo2"] = spo2;
  doc["hr"] = hr;
  String jsonPayload;
  serializeJson(doc, jsonPayload);

  int httpResponseCode = https.POST(jsonPayload);
  Serial.printf("Código de respuesta HTTP: %d\n", httpResponseCode);
  https.end();
}

// --- Callback BLE ---
static void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic, uint8_t* pData, size_t length, bool isNotify) {
  if (length != 20) return;

  int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

  int hr_sum = 0, hr_count = 0;
  int spo2_sum = 0, spo2_count = 0;

  for (int h : hr_samples) { if (h>=30 && h<=220) { hr_sum+=h; hr_count++; } }
  for (int s : spo2_samples) { if (s>=60 && s<=100) { spo2_sum+=s; spo2_count++; } }

  if(hr_count==0 || spo2_count==0) return;

  int hr = hr_sum / hr_count;
  int spo2 = spo2_sum / spo2_count;

  Serial.printf("Paquete BLE: SpO2=%d%% HR=%d bpm\n", spo2, hr);

  sendDataToServer(spo2, hr);
}

// --- Cliente BLE callbacks ---
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient) { deviceConnected = true; isConnecting = false; Serial.println("Conectado al BLE"); }
  void onDisconnect(BLEClient* pclient) { deviceConnected = false; isConnecting = false; Serial.println("BLE desconectado. Reconectando..."); }
};

// --- Conexión al dispositivo ---
void connectToDevice() {
  if (isConnecting || deviceConnected) return;
  isConnecting = true;
  Serial.print("Conectando a ");
  Serial.println(pServerAddress->toString().c_str());

  BLEClient* pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());

  if (!pClient->connect(*pServerAddress)) {
    Serial.println("Fallo al conectar con BLE");
    isConnecting=false;
    return;
  }

  BLERemoteService* pRemoteService = pClient->getService(serviceUUID);
  if (!pRemoteService) { Serial.println("No se encontró el servicio BLE"); pClient->disconnect(); isConnecting=false; return; }

  BLERemoteCharacteristic* pRemoteChar = pRemoteService->getCharacteristic(charUUID_notify);
  if (!pRemoteChar || !pRemoteChar->canNotify()) { Serial.println("No se encontró la característica BLE"); pClient->disconnect(); isConnecting=false; return; }
  pRemoteChar->registerForNotify(notifyCallback);

  BLERemoteCharacteristic* pWriteChar = pRemoteService->getCharacteristic(charUUID_write);
  if (pWriteChar && pWriteChar->canWrite()) {
    uint8_t command[] = {0xF1};
    pWriteChar->writeValue(command, 1, true);
    Serial.println("Comando de inicio enviado");
  }
}

// --- Setup ---
void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando a Wi-Fi...");
  while(WiFi.status()!=WL_CONNECTED){ delay(500); Serial.print("."); }
  Serial.println("\nWi-Fi conectado!");
  Serial.print("IP ESP32: ");
  Serial.println(WiFi.localIP());

  BLEDevice::init("");
}

// --- Loop ---
void loop() {
  if (!deviceConnected) connectToDevice();
  delay(2000);
}
