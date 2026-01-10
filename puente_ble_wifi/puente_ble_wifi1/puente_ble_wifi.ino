#include <WiFi.h>
#include <HTTPClient.h>
#include <BLEDevice.h>
#include <ArduinoJson.h>

// ======================= CONFIGURACIÓN =======================
const char* WIFI_SSID = "MOVISTAR-WIFI6-1A50";
const char* WIFI_PASSWORD = "AKpQXTCehuWJuokKL2du";

const char* SERVER_URL = "https://humans-cloud-dashboard.onrender.com/api/data";
const char* API_KEY = "f3b2a8d9c6e1f0a7d4b8c2e9f1a3b7d6";

static BLEAddress* pServerAddress = new BLEAddress("00:a0:50:36:ff:de");

static BLEUUID serviceUUID("49535343-fe7d-4ae5-8fa9-9fafd205e455");
static BLEUUID charUUID_notify("49535343-1E4D-4BD9-BA61-23C647249616");
static BLEUUID charUUID_write("49535343-8841-43F4-A8D4-ECBE34729BB3");

// ======================= VARIABLES ===========================
QueueHandle_t dataQueue;  // FreeRTOS queue para datos BLE->HTTP

struct SensorData {
  int spo2;
  int hr;
};

static bool deviceConnected = false;
static bool isConnecting = false;

// ======================= FUNCIONES AUXILIARES ===========================
void sendDataHTTP(SensorData data) {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("x-api-key", API_KEY);

  StaticJsonDocument<100> doc;
  doc["spo2"] = data.spo2;
  doc["hr"] = data.hr;

  String payload;
  serializeJson(doc, payload);

  int code = http.POST(payload);
  if(code > 0){
    Serial.printf("Código de respuesta HTTP: %d\n", code);
  } else {
    Serial.printf("Error HTTP: %s\n", http.errorToString(code).c_str());
  }

  http.end();
}

SensorData decodePacket(uint8_t* pData, size_t len){
  SensorData s = {0,0};
  if(len != 20) return s;

  int hr_samples[] = {pData[3], pData[8], pData[13], pData[18]};
  int spo2_samples[] = {pData[4], pData[9], pData[14], pData[19]};

  int hr_sum=0, hr_count=0, spo2_sum=0, spo2_count=0;
  for(int h: hr_samples) if(h>=30 && h<=220){ hr_sum+=h; hr_count++; }
  for(int s_: spo2_samples) if(s_>=60 && s_<=100){ spo2_sum+=s_; spo2_count++; }

  if(hr_count) s.hr = hr_sum/hr_count;
  if(spo2_count) s.spo2 = spo2_sum/spo2_count;

  return s;
}

// ======================= CALLBACK BLE ===========================
void notifyCallback(BLERemoteCharacteristic* pBLERemoteCharacteristic, uint8_t* pData, size_t length, bool isNotify){
  SensorData data = decodePacket(pData,length);
  if(data.spo2 && data.hr){
    xQueueSend(dataQueue, &data, 0); // Enviar a cola para HTTP
    Serial.printf("Paquete BLE recibido: SpO2=%d%% HR=%d bpm\n", data.spo2, data.hr);
  }
}

// ======================= CLIENT CALLBACK ===========================
class MyClientCallback : public BLEClientCallbacks {
  void onConnect(BLEClient* pclient){
    deviceConnected = true;
    isConnecting = false;
    Serial.println("Conectado al BLE.");
  }

  void onDisconnect(BLEClient* pclient){
    deviceConnected = false;
    isConnecting = false;
    Serial.println("BLE desconectado. Reconectando...");
  }
};

// ======================= TAREAS ===========================
void bleTask(void *pvParameters){
  BLEClient* pClient = BLEDevice::createClient();
  pClient->setClientCallbacks(new MyClientCallback());

  for(;;){
    if(!deviceConnected && !isConnecting){
      isConnecting = true;
      Serial.print("Conectando a ");
      Serial.println(pServerAddress->toString().c_str());

      if(pClient->connect(*pServerAddress)){
        BLERemoteService* service = pClient->getService(serviceUUID);
        if(service){
          BLERemoteCharacteristic* notifyChar = service->getCharacteristic(charUUID_notify);
          BLERemoteCharacteristic* writeChar  = service->getCharacteristic(charUUID_write);

          if(notifyChar && notifyChar->canNotify()){
            notifyChar->registerForNotify(notifyCallback);
            Serial.println("Callback BLE registrado.");
          }

          if(writeChar && writeChar->canWrite()){
            uint8_t cmd[]={0xF1};
            writeChar->writeValue(cmd,1,true);
            Serial.println("Comando de inicio enviado.");
          }
        } else {
          Serial.println("No se encontró el servicio BLE.");
          pClient->disconnect();
        }
      } else {
        Serial.println("Fallo al conectar BLE.");
      }
      isConnecting = false;
    }
    vTaskDelay(2000/portTICK_PERIOD_MS);
  }
}

void httpTask(void *pvParameters){
  SensorData data;
  for(;;){
    if(xQueueReceive(dataQueue, &data, portMAX_DELAY)){
      sendDataHTTP(data);
    }
  }
}

// ======================= SETUP ===========================
void setup() {
  Serial.begin(115200);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando a Wi-Fi");
  while(WiFi.status() != WL_CONNECTED){ Serial.print("."); delay(500);}
  Serial.println("\n¡Wi-Fi conectado!");
  Serial.print("IP ESP32: "); Serial.println(WiFi.localIP());

  BLEDevice::init("");
  dataQueue = xQueueCreate(10, sizeof(SensorData)); // Cola de 10 paquetes

  xTaskCreatePinnedToCore(bleTask,"BLE_Task",10000,NULL,1,NULL,0);
  xTaskCreatePinnedToCore(httpTask,"HTTP_Task",10000,NULL,1,NULL,1);
}

// ======================= LOOP ===========================
void loop(){
  // No hacemos nada, BLE y HTTP corren en tareas independientes
}
