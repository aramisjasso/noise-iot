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

  LEDs: GPIO 25 = encendido, GPIO 26 = tomando datos. La MAC del ESP32 se imprime al
  arrancar y se envía en los mensajes "inicio", "dht" y "fin".
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

// Algunas placas (antena mal adaptada, regulador débil) solo se conectan bajando la
// potencia de transmisión. Si tu placa ve la red pero no logra conectarse, pon true.
constexpr bool REDUCIR_POTENCIA_TX = false;

// LEDs (cada uno con una resistencia de 220-330 ohm en serie, cátodo a GND)
const int PIN_LED_ENCENDIDO = 22;            // encendido siempre que el ESP32 esté funcionando
const int PIN_LED_CAPTURA   = 23;            // encendido mientras se toman datos (DHT11 + audio)

const int PIN_DHT  = 4;                       // DATA del DHT11
const int PIN_MIC  = 34;                      // A0 del KY-037 (ADC1: GPIO 32-39)
const int BITS_ADC = 12;                      // máximo del ESP32: 0-4095

constexpr uint32_t FS              = 8000;    // Hz
constexpr uint32_t DURACION_S      = 300;     // 5 min de audio
constexpr uint32_t PAUSA_S         = 300;     // 5 min sin grabar entre sesiones (0 = continuo)
constexpr uint32_t MUESTRAS_BLOQUE = 4000;    // 0.5 s por mensaje MQTT (8 KB)
constexpr int      MAX_BUFFERS     = 16;      // en RAM interna: ~8 s de colchón
constexpr int      MAX_BUFFERS_PSRAM = 240;   // si la placa tiene PSRAM: 2 min de colchón
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
static char macTexto[18];                   // MAC del ESP32, p. ej. "24:6F:28:AB:CD:EF"
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
// Motivo de la última desconexión (lo llena el evento de WiFi)
static volatile int motivoDesconexion = 0;
static volatile bool asociado = false;     // true si el celular aceptó al ESP32 (aunque falte la IP)

const char* textoMotivo(int r) {
  switch (r) {
    case 0:   return "la red no respondió (señal débil, antena o alimentación de la placa)";
    case 2: case 15: case 204:
              return "contraseña incorrecta (o tiempo de autenticación agotado)";
    case 5:   return "la red tiene demasiados dispositivos conectados";
    case 201: return "no se encuentra la red (¿nombre exacto? ¿es de 2.4 GHz? ¿está cerca?)";
    case 202: return "autenticación rechazada (contraseña o seguridad WPA3)";
    case 203: return "la red rechazó la conexión (¿límite de dispositivos o filtro MAC?)";
    case 205: return "falló la conexión";
    case 210: case 211:
              return "seguridad incompatible: configura la red como WPA2";
    default:  return "otro motivo (ver código)";
  }
}

const char* textoSeguridad(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN:          return "abierta";
    case WIFI_AUTH_WPA_PSK:       return "WPA";
    case WIFI_AUTH_WPA2_PSK:      return "WPA2";
    case WIFI_AUTH_WPA_WPA2_PSK:  return "WPA/WPA2";
    case WIFI_AUTH_WPA3_PSK:      return "WPA3 (puede fallar)";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2/WPA3";
    default:                      return "otra";
  }
}

// Muestra las redes que ve este ESP32: si la tuya no aparece, es de 5 GHz o está lejos
void escanearRedes() {
  Serial.println("Buscando redes WiFi (el ESP32 solo ve redes de 2.4 GHz)...");
  int n = WiFi.scanNetworks();
  bool encontrada = false;
  for (int i = 0; i < n; i++) {
    bool esLaMia = WiFi.SSID(i) == WIFI_SSID;
    if (esLaMia) encontrada = true;
    Serial.printf("  %s %-24s canal %2ld  %4ld dBm  %s\n", esLaMia ? "->" : "  ",
                  WiFi.SSID(i).c_str(), (long)WiFi.channel(i), (long)WiFi.RSSI(i),
                  textoSeguridad(WiFi.encryptionType(i)));
  }
  if (n <= 0) Serial.println("  No se encontró ninguna red (¿antena o alimentación de la placa?)");
  if (!encontrada)
    Serial.printf("  La red '%s' NO aparece. Si es el hotspot del celular, actívale la banda de "
                  "2.4 GHz (\"Ampliar compatibilidad\" o \"Banda 2.4 GHz\").\n", WIFI_SSID);
  WiFi.scanDelete();
}

bool conectarWiFi() {
  if (WiFi.status() == WL_CONNECTED) return true;

  static bool primeraVez = true;
  if (primeraVez) {
    primeraVez = false;
    WiFi.persistent(false);              // no usar ni guardar redes viejas de la memoria flash
    WiFi.mode(WIFI_STA);
    WiFi.disconnect(true, true);         // borra la configuración WiFi anterior de esta placa
    vTaskDelay(pdMS_TO_TICKS(300));
    WiFi.mode(WIFI_STA);
    WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t info) {
      motivoDesconexion = info.wifi_sta_disconnected.reason;
      asociado = false;
    }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
    WiFi.onEvent([](WiFiEvent_t, WiFiEventInfo_t) {
      asociado = true;
    }, ARDUINO_EVENT_WIFI_STA_CONNECTED);
    escanearRedes();
  }

  for (int intento = 1; intento <= 3; intento++) {
    // Si el primer intento no tuvo respuesta, los siguientes bajan la potencia de
    // transmisión: muchas placas escuchan bien al router pero transmiten mal.
    bool potenciaBaja = REDUCIR_POTENCIA_TX || intento >= 2;
    Serial.printf("Conectando a WiFi '%s' (intento %d/3%s)", WIFI_SSID, intento,
                  potenciaBaja ? ", potencia TX reducida" : "");
    WiFi.disconnect();
    vTaskDelay(pdMS_TO_TICKS(300));
    motivoDesconexion = 0;             // se limpia DESPUÉS de desconectar (si no, queda el código 8)
    asociado = false;
    WiFi.begin(WIFI_SSID, WIFI_CLAVE);
    WiFi.setSleep(false);
    if (potenciaBaja) WiFi.setTxPower(WIFI_POWER_8_5dBm);

    uint32_t t = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - t < 15000) {
      Serial.print(".");
      vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (WiFi.status() == WL_CONNECTED) {
      WiFi.setAutoReconnect(true);
      Serial.printf(" OK, IP %s  señal %d dBm  canal %ld\n", WiFi.localIP().toString().c_str(),
                    (int)WiFi.RSSI(), (long)WiFi.channel());
      if (potenciaBaja && !REDUCIR_POTENCIA_TX)
        Serial.println("  Esta placa necesitó potencia TX reducida: pon REDUCIR_POTENCIA_TX = true.");
      return true;
    }
    int m = motivoDesconexion;
    if (asociado)
      Serial.println(" falló: el celular aceptó al ESP32 pero no le dio IP (DHCP). "
                     "Desconecta otros dispositivos del hotspot o reinícialo.");
    else
      Serial.printf(" falló: %s (código %d)\n", textoMotivo(m), m);
    if (m == 2 || m == 15 || m == 204 || m == 202)
      Serial.println("  Revisa WIFI_CLAVE: mayúsculas, espacios y que el hotspot use WPA2.");
    vTaskDelay(pdMS_TO_TICKS(1000));
  }
  Serial.println("No se pudo conectar al WiFi. Si la red aparece en la lista y la clave es "
                 "correcta, prueba REDUCIR_POTENCIA_TX = true o cambia el cable USB.");
  return false;
}

// MAC de la placa (la de fábrica, grabada en el chip; es la misma que usa el WiFi)
void leerMAC() {
  uint64_t mac = ESP.getEfuseMac();
  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = (mac >> (8 * i)) & 0xFF;
  snprintf(macTexto, sizeof macTexto, "%02X:%02X:%02X:%02X:%02X:%02X",
           b[0], b[1], b[2], b[3], b[4], b[5]);
}

bool conectarMQTT() {
  if (mqtt.connected()) return true;
  char id[48];
  snprintf(id, sizeof id, "%s-%lu", DISPOSITIVO, (unsigned long)millis());
  bool ok = mqtt.connect(id, MQTT_USUARIO, MQTT_CLAVE, topEstado, 0, true, "desconectado");
  if (ok) {
    // Desactiva Nagle: sin esto, el último pedazo de cada bloque espera el ACK del
    // servidor (hasta 200 ms) y por internet el envío se va atrasando hasta perder datos.
    clienteRed.setNoDelay(true);
    mqtt.publish(topEstado, "en_linea", true);
  }
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
    "{\"sesion\":\"%s\",\"mac\":\"%s\",\"momento\":\"%s\",\"hora_utc\":\"%s\",\"temp\":%s,\"hum\":%s,\"intentos\":%d}",
    nombreSesion, macTexto, momento, r.hora, st, sh, r.intentos);
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

  digitalWrite(PIN_LED_CAPTURA, HIGH);             // LED: tomando datos

  // 1) Temperatura y humedad ANTES del audio
  LecturaDHT antes = leerDHT();

  char json[420];
  snprintf(json, sizeof json,
    "{\"dispositivo\":\"%s\",\"mac\":\"%s\",\"sesion\":\"%s\",\"fs\":%lu,\"duracion_s\":%lu,"
    "\"muestras_bloque\":%lu,\"bloques\":%lu,\"bits_adc\":%d,"
    "\"formato_audio\":\"uint16 little-endian, valores 0-%d\","
    "\"firmware\":\"arduino-2nucleos-5min\"}",
    DISPOSITIVO, macTexto, nombreSesion, (unsigned long)FS, (unsigned long)DURACION_S,
    (unsigned long)MUESTRAS_BLOQUE, (unsigned long)N_BLOQUES, BITS_ADC, (1 << BITS_ADC) - 1);
  mqtt.publish(topInicio, json);
  publicarDHT("antes", antes);

  // 2) Audio: el núcleo 1 graba; este núcleo solo publica
  rellenosTotal = 0; bloquesPerdidos = 0;
  muestreoTerminado = false;
  uint32_t t0 = millis(), tFin = 0;
  xSemaphoreGive(semInicio);
  Serial.printf("Grabando %lu s de audio...\n", (unsigned long)DURACION_S);

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
        Serial.printf("Fallo al enviar un bloque (%lu ms, estado MQTT %d). Se reintentará.\n",
                      (unsigned long)dt, mqtt.state());
        clienteRed.stop();                        // fuerza una reconexión limpia
      }
    }

    // Si el broker no responde ni después de terminar de grabar, se descarta lo que falta
    if (muestreoTerminado && millis() - ultimoExito > 60000) {
      Bloque* b;
      if (pendiente) { noEnviados++; xQueueSend(colaLibres, &pendiente, 0); pendiente = nullptr; }
      while (xQueueReceive(colaLlenos, &b, 0) == pdTRUE) { noEnviados++; xQueueSend(colaLibres, &b, 0); }
      Serial.println("Sin conexión durante 60 s: se descartan los bloques pendientes");
      break;
    }

    uint32_t libres = uxQueueMessagesWaiting(colaLibres);
    if (libres < minLibres) minLibres = libres;

    if (!mqtt.connected()) {
      if (millis() - ultimoReintento > 2000) {
        ultimoReintento = millis();
        Serial.println("Conexión perdida, reintentando (el audio sigue grabándose)");
        if (reconectar()) reconexiones++;
      } else {
        vTaskDelay(pdMS_TO_TICKS(5));
      }
    }
    mqtt.loop();

    if ((int32_t)(millis() - proximoReporte) >= 0) {
      proximoReporte += 10000;
      Serial.printf("[%3lus] enviados=%lu en_cola=%lu perdidos=%lu fallos=%lu reconexiones=%lu "
                    "envio_prom=%lums envio_max=%lums buffers_libres_min=%lu/%d RSSI=%ddBm heap=%lu\n",
                    (unsigned long)((millis() - t0) / 1000), (unsigned long)enviados,
                    (unsigned long)uxQueueMessagesWaiting(colaLlenos), (unsigned long)bloquesPerdidos,
                    (unsigned long)fallos, (unsigned long)reconexiones,
                    (unsigned long)(nEnvio ? tEnvioSuma / nEnvio : 0), (unsigned long)tEnvioMax,
                    (unsigned long)minLibres, numBuffers, (int)WiFi.RSSI(),
                    (unsigned long)ESP.getFreeHeap());
      tEnvioSuma = 0; nEnvio = 0; tEnvioMax = 0;
    }
  }
  if (tFin == 0) tFin = millis();

  // 3) Temperatura y humedad DESPUÉS del audio
  LecturaDHT despues = leerDHT();
  digitalWrite(PIN_LED_CAPTURA, LOW);              // LED: terminó la toma de datos
  publicarDHT("despues", despues);

  snprintf(json, sizeof json,
    "{\"sesion\":\"%s\",\"mac\":\"%s\",\"duracion_real_ms\":%lu,\"muestras_rellenadas\":%lu,"
    "\"bloques_perdidos_buffer\":%lu,\"bloques_no_enviados\":%lu,\"bloques_enviados\":%lu,"
    "\"fallos_envio\":%lu,\"reconexiones\":%lu,\"envio_max_ms\":%lu,"
    "\"buffers\":%d,\"buffers_libres_min\":%lu}",
    nombreSesion, macTexto, (unsigned long)(tFin - t0), (unsigned long)rellenosTotal,
    (unsigned long)bloquesPerdidos, (unsigned long)noEnviados, (unsigned long)enviados,
    (unsigned long)fallos, (unsigned long)reconexiones, (unsigned long)tEnvioMaxTotal,
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
  mqtt.setBufferSize(1024);

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
  pinMode(PIN_LED_ENCENDIDO, OUTPUT);
  pinMode(PIN_LED_CAPTURA, OUTPUT);
  digitalWrite(PIN_LED_ENCENDIDO, HIGH);           // LED: ESP32 encendido
  digitalWrite(PIN_LED_CAPTURA, LOW);

  Serial.println("\nEstación ESP32: DHT11 + KY-037 -> MQTT (2 núcleos, sesiones de 5 min)");
  leerMAC();
  Serial.printf("MAC del ESP32: %s\n", macTexto);

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
  bloqueDescarte = (Bloque*)malloc(sizeof(Bloque));
  for (int i = 0; i < maxBuf; i++) {
    Bloque* b;
    if (conPsram) {
      b = (Bloque*)ps_malloc(sizeof(Bloque));
    } else {
      if (ESP.getFreeHeap() < 110000) break;       // deja memoria para WiFi y MQTT
      b = (Bloque*)malloc(sizeof(Bloque));
    }
    if (!b) break;
    xQueueSend(colaLibres, &b, 0);
    numBuffers++;
  }
  Serial.printf("PSRAM: %s\n", conPsram ? "sí" : "no");
  Serial.printf("Buffers de audio: %d (%.1f s de colchón). Memoria libre: %lu bytes\n",
                numBuffers, numBuffers * (float)MUESTRAS_BLOQUE / FS, (unsigned long)ESP.getFreeHeap());

  xTaskCreatePinnedToCore(tareaMuestreoFn, "muestreo", 4096, nullptr,
                          configMAX_PRIORITIES - 1, &tareaMuestreo, 1);
  xTaskCreatePinnedToCore(tareaRedFn, "red", 8192, nullptr, 2, nullptr, 0);
}

void loop() {
  vTaskDelete(nullptr);
}
