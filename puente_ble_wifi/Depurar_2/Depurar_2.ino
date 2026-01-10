/*
  Versión 2.2 - BLE + HTTPS estable para Render
  - Envío HTTPS (WiFiClientSecure + setInsecure) fuera del callback BLE
  - Buffer (último valor recibido) usado por loop()
  - Reintento HTTP y reconexión WiFi/BLE
  - Timeouts y protecciones para minimizar conflictos BLE<->WiFi
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <BLEDevice.h>

// ======================= CONFIG =======================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

// Debe ser HTTPS (Render fuerza HTTPS)
const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com/api/data";
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

// BLE device (ajusta MAC)
static BLEAddress* pServerAddress = new BLEAddress("00:A0:50:36:FF:DE");

// UUIDs (mantén los tuyos)
static BLEUUID serviceUUID("49535343-fe7d-4ae5-8fa9-9fafd205e455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

// Estados
static volatile bool deviceConnected = false;
static volatile bool isConnecting = false;

// Buffer simple (última muestra válida)
volatile int pendingSpo2 = -1;
volatile int pendingHR   = -1;

// Control de envío
unsigned long lastPostMs = 0;
const unsigned long POST_INTERVAL = 2000; // ms entre POSTs
const int HTTP_MAX_RETRIES = 2;           // reintentos por POST
const unsigned long WIFI_RECONNECT_INTERVAL = 5000; // ms

// Último intento de reconexión WiFi
unsigned long lastWifiCheck = 0;

// ======================= UTILIDADES =======================
void ensureWiFiConnected() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long start = millis();
  Serial.println("WiFi desconectado. Intentando reconectar...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  // Intentamos reconectar con timeout corto (no bloquear mucho)
  while (WiFi.status() != WL_CONNECTED && millis() - start < 10000) {
    delay(200);
    Serial.print(".");
    yield();
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi reconectado!");
    Serial.print("IP: "); Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nReconexión WiFi fallida (timeout). Seguiremos intentando periódicamente.");
  }
}

// ======================= HTTP(S) SEND =======================
// Envía POST HTTPS usando WiFiClientSecure; reintenta hasta HTTP_MAX_RETRIES
bool sendDataToServerHTTPS(int spo2, int hr) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("No hay WiFi. Abortando POST.");
    return false;
  }

  int attempt = 0;
  while (attempt <= HTTP_MAX_RETRIES) {
    attempt++;
    Serial.printf("POST intento %d/%d\n", attempt, HTTP_MAX_RETRIES + 1);

    WiFiClientSecure client;
    client.setInsecure(); // Ignorar verificación de certificado (pruebas)

    HTTPClient https;
    https.setTimeout(5000); // ms de timeout para operaciones

    // Iniciar conexión HTTPS
    if (!https.begin(client, SERVER_URL)) {
      Serial.println("https.begin() fallo.");
      client.stop();
      https.end();
      // Esperar un poco y reintentar
      delay(200);
      continue;
    }

    https.addHeader("Content-Type", "application/json");
    https.addHeader("x-api-key", API_KEY);

    StaticJsonDocument<128> doc;
    doc["spo2"] = spo2;
    doc["hr"]   = hr;
    String payload;
    serializeJson(doc, payload);

    Serial.print("Enviando POST: ");
    Serial.println(payload);

    int code = https.POST(payload);
    Serial.printf("HTTP code: %d\n", code);

    if (code > 0) {
      // Caso éxito o redirección con contenido
      String response = https.getString();
      if (response.length()) {
        Serial.print("Respuesta servidor: ");
        Serial.println(response);
      } else {
        Serial.println("Respuesta servidor vacía.");
      }

      // Cerrar y limpiar
      https.end();
      client.stop();
      // Considerar 2xx como éxito
      if (code >= 200 && code < 300) return true;

      // Si recibimos 301/307 etc., tomamos como fallo y reintentamos una vez más
      Serial.println("No 2xx recibido, reintentando si aplica...");
      delay(100);
      continue;
    } else {
      // code <= 0 => error local (socket cerrado, timeout,...)
      Serial.printf("Error en POST (cliente): %d -> %s\n", code, https.errorToString(code).c_str());
      https.end();
      client.stop();
      // Esperar un poco antes de reintentar
      delay(200);
      continue;
    }
  } // fin while intentos

  Serial.println("POST fallido tras reintentos.");
  return false;
}

// ======================= CALLBACK BLE =======================
static void notifyCallback(
  BLERemoteCharacteristic* pBLERemoteCharacteristic,
  uint8_t* pData, size_t length, bool isNotify)
{
  // Lo más rápido posible: validar longitud y extraer promedio
  if (length != 20) return;

  int hr_samples[]   = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

  int hr_sum=0, hr_count=0;
  int spo2_sum=0, spo2_count=0;

  for (int i=0;i<4;i++) {
    int h = hr_samples[i];
    int s = spo2_samples[i];
    if (h>=30 && h<=220) { hr_sum += h; hr_count++; }
    if (s>=60 && s<=100) { spo2_sum += s; spo2_count++; }
  }

  if (hr_count == 0 || spo2_count == 0) return;

  int hr = hr_sum / hr_count;
  int spo2 = spo2_sum / spo2_count;

  // Logging mínimo
  Serial.printf("[BLE] SpO2=%d HR=%d\n", spo2, hr);

  // Guardar en buffer atómico (volatiles)
  pendingSpo2 = spo2;
  pendingHR   = hr;

  // NUNCA LLEVAR A CABO OPERACIONES DE RED AQUÍ
}

// ======================= CLIENTE BLE CALLBACKS =======================
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient) {
    deviceConnected = true;
    isConnecting = false;
    Serial.println("BLE conectado");
  }
  void onDisconnect(BLEClient* pclient) {
    deviceConnected = false;
    isConnecting = false;
    Serial.println("BLE desconectado");
  }
};

// ======================= CONEXIÓN AL DISPOSITIVO BLE =======================
void connectToDevice() {
  if (isConnecting || deviceConnected) return;
  isConnecting = true;
  Serial.print("Conectando a ");
  Serial.println(pServerAddress->toString().c_str());

  BLEClient* pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());

  // Intento de conexión (no bloqueante excesivo)
  if (!pClient->connect(*pServerAddress)) {
    Serial.println("Fallo al conectar BLE");
    isConnecting = false;
    // liberar cliente creado
    delete pClient;
    return;
  }

  BLERemoteService* pService = pClient->getService(serviceUUID);
  if (!pService) {
    Serial.println("Servicio BLE no encontrado");
    pClient->disconnect();
    isConnecting = false;
    delete pClient;
    return;
  }

  BLERemoteCharacteristic* pNotify = pService->getCharacteristic(charUUID_notify);
  if (!pNotify || !pNotify->canNotify()) {
    Serial.println("Caracteristica notify no encontrada");
    pClient->disconnect();
    isConnecting = false;
    delete pClient;
    return;
  }
  pNotify->registerForNotify(notifyCallback);

  // Enviar comando de inicio si existe
  BLERemoteCharacteristic* pWrite = pService->getCharacteristic(charUUID_write);
  if (pWrite && pWrite->canWrite()) {
    uint8_t cmd[] = {0xF1};
    pWrite->writeValue(cmd, 1, true);
    Serial.println("Comando de inicio enviado");
  }

  // dejar pClient gestionado por BLEDevice internamente (no borramos aquí)
}

// ======================= SETUP =======================
void setup() {
  Serial.begin(115200);
  delay(50);

  // Desactivar sleep WiFi para robustez en BLE+WiFi
  WiFi.setSleep(false);

  Serial.print("Conectando a WiFi ");
  Serial.print(WIFI_SSID);
  Serial.print(" ...");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(300);
    Serial.print(".");
    yield();
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWiFi conectado!");
    Serial.print("IP ESP32: "); Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nAVISO: No conectado a WiFi al inicio. Se intentará reconectar periódicamente.");
  }

  BLEDevice::init("");
  lastWifiCheck = millis();
}

// ======================= LOOP =======================
void loop() {
  // 1) Mantener WiFi
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiCheck > WIFI_RECONNECT_INTERVAL) {
    lastWifiCheck = millis();
    ensureWiFiConnected();
  }

  // 2) Mantener BLE
  if (!deviceConnected) {
    connectToDevice();
    delay(100);
  }

  // 3) Si hay datos pendientes, y ha pasado el intervalo, enviarlos por HTTPS
  unsigned long now = millis();
  if (pendingSpo2 > 0 && pendingHR > 0 && now - lastPostMs >= POST_INTERVAL) {
    // Capturar y limpiar buffer (operación rápida)
    int spo2 = pendingSpo2;
    int hr   = pendingHR;
    pendingSpo2 = -1;
    pendingHR   = -1;

    lastPostMs = now;

    // Intentar enviar por HTTPS (bloquea hasta 5s por timeout interno)
    bool ok = sendDataToServerHTTPS(spo2, hr);
    if (!ok) {
      Serial.println("POST no enviado correctamente (ver logs).");
      // En caso de fallo persistente, considerar reconexión WiFi
      if (WiFi.status() != WL_CONNECTED) {
        ensureWiFiConnected();
      }
    } else {
      Serial.println("POST completado correctamente.");
    }
  }

  // Pequeña suspensión cooperativa (evitar bloqueos)
  delay(10);
}
