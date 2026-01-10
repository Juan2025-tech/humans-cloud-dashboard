#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com/api/data";
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando a Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\n¡Wi-Fi conectado!");
  Serial.print("IP ESP32: "); Serial.println(WiFi.localIP());

  // --- Test POST ---
  WiFiClientSecure client;
  client.setInsecure();  // Ignora validación SSL para prueba rápida

  HTTPClient https;
  https.begin(client, SERVER_URL);
  https.addHeader("Content-Type", "application/json");
  https.addHeader("x-api-key", API_KEY);

  int code = https.POST("{\"spo2\":99,\"hr\":80}");
  Serial.print("Código de respuesta HTTP: "); Serial.println(code);

  https.end();
}

void loop() {
  // Nada
}
