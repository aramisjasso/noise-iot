/*
  esp32_mqtt_5min_5.ino — ESP32 (Arduino, 2 núcleos) -> broker MQTT (Mosquitto, red local)

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
  (El formato de los mensajes NO cambió respecto a la versión anterior.)

  Tópicos (BASE = estacion/<DISPOSITIVO>):
    BASE/inicio   JSON    metadatos de la sesión
    BASE/dht      JSON    {"sesion","momento":"antes"|"despues","temp","hum","intentos"}
    BASE/audio    BIN     sesion(16 bytes) + seq(uint32) + rellenos(uint16) + muestras uint16 LE
    BASE/fin      JSON    resumen (muestras rellenadas, bloques perdidos...)
    BASE/estado   texto   "en_linea" / "desconectado"  (retenido)

  CAMBIOS RESPECTO A LA VERSIÓN _4:
    - El descarte de bloques pendientes ahora cuenta 60 s desde que TERMINA la grabación
      (o desde el último envío exitoso, lo que sea más reciente), no solo desde el último éxito.
    - Reconexiones con timeouts cortos durante la grabación (el colchón sin PSRAM es de ~8 s).
    - Keepalive MQTT de 15 s: una conexión "a medias" se detecta en segundos, no en 60 s.
    - WiFi: sin escrituras en flash (persistent=false), auto-reconexión y sin llamar a
      WiFi.begin() en cada intento.
    - Hora: no se acepta la hora 1970; si NTP falla, el nombre de sesión es "SINHORA<segundos>".
    - Mensajes inicio/dht/fin con reintentos.
    - Verificación de memoria/colas al arrancar (si falla, reinicia en vez de colgarse).
    - duracion_real_ms ahora es la duración real del muestreo (medida en el núcleo 1).
    - DHT11 sin espera inútil tras el último intento. Pilas más grandes + monitoreo de pila.

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
const char* WIFI_SSID   = "Redes";
const char* WIFI_CLAVE  = "12345678";

const char* MQTT_BROKER  = "158.97.122.248";   // IP de la PC u Orange Pi con Mosquitto (sin espacios)
const uint16_t MQTT_PUERTO = 1883;
const char* MQTT_USUARIO = nullptr;          // o "usuario" si Mosquitto pide clave (recomendado)
const char* MQTT_CLAVE   = nullptr;

const char* DISPOSITIVO = "esp32-04";         // ÚNICO por placa

const int PIN_DHT  = 4;                       // DATA del DHT11
const int PIN_MIC  = 34;                      // A0 del KY-037 (ADC1: GPIO 32-39). Alimenta el KY-037 a 3.3 V
const int BITS_ADC = 12;                      // máximo del ESP32: 0-4095

// LEDs (cada uno con una resistencia de 220-330 Ω en serie hacia GND)
const int LED_ENCENDIDO = 22;                 // encendido mientras el ESP32 esté funcionando
const int LED_MEDICION  = 23;                 // encendido mientras se toman datos

// false: por el Monitor Serie solo se imprime la MAC.
// true : se imprimen también los mensajes de diagnóstico (WiFi, envíos, DHT11...).
constexpr bool IMPRIMIR_DIAGNOSTICO = true;

constexpr uint32_t FS              = 8000;    // Hz
constexpr uint32_t DURACION_S      = 300;     // 5 min de audio
constexpr uint32_t PAUSA_S         = 300;     // 5 min sin grabar entre sesiones (0 = continuo)
constexpr uint32_t MUESTRAS_BLOQUE = 4000;    // 0.5 s por mensaje MQTT (8 KB)
constexpr int      MAX_BUFFERS     = 16;      // en RAM interna: ~8 s de colchón
constexpr int      MAX_BUFFERS_PSRAM = 240;   // si la placa tiene PSRAM: 2 min de colchón

constexpr uint32_t TIMEOUT_WIFI_INICIAL_MS = 20000;  // al arrancar / entre sesiones
constexpr uint32_t TIMEOUT_WIFI_SESION_MS  = 1500;   // durante la grabación (no bloquear)
constexpr uint32_t DESCARTE_TRAS_MS        = 60000;  // sin red tras terminar de grabar
// =======================================================================

constexpr uint32_t N_BLOQUES = DURACION_S * FS / MUESTRAS_BLOQUE;   // 600
static_assert((DURACION_S * FS) % MUESTRAS_BLOQUE == 0, "DURACION_S*FS debe ser múltiplo del bloque");
constexpr size_t CAB = 16 + 4 + 2;                                  // sesion + seq + rellenos
constexpr size_t TAM_BLOQUE = CAB + 2 * MUESTRAS_BLOQUE;

// Los mensajes de diagnóstico solo salen si IMPRIMIR_DIAGNOSTICO = true
#define LOG(x)      do { if (IMPRIMIR_DIAGNOSTICO) Serial.print(x); } while (0)
#define LOGLN(x)    do { if (IMPRIMIR_DIAGNOSTICO) Serial.println(x); } while (0)
#define LOGF(...)   do { if (IMPRIMIR_DIAGNOSTICO) Serial.printf(__VA_ARGS__); } while (0)

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
static volatile uint32_t tInicioMuestreo = 0, tFinMuestreo = 0;   // medidos en el núcleo 1

DHT dht(PIN_DHT, DHT11);
WiFiClient clienteRed;
PubSubClient mqtt(clienteRed);
char topInicio[64], topAudio[64], topDht[64], topFin[64], topEstado[64];

// Declaraciones adelantadas (valores por defecto aquí, no en la definición)
bool conectarWiFi(uint32_t timeoutMs = TIMEOUT_WIFI_INICIAL_MS);
bool reconectar(uint32_t timeoutWifiMs = TIMEOUT_WIFI_INICIAL_MS);

// Error fatal de arranque: avisa y reinicia en vez de colgarse
static void errorFatal(const char* msg) {
  for (;;) {
    Serial.println(msg);
    Serial.println("Reiniciando en 5 s...");
    delay(5000);
    ESP.restart();
  }
}

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
    tInicioMuestreo = millis();

    for (uint32_t seq = 0; seq < N_BLOQUES; seq++) {
      Bloque* b;
      bool conBuffer = xQueueReceive(colaLibres, &b, 0) == pdTRUE;
      if (!conBuffer) { b = bloqueDescarte; bloquesPerdidos++; }
      uint16_t* m = (uint16_t*)(b->datos + CAB);
      uint16_t rell = 0;

      for (uint32_t i = 0; i < MUESTRAS_BLOQUE; ) {
        if (pendientes == 0) pendientes = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10));
        if (pendientes == 0) continue;
        if (pendientes > 1) { m[i++] = ultimo; rell++; }   // se perdió un tick: repite la última muestra
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
    tFinMuestreo = millis();
    muestreoTerminado = true;
  }
}

// ===================================================== NÚCLEO 0: RED Y DHT11
static bool wifiIniciado = false;
static uint32_t tUltimoBegin = 0;

// Espera hasta timeoutMs a que haya WiFi. WiFi.begin() solo se llama la primera vez
// o si pasaron 20 s sin conseguir conexión (el ESP32 ya se auto-reconecta solo).
bool conectarWiFi(uint32_t timeoutMs) {
  if (WiFi.status() == WL_CONNECTED) return true;

  if (!wifiIniciado || millis() - tUltimoBegin > 20000) {
    LOGF("Conectando a WiFi '%s'...\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setAutoReconnect(true);
    if (wifiIniciado) WiFi.disconnect(false);
    WiFi.begin(WIFI_SSID, WIFI_CLAVE);
    wifiIniciado = true;
    tUltimoBegin = millis();
  }

  uint32_t t = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - t > timeoutMs) { LOGLN("WiFi: sin conexión (por ahora)"); return false; }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
  LOGF("WiFi OK, IP %s\n", WiFi.localIP().toString().c_str());
  return true;
}

bool conectarMQTT() {
  if (mqtt.connected()) return true;
  char id[48];
  snprintf(id, sizeof id, "%s-%lu", DISPOSITIVO, (unsigned long)millis());
  bool ok = mqtt.connect(id, MQTT_USUARIO, MQTT_CLAVE, topEstado, 0, true, "desconectado");
  if (ok) {
    // Desactiva Nagle: sin esto, el último pedazo de cada bloque espera el ACK del
    // servidor (hasta 200 ms) y el envío se va atrasando.
    clienteRed.setNoDelay(true);
    mqtt.publish(topEstado, "en_linea", true);
  }
  else LOGF("MQTT: no se pudo conectar a %s:%u (estado %d)\n",
                     MQTT_BROKER, MQTT_PUERTO, mqtt.state());
  return ok;
}

bool reconectar(uint32_t timeoutWifiMs) {
  return conectarWiFi(timeoutWifiMs) && conectarMQTT();
}

// ¿El reloj ya se sincronizó con NTP? (si no, time() da fechas de 1970)
static bool horaValida() { return time(nullptr) > 1700000000; }

void selloUTC(char* sal) {
  if (!horaValida()) {                             // fallback único dentro de este arranque
    snprintf(sal, 17, "SINHORA%09lu", (unsigned long)(millis() / 1000));
    return;
  }
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
    if (i < 2) vTaskDelay(pdMS_TO_TICKS(2100));   // sin espera tras el último intento
  }
  selloUTC(r.hora);
  return r;
}

void numeroJson(char* sal, size_t tam, float v) {
  if (isnan(v)) snprintf(sal, tam, "null");
  else snprintf(sal, tam, "%.1f", v);
}

// Publica un mensaje corto con hasta 3 intentos (reconectando si hace falta).
// Solo se usa cuando NO se está grabando audio, así que puede esperar con calma.
bool publicarSeguro(const char* topico, const char* payload) {
  for (int i = 0; i < 3; i++) {
    if (!mqtt.connected() && !reconectar(TIMEOUT_WIFI_INICIAL_MS)) {
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    if (mqtt.publish(topico, payload)) { mqtt.loop(); return true; }
    clienteRed.stop();                             // fuerza reconexión limpia y reintenta
  }
  return false;
}

void publicarDHT(const char* momento, const LecturaDHT& r) {
  char st[12], sh[12], json[200];
  numeroJson(st, sizeof st, r.t);
  numeroJson(sh, sizeof sh, r.h);
  snprintf(json, sizeof json,
    "{\"sesion\":\"%s\",\"momento\":\"%s\",\"hora_utc\":\"%s\",\"temp\":%s,\"hum\":%s,\"intentos\":%d}",
    nombreSesion, momento, r.hora, st, sh, r.intentos);
  bool ok = publicarSeguro(topDht, json);
  LOGF("DHT11 %s: T=%s °C H=%s %% (%s)\n", momento, st, sh, ok ? "enviado" : "NO enviado");
}

bool enviarAudio(Bloque* b) {
  // Publicación en streaming: no hace falta un buffer MQTT de 8 KB
  if (!mqtt.beginPublish(topAudio, TAM_BLOQUE, false)) return false;
  size_t n = mqtt.write(b->datos, TAM_BLOQUE);
  return mqtt.endPublish() && n == TAM_BLOQUE;
}

void ejecutarSesion() {
  digitalWrite(LED_MEDICION, HIGH);              // LED de medición: inicia la toma de datos

  // Sin hora válida los nombres de sesión se repetirían tras un reinicio: espera NTP un rato
  if (!horaValida()) {
    LOGLN("Esperando hora NTP...");
    struct tm tmp;
    getLocalTime(&tmp, 15000);
  }
  selloUTC(nombreSesion);
  LOGF("\n=== Sesión %s ===\n", nombreSesion);

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
  if (!publicarSeguro(topInicio, json)) LOGLN("AVISO: no se pudo enviar el mensaje de inicio");
  publicarDHT("antes", antes);

  // Antes de empezar a grabar, asegura la conexión (aquí aún se puede esperar con calma)
  if (!mqtt.connected()) reconectar(TIMEOUT_WIFI_INICIAL_MS);

  // 2) Audio: el núcleo 1 graba; este núcleo solo publica
  rellenosTotal = 0; bloquesPerdidos = 0;
  muestreoTerminado = false;
  uint32_t t0 = millis(), tFin = 0;
  xSemaphoreGive(semInicio);
  LOGF("Grabando %lu s de audio...\n", (unsigned long)DURACION_S);

  uint32_t enviados = 0, noEnviados = 0, fallos = 0, reconexiones = 0;
  uint32_t minLibres = numBuffers, tEnvioMax = 0, tEnvioMaxTotal = 0, tEnvioSuma = 0, nEnvio = 0;
  uint32_t proximoReporte = t0 + 10000, ultimoReintento = 0, ultimoExito = millis();
  Bloque* pendiente = nullptr;      // bloque que falló: se reintenta en vez de tirarlo

  for (;;) {
    if (!pendiente && xQueueReceive(colaLlenos, &pendiente, pdMS_TO_TICKS(20)) != pdTRUE) {
      pendiente = nullptr;
      if (muestreoTerminado && uxQueueMessagesWaiting(colaLlenos) == 0) break;
    }
    if (muestreoTerminado && tFin == 0) tFin = millis();

    if (pendiente && mqtt.connected()) {
      uint32_t ti = millis();
      bool ok = enviarAudio(pendiente);
      uint32_t dt = millis() - ti;
      if (ok) {
        enviados++;
        tEnvioSuma += dt; nEnvio++;
        if (dt > tEnvioMax) tEnvioMax = dt;
        if (dt > tEnvioMaxTotal) tEnvioMaxTotal = dt;
        ultimoExito = millis();
        xQueueSend(colaLibres, &pendiente, 0);   // devuelve el buffer al núcleo 1
        pendiente = nullptr;
      } else {
        fallos++;
        LOGF("Fallo al enviar un bloque (%lu ms, estado MQTT %d). Se reintentará.\n",
                      (unsigned long)dt, mqtt.state());
        clienteRed.stop();                        // fuerza una reconexión limpia
      }
    }

    // Si el broker no responde ni después de terminar de grabar, se descarta lo que falta.
    // Los 60 s se cuentan desde el fin de la grabación o desde el último envío exitoso
    // (lo más reciente), para no tirar el colchón apenas termina el muestreo.
    if (muestreoTerminado && tFin != 0) {
      uint32_t base = ((int32_t)(tFin - ultimoExito) > 0) ? tFin : ultimoExito;
      if (millis() - base > DESCARTE_TRAS_MS) {
        Bloque* b;
        if (pendiente) { noEnviados++; xQueueSend(colaLibres, &pendiente, 0); pendiente = nullptr; }
        while (xQueueReceive(colaLlenos, &b, 0) == pdTRUE) { noEnviados++; xQueueSend(colaLibres, &b, 0); }
        LOGLN("Sin conexión durante 60 s tras la grabación: se descartan los bloques pendientes");
        break;
      }
    }

    uint32_t libres = uxQueueMessagesWaiting(colaLibres);
    if (libres < minLibres) minLibres = libres;

    if (!mqtt.connected()) {
      if (millis() - ultimoReintento > 2000) {
        ultimoReintento = millis();
        LOGLN("Conexión perdida, reintentando (el audio sigue grabándose)");
        // timeouts cortos: el colchón sin PSRAM es de solo ~8 s
        if (reconectar(TIMEOUT_WIFI_SESION_MS)) reconexiones++;
      } else {
        vTaskDelay(pdMS_TO_TICKS(5));
      }
    }
    mqtt.loop();

    if ((int32_t)(millis() - proximoReporte) >= 0) {
      proximoReporte += 10000;
      LOGF("[%3lus] enviados=%lu en_cola=%lu perdidos=%lu fallos=%lu reconexiones=%lu "
                    "envio_prom=%lums envio_max=%lums buffers_libres_min=%lu/%d RSSI=%ddBm heap=%lu "
                    "pila_red_libre=%lu pila_muestreo_libre=%lu\n",
                    (unsigned long)((millis() - t0) / 1000), (unsigned long)enviados,
                    (unsigned long)uxQueueMessagesWaiting(colaLlenos), (unsigned long)bloquesPerdidos,
                    (unsigned long)fallos, (unsigned long)reconexiones,
                    (unsigned long)(nEnvio ? tEnvioSuma / nEnvio : 0), (unsigned long)tEnvioMax,
                    (unsigned long)minLibres, numBuffers, (int)WiFi.RSSI(),
                    (unsigned long)ESP.getFreeHeap(),
                    (unsigned long)uxTaskGetStackHighWaterMark(nullptr),
                    (unsigned long)uxTaskGetStackHighWaterMark(tareaMuestreo));
      tEnvioSuma = 0; nEnvio = 0; tEnvioMax = 0;
    }
  }
  if (tFin == 0) tFin = millis();

  // 3) Temperatura y humedad DESPUÉS del audio
  LecturaDHT despues = leerDHT();
  publicarDHT("despues", despues);

  uint32_t duracionReal = tFinMuestreo - tInicioMuestreo;   // medida en el núcleo 1
  snprintf(json, sizeof json,
    "{\"sesion\":\"%s\",\"duracion_real_ms\":%lu,\"muestras_rellenadas\":%lu,"
    "\"bloques_perdidos_buffer\":%lu,\"bloques_no_enviados\":%lu,\"bloques_enviados\":%lu,"
    "\"fallos_envio\":%lu,\"reconexiones\":%lu,\"envio_max_ms\":%lu,"
    "\"buffers\":%d,\"buffers_libres_min\":%lu}",
    nombreSesion, (unsigned long)duracionReal, (unsigned long)rellenosTotal,
    (unsigned long)bloquesPerdidos, (unsigned long)noEnviados, (unsigned long)enviados,
    (unsigned long)fallos, (unsigned long)reconexiones, (unsigned long)tEnvioMaxTotal,
    numBuffers, (unsigned long)minLibres);
  if (!publicarSeguro(topFin, json)) LOGLN("AVISO: no se pudo enviar el resumen final");
  LOGF("Fin de sesión: %s\n", json);
  digitalWrite(LED_MEDICION, LOW);               // LED de medición: terminó la toma de datos
}

void tareaRedFn(void*) {
  dht.begin();
  vTaskDelay(pdMS_TO_TICKS(2000));                 // el DHT11 necesita ~1 s tras encender

  while (!conectarWiFi(TIMEOUT_WIFI_INICIAL_MS)) vTaskDelay(pdMS_TO_TICKS(2000));
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");   // hora UTC para nombrar sesiones
  struct tm t;
  if (!getLocalTime(&t, 10000)) LOGLN("NTP no disponible por ahora; se reintentará antes de cada sesión");

  mqtt.setServer(MQTT_BROKER, MQTT_PUERTO);
  mqtt.setKeepAlive(15);                           // detecta conexiones "a medias" en segundos
  mqtt.setSocketTimeout(3);                        // no bloquear mucho (colchón sin PSRAM ~8 s)
  mqtt.setBufferSize(1024);

  for (;;) {
    if (!reconectar(TIMEOUT_WIFI_INICIAL_MS)) {
      LOGLN("Sin conexión con el broker; reintento en 10 s");
      vTaskDelay(pdMS_TO_TICKS(10000));
      continue;
    }
    ejecutarSesion();

    if (PAUSA_S > 0) {
      LOGF("Pausa de %lu s\n", (unsigned long)PAUSA_S);
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
  pinMode(LED_ENCENDIDO, OUTPUT);
  pinMode(LED_MEDICION, OUTPUT);
  digitalWrite(LED_ENCENDIDO, HIGH);             // LED de encendido: fijo mientras funcione
  digitalWrite(LED_MEDICION, LOW);

  Serial.begin(115200);
  delay(500);

  // No guardar credenciales en flash en cada begin(): una escritura en flash detiene
  // ambos núcleos unos milisegundos y se notaría como un hueco en el audio.
  WiFi.persistent(false);

  // La MAC se lee después de activar el WiFi (antes de eso puede salir en ceros)
  WiFi.mode(WIFI_STA);
  Serial.println();
  Serial.println(WiFi.macAddress());
  LOGLN("\nEstación ESP32: DHT11 + KY-037 -> MQTT (2 núcleos, sesiones de 5 min)");

  snprintf(topInicio, sizeof topInicio, "estacion/%s/inicio", DISPOSITIVO);
  snprintf(topAudio,  sizeof topAudio,  "estacion/%s/audio",  DISPOSITIVO);
  snprintf(topDht,    sizeof topDht,    "estacion/%s/dht",    DISPOSITIVO);
  snprintf(topFin,    sizeof topFin,    "estacion/%s/fin",    DISPOSITIVO);
  snprintf(topEstado, sizeof topEstado, "estacion/%s/estado", DISPOSITIVO);

  // Con PSRAM (placas WROVER; activar en Herramientas > PSRAM: Enabled) se usan
  // 240 buffers = 2 min de colchón ante cortes de red. Sin PSRAM, 16 buffers = 8 s.
  bool conPsram = psramFound();
  int maxBuf = conPsram ? MAX_BUFFERS_PSRAM : MAX_BUFFERS;
  colaLibres = xQueueCreate(maxBuf, sizeof(Bloque*));
  colaLlenos = xQueueCreate(maxBuf, sizeof(Bloque*));
  semInicio = xSemaphoreCreateBinary();
  if (!colaLibres || !colaLlenos || !semInicio) errorFatal("ERROR: no se pudieron crear las colas/semáforo");

  bloqueDescarte = (Bloque*)malloc(sizeof(Bloque));
  if (!bloqueDescarte) errorFatal("ERROR: sin memoria para el bloque de descarte");

  for (int i = 0; i < maxBuf; i++) {
    Bloque* b;
    if (conPsram) {
      b = (Bloque*)ps_malloc(sizeof(Bloque));
    } else {
      if (ESP.getFreeHeap() < 120000) break;       // deja memoria para WiFi, MQTT y las pilas de las tareas
      b = (Bloque*)malloc(sizeof(Bloque));
    }
    if (!b) break;
    xQueueSend(colaLibres, &b, 0);
    numBuffers++;
  }
  if (numBuffers == 0) errorFatal("ERROR: no se pudo reservar ningún buffer de audio");
  LOGF("PSRAM: %s\n", conPsram ? "sí" : "no");
  LOGF("Buffers de audio: %d (%.1f s de colchón). Memoria libre: %lu bytes\n",
                numBuffers, numBuffers * (float)MUESTRAS_BLOQUE / FS, (unsigned long)ESP.getFreeHeap());

  xTaskCreatePinnedToCore(tareaMuestreoFn, "muestreo", 6144, nullptr,
                          configMAX_PRIORITIES - 1, &tareaMuestreo, 1);
  xTaskCreatePinnedToCore(tareaRedFn, "red", 10240, nullptr, 2, nullptr, 0);
}

void loop() {
  vTaskDelete(nullptr);
}
