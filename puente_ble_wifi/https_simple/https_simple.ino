#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>

const char* ssid = "MOVISTAR-WIFI6-1A50";
const char* password = "AKpQXTCehuWJuokKL2du";
const char* server = "https://humans-cloud-dashboard.onrender.com/api/data";

void setup() {
  Serial.begin(115200);
  WiFi.begin(ssid,password);
  while(WiFi.status()!=WL_CONNECTED){ delay(500); Serial.print("."); }
  Serial.println("\nWi-Fi conectado.");

  WiFiClientSecure client;
  client.setInsecure(); // DESACTIVADO SSL temporal
  HTTPClient https;
  https.begin(client, server);
  https.addHeader("Content-Type","application/json");
  https.addHeader("x-api-key","f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6");

  int code = https.POST("{\"spo2\":99,\"hr\":70}");
  Serial.printf("HTTP response: %d\n", code);
  https.end();
}

void loop(){}
