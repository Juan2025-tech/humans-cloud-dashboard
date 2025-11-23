/*********************************************************************************
 *  HumanS / VinculoCare - Puente BLE a Wi-Fi para ESP32
 * -------------------------------------------------------------------------------
 *  Autor: Manus (basado en requerimientos del proyecto)
 *  Fecha: 23 de Noviembre de 2025
 *********************************************************************************/

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <BLEDevice.h>
#include <ArduinoJson.h>

// ======================= CONFIGURACIÓN =======================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com";  // URL completa del endpoint
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

// MAC de tu dispositivo BLE (BerryMed/HumanS)
static BLEAddress* pServerAddress = new BLEAddress("00:a0:50:36:ff:de");

// UUIDs del protocolo BLE
static BLEUUID serviceUUID("49535343-fe7d-4ae5-8fa9-9fafd205e455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

// ======================= VARIABLES GLOBALES =======================
bool deviceConnected = false;
bool isConnecting = false;

// ======================= FUNCIONES =======================

// Decodifica el paquete BLE y retorna SpO2 y HR
void decodeAndSendData(uint8_t* pData, size_t length) {
    if (length != 20) {
        Serial.println("Paquete BLE incorrecto");
        return;
    }

    int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
    int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

    int hr_sum = 0, hr_count = 0, spo2_sum = 0, spo2_count = 0;

    for (int h : hr_samples) if (h >= 30 && h <= 220) { hr_sum += h; hr_count++; }
    for (int s : spo2_samples) if (s >= 60 && s <= 100) { spo2_sum += s; spo2_count++; }

    if (hr_count == 0 || spo2_count == 0) return;

    int hr = hr_sum / hr_count;
    int spo2 = spo2_sum / spo2_count;

    Serial.printf("SpO2=%d%% HR=%d bpm\n", spo2, hr);

    // Enviar al servidor
    if (WiFi.status() == WL_CONNECTED) {
        WiFiClientSecure client;
        client.setInsecure(); // Para HTTPS sin validar certificado

        HTTPClient https;
        https.begin(client, SERVER_URL);
        https.addHeader("Content-Type", "application/json");
        https.addHeader("X-API-KEY", API_KEY);

        StaticJsonDocument<100> doc;
        doc["spo2"] = spo2;
        doc["hr"] = hr;

        String payload;
        serializeJson(doc, payload);

        int httpResponseCode = https.POST(payload);
        if (httpResponseCode > 0) {
            Serial.printf("Datos enviados, código: %d\n", httpResponseCode);
        } else {
            Serial.printf("Error HTTP: %s\n", https.errorToString(httpResponseCode).c_str());
        }
        https.end();
    } else {
        Serial.println("Wi-Fi desconectado");
    }
}

// Callback de notificaciones BLE
static void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic,
                           uint8_t* pData, size_t length, bool isNotify) {
    decodeAndSendData(pData, length);
}

// Callback de conexión BLE
class MyClientCallback : public BLEClientCallbacks {
    void onConnect(BLEClient* pclient) {
        deviceConnected = true;
        isConnecting = false;
        Serial.println("Conectado al BLE");
    }

    void onDisconnect(BLEClient* pclient) {
        deviceConnected = false;
        isConnecting = false;
        Serial.println("BLE desconectado. Reconectando...");
    }
};

// Conecta al dispositivo BLE
void connectToDevice() {
    if (isConnecting || deviceConnected) return;

    isConnecting = true;
    Serial.print("Conectando a ");
    Serial.println(pServerAddress->toString().c_str());

    BLEClient* pClient = BLEDevice::createClient();
    pClient->setClientCallbacks(new MyClientCallback());

    if (!pClient->connect(*pServerAddress)) {
        Serial.println("Fallo al conectar BLE");
        pClient->~BLEClient();
        isConnecting = false;
        return;
    }

    BLERemoteService* pRemoteService = pClient->getService(serviceUUID);
    if (!pRemoteService) {
        Serial.println("No se encontró el servicio BLE");
        pClient->disconnect();
        isConnecting = false;
        return;
    }

    BLERemoteCharacteristic* pRemoteCharacteristic = pRemoteService->getCharacteristic(charUUID_notify);
    if (!pRemoteCharacteristic || !pRemoteCharacteristic->canNotify()) {
        Serial.println("No se encontró la característica de notificación");
        pClient->disconnect();
        isConnecting = false;
        return;
    }
    pRemoteCharacteristic->registerForNotify(notifyCallback);

    BLERemoteCharacteristic* pWriteCharacteristic = pRemoteService->getCharacteristic(charUUID_write);
    if (pWriteCharacteristic && pWriteCharacteristic->canWrite()) {
        uint8_t cmd[] = {0xF1};
        pWriteCharacteristic->writeValue(cmd, 1, true);
        Serial.println("Comando de inicio enviado");
    }
}

// ======================= SETUP =======================
void setup() {
    Serial.begin(115200);
    Serial.println("\nIniciando ESP32...");

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Conectando Wi-Fi");
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nWi-Fi conectado");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());

    BLEDevice::init("");
}

// ======================= LOOP =======================
void loop() {
    if (!deviceConnected) {
        connectToDevice();
    }
    delay(2000); // Pequeña pausa
}
