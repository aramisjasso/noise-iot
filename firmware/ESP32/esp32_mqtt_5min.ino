/*
  esp32_mqtt_5min.ino — ESP32 (Arduino, 2 núcleos) -> broker MQTT (Mosquitto)

  Ciclo:
    1. DHT11: lectura "antes"
    2. 5 min SOLO de audio, continuo y sin huecos (8 kHz, ADC 12 bits)
         - NÚCLEO 1: un temporizador de hardware marca cada 125 µs y una tarea de
           prioridad máxima lee el KY-037. Nunca se detiene por la red.
         - NÚCLEO 0: publica cada bloque de 0.5 s en el broker mientras se sigue grabando.
    3. DHT11: lectura "despues"
    4. Pausa de 5 min y se repite.

  El ESP32 solo envía el audio en crudo (es lo más ligero: 16 KB/s).
  El WAV y los CSV los genera receptor_mqtt.py en la PC u Orange Pi.

  Tópicos (BASE = estacion/<DISPOSITIVO>):
    BASE/inicio   JSON    metadatos de la sesión
    BASE/dht      JSON    {"sesion","momento":"antes"|"despues","temp","hum","intentos"}
    BASE/audio    BIN     sesion(16 bytes) + seq(uint32) + rellenos(uint16) + muestras uint16 LE
    BASE/fin      JSON    resumen (muestras rellenadas, bloques perdidos...)
    BASE/estado   texto   "en_linea" / "desconectado"  (retenido)

  Arduino IDE:
    - Placa: "ESP32 Dev Module", paquete "esp32" de Espressif versión 3.x
    - Bibliotecas: "PubSubClient" (Nick O'Leary) y "DHT sensor library" (Adafruit)
    - Herramientas > Core Debug Level: None
*/

#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <DHT.h>
#include <time.h>

// ============================ CONFIGURACIÓN ============================
const char* WIFI_SSID   = "POCOX7";
const char* WIFI_CLAVE  = "00000000";

const char* MQTT_BROKER  = "158.97.122.248";   // IP de la PC u Orange Pi con Mosquitto (sin espacios)
const uint16_t MQTT_PUERTO = 1883;
const char* MQTT_USUARIO = nullptr;          // o "usuario" si Mosquitto pide clave
const char* MQTT_CLAVE   = nullptr;

const char* DISPOSITIVO = "esp32-01";

const int PIN_DHT  = 4;                       // DATA del DHT11
const int PIN_MIC  = 34;                      // A0 del KY-037 (ADC1: GPIO 32-39)
const int BITS_ADC = 12;                      // máximo del ESP32: 0-4095

constexpr uint32_t FS              = 8000;    // Hz
constexpr uint32_t DURACION_S      = 300;     // 5 min de audio
constexpr uint32_t PAUSA_S         = 300;     // 5 min sin grabar entre sesiones (0 = continuo)
constexpr uint32_t MUESTRAS_BLOQUE = 4000;    // 0.5 s por mensaje MQTT (8 KB)
constexpr int      MAX_BUFFERS     = 16;      // hasta 8 s de colchón si la red se atora
// =======================================================================

constexpr uint32_t N_BLOQUES = DURACION_S * FS / MUESTRAS_BLOQUE;   // 600
static_assert((DURACION_S * FS) % MUESTRAS_BLOQUE == 0, "DURACION_S*FS debe ser múltiplo del bloque");
constexpr size_t CAB = 16 + 4 + 2;                                  // sesion + seq + rellenos
constexpr size_t TAM_BLOQUE = CAB + 2 * MUESTRAS_BLOQUE;

struct Bloque { uint8_t datos[TAM_BLOQUE]; };
// Definida aquí arriba (antes de cualquier función) porque el Arduino IDE
// inserta los prototipos de las funciones antes de la primera de ellas.
struct LecturaDHT { float t; float h; int intentos; char hora[17]; };

static QueueHandle_t colaLibres, colaLlenos;
static SemaphoreHandle_t semInicio;
static TaskHandle_t tareaMuestreo = nullptr;
static hw_timer_t* temporizador = nullptr;
static Bloque* bloqueDescarte = nullptr;     // se usa si se acaban los buffers
static int numBuffers = 0;

static char nombreSesion[17];
static volatile uint32_t rellenosTotal = 0;
static volatile uint32_t bloquesPerdidos = 0;
static volatile bool muestreoTerminado = true;

DHT dht(PIN_DHT, DHT11);
WiFiClient clienteRed;
PubSubClient mqtt(clienteRed);
char topInicio[64], topAudio[64], topDht[64], topFin[64], topEstado[64];

// ===================================================== NÚCLEO 1: MUESTREO
void IRAM_ATTR alTemporizador() {
  BaseType_t despertar = pdFALSE;
  vTaskNotifyGiveFromISR(tareaMuestreo, &despertar);
  if (despertar) portYIELD_FROM_ISR();
}

void tareaMuestreoFn(void*) {
  analogReadResolution(BITS_ADC);
  analogSetPinAttenuation(PIN_MIC, ADC_11db);     // rango ~0-3.3 V
  analogRead(PIN_MIC);

  temporizador = timerBegin(1000000);             // 1 tick = 1 µs; ISR en este núcleo
  timerAttachInterrupt(temporizador, &alTemporizador);
  timerAlarm(temporizador, 1000000 / FS, true, 0);
  timerStop(temporizador);

  for (;;) {
    xSemaphoreTake(semInicio, portMAX_DELAY);     // espera la orden del núcleo 0

    ulTaskNotifyTake(pdTRUE, 0);
    uint16_t ultimo = analogRead(PIN_MIC);
    uint32_t pendientes = 0;
    timerRestart(temporizador);
    timerStart(temporizador);

    for (uint32_t seq = 0; seq < N_BLOQUES; seq++) {
      Bloque* b;
      bool conBuffer = xQueueReceive(colaLibres, &b, 0) == pdTRUE;
      if (!conBuffer) { b = bloqueDescarte; bloquesPerdidos++; }
      uint16_t* m = (uint16_t*)(b->datos + CAB);
      uint16_t rell = 0;

      for (uint32_t i = 0; i < MUESTRAS_BLOQUE; ) {
        if (pendientes == 0) pendientes = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10));
        if (pendientes == 0) continue;
        if (pendientes > 1) { m[i++] = ultimo; rell++; }   // no debería pasar en este núcleo
        else { ultimo = analogRead(PIN_MIC); m[i++] = ultimo; }
        pendientes--;
      }

      rellenosTotal += rell;
      if (conBuffer) {
        memcpy(b->datos, nombreSesion, 16);
        memcpy(b->datos + 16, &seq, 4);           // ESP32 es little-endian
        memcpy(b->datos + 20, &rell, 2);
        xQueueSend(colaLlenos, &b, portMAX_DELAY);
      }
    }
    timerStop(temporizador);
    muestreoTerminado = true;
  }
}

// ===================================================== NÚCLEO 0: RED Y DHT11
bool conectarWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  Serial.printf("Conectando a WiFi '%s'", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_CLAVE);
  uint32_t t = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t > 20000) { Serial.println(" sin conexión"); return false; }
    Serial.print(".");
    vTaskDelay(pdMS_TO_TICKS(500));
  }
  Serial.printf(" OK, IP %s\n", WiFi.localIP().toString().c_str());
  return true;
}

bool conectarMQTT() {
  if (mqtt.connected()) return true;
  char id[48];
  snprintf(id, sizeof id, "%s-%lu", DISPOSITIVO, (unsigned long)millis());
  bool ok = mqtt.connect(id, MQTT_USUARIO, MQTT_CLAVE, topEstado, 0, true, "desconectado");
  if (ok) mqtt.publish(topEstado, "en_linea", true);
  else Serial.printf("MQTT: no se pudo conectar a %s:%u (estado %d)\n",
                     MQTT_BROKER, MQTT_PUERTO, mqtt.state());
  return ok;
}

bool reconectar() {
  return conectarWiFi() && conectarMQTT();
}

void selloUTC(char* sal) {
  time_t ahora = time(nullptr);
  struct tm t;
  gmtime_r(&ahora, &t);
  snprintf(sal, 17, "%04d%02d%02dT%02d%02d%02dZ", t.tm_year + 1900, t.tm_mon + 1,
           t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec);
}

LecturaDHT leerDHT() {
  LecturaDHT r = {NAN, NAN, 0, ""};
  for (int i = 0; i < 3; i++) {                   // hasta 3 intentos, 2 s entre ellos
    r.intentos = i + 1;
    if (dht.read(true)) {
      r.t = dht.readTemperature();
      r.h = dht.readHumidity();
      if (!isnan(r.t) && !isnan(r.h)) break;
    }
    vTaskDelay(pdMS_TO_TICKS(2100));
  }
  selloUTC(r.hora);
  return r;
}

void numeroJson(char* sal, size_t tam, float v) {
  if (isnan(v)) snprintf(sal, tam, "null");
  else snprintf(sal, tam, "%.1f", v);
}

void publicarDHT(const char* momento, const LecturaDHT& r) {
  char st[12], sh[12], json[200];
  numeroJson(st, sizeof st, r.t);
  numeroJson(sh, sizeof sh, r.h);
  snprintf(json, sizeof json,
    "{\"sesion\":\"%s\",\"momento\":\"%s\",\"hora_utc\":\"%s\",\"temp\":%s,\"hum\":%s,\"intentos\":%d}",
    nombreSesion, momento, r.hora, st, sh, r.intentos);
  if (!mqtt.connected()) reconectar();
  bool ok = mqtt.publish(topDht, json);
  Serial.printf("DHT11 %s: T=%s °C H=%s %% (%s)\n", momento, st, sh, ok ? "enviado" : "NO enviado");
}

bool enviarAudio(Bloque* b) {
  // Publicación en streaming: no hace falta un buffer MQTT de 8 KB
  if (!mqtt.beginPublish(topAudio, TAM_BLOQUE, false)) return false;
  size_t n = mqtt.write(b->datos, TAM_BLOQUE);
  return mqtt.endPublish() && n == TAM_BLOQUE;
}

void ejecutarSesion() {
  selloUTC(nombreSesion);
  Serial.printf("\n=== Sesión %s ===\n", nombreSesion);

  // 1) Temperatura y humedad ANTES del audio
  LecturaDHT antes = leerDHT();

  char json[420];
  snprintf(json, sizeof json,
    "{\"dispositivo\":\"%s\",\"sesion\":\"%s\",\"fs\":%lu,\"duracion_s\":%lu,"
    "\"muestras_bloque\":%lu,\"bloques\":%lu,\"bits_adc\":%d,"
    "\"formato_audio\":\"uint16 little-endian, valores 0-%d\","
    "\"firmware\":\"arduino-2nucleos-5min\"}",
    DISPOSITIVO, nombreSesion, (unsigned long)FS, (unsigned long)DURACION_S,
    (unsigned long)MUESTRAS_BLOQUE, (unsigned long)N_BLOQUES, BITS_ADC, (1 << BITS_ADC) - 1);
  mqtt.publish(topInicio, json);
  publicarDHT("antes", antes);

  // 2) Audio: el núcleo 1 graba; este núcleo solo publica
  rellenosTotal = 0; bloquesPerdidos = 0;
  muestreoTerminado = false;
  uint32_t t0 = millis(), tFin = 0;
  xSemaphoreGive(semInicio);
  Serial.printf("Grabando %lu s de audio...\n", (unsigned long)DURACION_S);

  uint32_t enviados = 0, noEnviados = 0, minLibres = numBuffers;
  uint32_t proximoReporte = t0 + 10000, ultimoReintento = 0;

  for (;;) {
    Bloque* b;
    if (xQueueReceive(colaLlenos, &b, pdMS_TO_TICKS(20)) == pdTRUE) {
      if (mqtt.connected() && enviarAudio(b)) enviados++;
      else noEnviados++;
      xQueueSend(colaLibres, &b, 0);           // devuelve el buffer al núcleo 1
    } else if (muestreoTerminado && uxQueueMessagesWaiting(colaLlenos) == 0) {
      break;
    }
    if (muestreoTerminado && tFin == 0) tFin = millis();

    uint32_t libres = uxQueueMessagesWaiting(colaLibres);
    if (libres < minLibres) minLibres = libres;

    if (!mqtt.connected() && millis() - ultimoReintento > 3000) {
      ultimoReintento = millis();
      Serial.println("Conexión perdida, reintentando (el audio sigue grabándose)");
      reconectar();
    }
    mqtt.loop();

    if ((int32_t)(millis() - proximoReporte) >= 0) {
      proximoReporte += 10000;
      Serial.printf("[%3lus] enviados=%lu no_enviados=%lu perdidos=%lu rellenos=%lu buffers_libres_min=%lu\n",
                    (unsigned long)((millis() - t0) / 1000), (unsigned long)enviados,
                    (unsigned long)noEnviados, (unsigned long)bloquesPerdidos,
                    (unsigned long)rellenosTotal, (unsigned long)minLibres);
    }
  }
  if (tFin == 0) tFin = millis();

  // 3) Temperatura y humedad DESPUÉS del audio
  LecturaDHT despues = leerDHT();
  publicarDHT("despues", despues);

  snprintf(json, sizeof json,
    "{\"sesion\":\"%s\",\"duracion_real_ms\":%lu,\"muestras_rellenadas\":%lu,"
    "\"bloques_perdidos_buffer\":%lu,\"bloques_no_enviados\":%lu,\"bloques_enviados\":%lu,"
    "\"buffers\":%d,\"buffers_libres_min\":%lu}",
    nombreSesion, (unsigned long)(tFin - t0), (unsigned long)rellenosTotal,
    (unsigned long)bloquesPerdidos, (unsigned long)noEnviados, (unsigned long)enviados,
    numBuffers, (unsigned long)minLibres);
  if (!mqtt.connected()) reconectar();
  mqtt.publish(topFin, json);
  Serial.printf("Fin de sesión: %s\n", json);
}

void tareaRedFn(void*) {
  dht.begin();
  vTaskDelay(pdMS_TO_TICKS(2000));                 // el DHT11 necesita ~1 s tras encender

  while (!conectarWiFi()) vTaskDelay(pdMS_TO_TICKS(5000));
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");   // hora UTC para nombrar sesiones
  struct tm t;
  if (!getLocalTime(&t, 10000)) Serial.println("NTP no disponible: se usará el reloj interno");

  mqtt.setServer(MQTT_BROKER, MQTT_PUERTO);
  mqtt.setKeepAlive(60);
  mqtt.setSocketTimeout(10);

  for (;;) {
    if (!reconectar()) {
      Serial.println("Sin conexión con el broker; reintento en 10 s");
      vTaskDelay(pdMS_TO_TICKS(10000));
      continue;
    }
    ejecutarSesion();

    if (PAUSA_S > 0) {
      Serial.printf("Pausa de %lu s\n", (unsigned long)PAUSA_S);
      uint32_t t0 = millis();
      while (millis() - t0 < PAUSA_S * 1000UL) {   // mantiene viva la conexión MQTT
        if (mqtt.connected()) mqtt.loop();
        vTaskDelay(pdMS_TO_TICKS(100));
      }
    }
  }
}

// ===================================================== ARRANQUE
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\nEstación ESP32: DHT11 + KY-037 -> MQTT (2 núcleos, sesiones de 5 min)");

  snprintf(topInicio, sizeof topInicio, "estacion/%s/inicio", DISPOSITIVO);
  snprintf(topAudio,  sizeof topAudio,  "estacion/%s/audio",  DISPOSITIVO);
  snprintf(topDht,    sizeof topDht,    "estacion/%s/dht",    DISPOSITIVO);
  snprintf(topFin,    sizeof topFin,    "estacion/%s/fin",    DISPOSITIVO);
  snprintf(topEstado, sizeof topEstado, "estacion/%s/estado", DISPOSITIVO);

  colaLibres = xQueueCreate(MAX_BUFFERS, sizeof(Bloque*));
  colaLlenos = xQueueCreate(MAX_BUFFERS, sizeof(Bloque*));
  semInicio = xSemaphoreCreateBinary();
  bloqueDescarte = (Bloque*)malloc(sizeof(Bloque));
  for (int i = 0; i < MAX_BUFFERS; i++) {
    if (ESP.getFreeHeap() < 110000) break;         // deja memoria para WiFi y MQTT
    Bloque* b = (Bloque*)malloc(sizeof(Bloque));
    if (!b) break;
    xQueueSend(colaLibres, &b, 0);
    numBuffers++;
  }
  Serial.printf("Buffers de audio: %d (%.1f s de colchón). Memoria libre: %lu bytes\n",
                numBuffers, numBuffers * (float)MUESTRAS_BLOQUE / FS, (unsigned long)ESP.getFreeHeap());

  xTaskCreatePinnedToCore(tareaMuestreoFn, "muestreo", 4096, nullptr,
                          configMAX_PRIORITIES - 1, &tareaMuestreo, 1);
  xTaskCreatePinnedToCore(tareaRedFn, "red", 8192, nullptr, 2, nullptr, 0);
}

void loop() {
  vTaskDelete(nullptr);
}
