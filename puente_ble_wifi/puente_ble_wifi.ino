#include <WiFi.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <ArduinoJson.h> // Necesitarás instalar esta librería desde el Gestor de Librerías

// ----------------------------------------------------
// --- CONFIGURACIÓN: MODIFICA ESTOS VALORES ---
// ----------------------------------------------------
const char* WIFI_SSID = "El_Nombre_De_Tu_WiFi";
const char* WIFI_PASSWORD = "Tu_Contraseña_De_WiFi";

// URL de tu servidor en la nube (o local para pruebas: http://tu_ip_local:5000/api/data )
const char* SERVER_URL = "https://tu-app.onrender.com/api/data"; 

// Dirección MAC del smartwatch. Dejar en blanco para conectar al primer dispositivo que se encuentre.
// Formato: "01:23:45:67:89:ab"
static BLEAddress* pServerAddress = new BLEAddress("XX:XX:XX:XX:XX:XX" ); // <-- ¡MUY IMPORTANTE! Pon la MAC de tu reloj

// UUIDs del servicio y característica del smartwatch
static BLEUUID serviceUUID("49535343-C321-453D-90A2-C6D14315B68B"); // <-- Reemplaza si el servicio UUID es diferente
static BLEUUID charUUID("49535343-1E4D-4BD9-BA61-23C647249616"); // Característica de notificación (SEND_CHAR_UUID)
static BLEUUID writeCharUUID("49535343-8841-43F4-A8D4-ECBE34729BB3"); // Característica de escritura (RECV_CHAR_UUID)
// ----------------------------------------------------

// Variables globales de estado
static boolean doConnect = false;
static boolean connected = false;
static BLERemoteCharacteristic* pRemoteCharacteristic;
static BLERemoteCharacteristic* pWriteCharacteristic;

// Función para decodificar el paquete de datos
void decodeAndSendData(uint8_t* pData, size_t length) {
    if (length != 20) return;

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

    if (hr_count == 0 || spo2_count == 0) return;

    int hr = hr_sum / hr_count;
    int spo2 = spo2_sum / spo2_count;

    Serial.printf("Decodificado: SpO2=%d%%, HR=%d bpm\n", spo2, hr);

    // Enviar datos al servidor si estamos conectados a WiFi
    if (WiFi.status() == WL_CONNECTED) {
        HTTPClient http;
        http.begin(SERVER_URL );
        http.addHeader("Content-Type", "application/json" );

        StaticJsonDocument<100> doc;
        doc["spo2"] = spo2;
        doc["hr"] = hr;

        String jsonPayload;
        serializeJson(doc, jsonPayload);

        int httpResponseCode = http.POST(jsonPayload );
        if (httpResponseCode > 0 ) {
            Serial.printf("Datos enviados. Código de respuesta: %d\n", httpResponseCode );
        } else {
            Serial.printf("Error en el envío HTTP: %s\n", http.errorToString(httpResponseCode ).c_str());
        }
        http.end( );
    }
}

// Callback que se ejecuta cuando llegan datos por BLE
static void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic, uint8_t* pData, size_t length, bool isNotify) {
    decodeAndSendData(pData, length);
}

// Clase para gestionar la conexión BLE
class MyClientCallback : public BLEClientCallbacks {
    void onConnect(BLEClient* pclient) {
        connected = true;
        Serial.println("Conectado al dispositivo BLE.");
    }
    void onDisconnect(BLEClient* pclient) {
        connected = false;
        doConnect = true; // Marcar para intentar reconectar
        Serial.println("Desconectado del dispositivo BLE. Intentando reconectar...");
    }
};

// Función para conectar al servidor BLE
bool connectToServer() {
    Serial.print("Conectando a ");
    Serial.println(pServerAddress->toString().c_str());
    
    BLEClient* pClient = BLEDevice::createClient();
    pClient->setClientCallbacks(new MyClientCallback());
    pClient->connect(*pServerAddress);

    BLERemoteService* pRemoteService = pClient->getService(serviceUUID);
    if (pRemoteService == nullptr) {
        Serial.print("No se pudo encontrar el servicio UUID: ");
        Serial.println(serviceUUID.toString().c_str());
        pClient->disconnect();
        return false;
    }

    pRemoteCharacteristic = pRemoteService->getCharacteristic(charUUID);
    if (pRemoteCharacteristic == nullptr || !pRemoteCharacteristic->canNotify()) {
        Serial.println("No se pudo encontrar la característica de notificación.");
        pClient->disconnect();
        return false;
    }
    pRemoteCharacteristic->registerForNotify(notifyCallback);

    pWriteCharacteristic = pRemoteService->getCharacteristic(writeCharUUID);
    if (pWriteCharacteristic == nullptr || !pWriteCharacteristic->canWrite()) {
        Serial.println("No se pudo encontrar la característica de escritura.");
        return false;
    }
    
    // Enviar el comando para iniciar la transmisión de datos
    uint8_t command[] = {0xF1};
    pWriteCharacteristic->writeValue(command, 1, true);
    
    return true;
}

void setup() {
    Serial.begin(115200);
    Serial.println("Iniciando ESP32...");

    // Conectar a WiFi
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.print("Conectando a WiFi...");
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
    }
    Serial.println("\nConectado a WiFi!");

    // Iniciar BLE
    BLEDevice::init("");
    doConnect = true;
}

void loop() {
    if (doConnect) {
        if (connectToServer()) {
            Serial.println("¡Conexión BLE exitosa!");
            doConnect = false;
        } else {
            Serial.println("Fallo en la conexión BLE. Reintentando en 5 segundos...");
            delay(5000); // Esperar antes de reintentar
        }
    }
    // El trabajo principal se realiza en el callback de notificación
    delay(1000);
}
void setup() {
  // put your setup code here, to run once:

}

void loop() {
  // put your main code here, to run repeatedly:

}
