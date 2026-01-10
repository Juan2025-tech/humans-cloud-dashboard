#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>

// ======================= CONFIGURACIÓN =======================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com/api/data";
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

const unsigned long POST_INTERVAL_MS = 5000; // 5 segundos

unsigned long lastPostMs = 0;

// --- Función para generar valores aleatorios plausibles ---
int randomHR() {
  // frecuencia cardíaca plausible 45..120
  return random(55, 95); // ajusta rango si quieres otro
}
int randomSpO2() {
  // SpO2 plausible 85..100
  return random(94, 100);
}

// --- Función para enviar datos al servidor ---
void sendDataToServer(int spo2, int hr) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi desconectado. No se pueden enviar los datos.");
    return;
  }

  // Usamos WiFiClientSecure con setInsecure() para evitar problemas con certificados en pruebas
  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient https;
  Serial.print("Iniciando POST a: ");
  Serial.println(SERVER_URL);

  if (!https.begin(client, SERVER_URL)) {
    Serial.println("Error al iniciar cliente HTTPS (https.begin).");
    return;
  }

  https.addHeader("Content-Type", "application/json");
  https.addHeader("x-api-key", API_KEY);

  StaticJsonDocument<128> doc;
  doc["spo2"] = spo2;
  doc["hr"]   = hr;
  String jsonPayload;
  serializeJson(doc, jsonPayload);

  Serial.print("Payload: ");
  Serial.println(jsonPayload);

  int httpResponseCode = https.POST(jsonPayload);
  if (httpResponseCode > 0) {
    Serial.printf("Código de respuesta HTTP: %d\n", httpResponseCode);
    String resp = https.getString();
    if (resp.length() > 0) {
      Serial.print("Respuesta servidor: ");
      Serial.println(resp);
    }
  } else {
    Serial.printf("Error en POST: %d -> %s\n", httpResponseCode, https.errorToString(httpResponseCode).c_str());
  }

  https.end();
}

// --- Setup ---
void setup() {
  Serial.begin(115200);
  delay(100);

  // seed aleatorio usando esp_random
  uint32_t seed = esp_random();
  randomSeed(seed);
  Serial.printf("seed aleatorio: %u\n", seed);

  Serial.print("Conectando a Wi-Fi ");
  Serial.print(WIFI_SSID);
  Serial.print(" ...");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    // timeout de 20s
    if (millis() - start > 20000) {
      Serial.println("\nNo se pudo conectar a Wi-Fi en 20s. Reintentando...");
      WiFi.disconnect();
      delay(1000);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      start = millis();
    }
  }
  Serial.println("\nWi-Fi conectado!");
  Serial.print("IP ESP32: ");
  Serial.println(WiFi.localIP());
  lastPostMs = millis();
}

// --- Loop ---
void loop() {
  unsigned long now = millis();
  if (now - lastPostMs >= POST_INTERVAL_MS) {
    lastPostMs = now;

    int hr = randomHR();
    int spo2 = randomSpO2();

    Serial.printf("Enviando (test) SpO2=%d HR=%d\n", spo2, hr);
    sendDataToServer(spo2, hr);
    Serial.println("--------------------------------");
  }

  // tareas livianas en el loop (no bloquear más de lo necesario)
  delay(10);
}
