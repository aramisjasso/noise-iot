# main.py — ESP32 + MicroPython
# VERSIÓN 1: envía los datos a la COMPUTADORA por WiFi (TCP).
# En la PC debe estar corriendo receptor_pc.py, que guarda:
#   - audio en crudo (uint16) + audio .wav
#   - temperatura/humedad en CSV
#
# Ciclo: cada 30 min captura 30 s (PRUEBA).
#   - DHT11: una lectura cada 6 s (100 lecturas por sesión)
#   - KY-037 (salida analógica A0): muestreo continuo a 8 kHz, ADC de 10 bits
#
# ¿Por qué WiFi y no USB? 8000 muestras/s x 2 bytes = 16 KB/s, y el puerto
# serie del REPL (115200 baudios) solo da ~11 KB/s. Por WiFi sobra ancho de banda.
 
import gc
import json
import socket
import struct
import time
 
import dht
import machine
import micropython
import network
 
try:
    import ntptime
except ImportError:
    ntptime = None
 
# ======================= CONFIGURACIÓN =======================
# Credenciales retiradas al subir al repo: llénalas solo en tu copia local.
WIFI_SSID = ""
WIFI_CLAVE = ""
PC_IP = ""                  # IP de la computadora que corre receptor_pc.py
PC_PUERTO = 5005
DISPOSITIVO = "esp32-01"
 
PIN_DHT = 4                 # DATA del DHT11
PIN_MIC = 34                # A0 del KY-037. Usa ADC1 (GPIO 32-39): ADC2 no funciona con WiFi
BITS_ADC = 12               # resolución del ADC del micrófono (0-1023)
 
FS = 8000                   # frecuencia de muestreo del micrófono (Hz)
DURACION_S = 30             # 30 s de captura (prueba). Para 10 min: 600
PERIODO_CICLO_S = 1800      # una captura cada 30 minutos
INTERVALO_DHT_S = 6         # lectura de temperatura/humedad cada 6 s
MUESTRAS_BLOQUE = 4000      # 0.5 s de audio por envío (8 KB)
# =============================================================
 
PERIODO_US = 1000000 // FS
N_BLOQUES = DURACION_S * FS // MUESTRAS_BLOQUE           # 60 con 30 s
BLOQUES_POR_DHT = INTERVALO_DHT_S * FS // MUESTRAS_BLOQUE  # 12
assert (DURACION_S * FS) % MUESTRAS_BLOQUE == 0
assert (INTERVALO_DHT_S * FS) % MUESTRAS_BLOQUE == 0
 
# Tipos de trama (protocolo: tipo[1 byte] + longitud[4 bytes LE] + datos)
T_INICIO = 0x53   # 'S'  JSON
T_AUDIO = 0x41    # 'A'  seq[uint32] + rellenos[uint16] + muestras uint16 LE
T_DHT = 0x54      # 'T'  JSON
T_FIN = 0x45      # 'E'  JSON
 
OFF_DATOS = 11    # 1 (tipo) + 4 (longitud) + 4 (seq) + 2 (rellenos)
trama = bytearray(OFF_DATOS + 2 * MUESTRAS_BLOQUE)   # se reutiliza: sin basura en memoria
 
 
@micropython.native
def capturar_bloque(leer, buf, off, n, periodo, t_obj, ultimo):
    """Toma n muestras a ritmo fijo y las guarda como uint16 little-endian en buf[off:].
    Si el programa se atrasó (envío por red, lectura del DHT11), las muestras perdidas
    se rellenan con el último valor para no perder la alineación temporal."""
    tus = time.ticks_us
    tdiff = time.ticks_diff
    tadd = time.ticks_add
    rellenos = 0
    for i in range(n):
        atraso = tdiff(tus(), t_obj)
        if atraso < 0:
            while tdiff(tus(), t_obj) < 0:
                pass
            ultimo = leer()
        elif atraso < periodo:
            ultimo = leer()
        else:
            rellenos += 1
        j = off + 2 * i
        buf[j] = ultimo & 0xFF
        buf[j + 1] = ultimo >> 8
        t_obj = tadd(t_obj, periodo)
    return t_obj, ultimo, rellenos
 
 
def configurar_adc():
    """Configura el ADC a BITS_ADC bits y devuelve la función de lectura.
    ESP32 clásico: el ADC admite 9-12 bits y read() entrega el valor crudo (0-1023 a 10 bits).
    ESP32-S2/S3/C3 no tienen modo de 10 bits: se lee a 16 bits y se recorta."""
    adc = machine.ADC(machine.Pin(PIN_MIC))
    adc.atten(machine.ADC.ATTN_11DB)          # rango ~0-3.3 V
    try:
        adc.width(getattr(machine.ADC, "WIDTH_%dBIT" % BITS_ADC))
        return adc.read
    except (AttributeError, ValueError):
        desplazamiento = 16 - BITS_ADC
        leer16 = adc.read_u16
        print("Este chip no permite fijar %d bits; se reduce por software" % BITS_ADC)
        return lambda: leer16() >> desplazamiento
 
 
def conectar_wifi(intentos=5):
    """Conecta al WiFi reiniciando la interfaz antes de cada intento.
    Evita 'Wifi Internal State Error', que aparece tras un soft reboot (Ctrl+D / Thonny)
    porque el WiFi sigue activo o a medio conectar del programa anterior."""
    wlan = network.WLAN(network.STA_IF)
    if wlan.active() and wlan.isconnected():
        return wlan
    clave_mal = getattr(network, "STAT_WRONG_PASSWORD", None)
    sin_red = getattr(network, "STAT_NO_AP_FOUND", None)
    for intento in range(1, intentos + 1):
        print("Conectando a WiFi '%s' (intento %d/%d)..." % (WIFI_SSID, intento, intentos))
        try:
            wlan.active(False)          # reinicio limpio de la interfaz
            time.sleep_ms(500)
            wlan.active(True)
            time.sleep_ms(500)
            try:
                wlan.disconnect()
            except OSError:
                pass
            wlan.connect(WIFI_SSID, WIFI_CLAVE)
        except OSError as e:
            print("  Error al iniciar la conexión:", e)
            time.sleep(2)
            continue
        t = time.ticks_ms()
        while not wlan.isconnected() and time.ticks_diff(time.ticks_ms(), t) < 20000:
            estado = wlan.status()
            if estado == clave_mal:
                print("  Contraseña incorrecta")
                break
            if estado == sin_red:
                print("  No se encuentra la red (¿nombre correcto? ¿es de 2.4 GHz?)")
                break
            time.sleep_ms(200)
        if wlan.isconnected():
            try:
                wlan.config(pm=network.WLAN.PM_NONE)   # sin ahorro de energía: menos latencia
            except Exception:
                pass
            print("WiFi OK:", wlan.ifconfig()[0])
            return wlan
        time.sleep(2)
    raise OSError("No se pudo conectar al WiFi")
 
 
def sincronizar_hora():
    if ntptime is None:
        return
    for _ in range(3):
        try:
            ntptime.settime()   # hora UTC
            return
        except Exception:
            time.sleep(1)
 
 
def sello_utc():
    t = time.gmtime()
    return "%04d%02d%02dT%02d%02d%02dZ" % t[:6]
 
 
def leer_dht(sensor):
    """Devuelve (temperatura, humedad, error)."""
    try:
        sensor.measure()
        return sensor.temperature(), sensor.humidity(), None
    except OSError as e:
        return None, None, str(e)
 
 
def enviar_json(sock, tipo, obj):
    datos = json.dumps(obj).encode()
    sock.write(struct.pack("<BI", tipo, len(datos)))
    sock.write(datos)
 
 
def conectar_pc():
    ip_esp = network.WLAN(network.STA_IF).ifconfig()[0]
    if ip_esp.rsplit(".", 1)[0] != PC_IP.rsplit(".", 1)[0]:
        print("AVISO: el ESP32 (%s) y la PC (%s) están en redes distintas; "
              "conéctalos al mismo WiFi o corrige PC_IP" % (ip_esp, PC_IP))
    direccion = socket.getaddrinfo(PC_IP, PC_PUERTO)[0][-1]
    for intento in range(12):           # hasta ~1 min esperando al receptor
        s = socket.socket()
        try:
            s.connect(direccion)
            return s
        except OSError as e:
            s.close()
            codigo = e.args[0] if e.args else e
            if codigo in (104, 111):
                causa = "la PC rechazó la conexión: ¿está corriendo receptor_pc.py?"
            elif codigo == 113:
                causa = "no hay ruta a la PC: ¿están en la misma red WiFi?"
            elif codigo in (110, 116, 118):
                causa = "sin respuesta: IP equivocada o el firewall bloquea el puerto"
            else:
                causa = "error %s" % codigo
            print("Receptor no disponible en %s:%d (%s). Reintento %d"
                  % (PC_IP, PC_PUERTO, causa, intento + 1))
            time.sleep(5)
    raise OSError("No se pudo conectar con la PC")
 
 
def ejecutar_sesion(sock, leer, sensor):
    sesion = sello_utc()
    print("Inicio de sesión", sesion)
    enviar_json(sock, T_INICIO, {
        "dispositivo": DISPOSITIVO, "sesion": sesion, "fs": FS,
        "duracion_s": DURACION_S, "muestras_bloque": MUESTRAS_BLOQUE,
        "bloques": N_BLOQUES, "intervalo_dht_s": INTERVALO_DHT_S,
        "bits_adc": BITS_ADC,
        "formato_audio": "uint16 little-endian, valores 0-%d" % ((1 << BITS_ADC) - 1),
    })
    gc.collect()
 
    ultimo = leer()
    total_rellenos = 0
    lecturas_ok = 0
    t0 = time.ticks_ms()
    t_obj = time.ticks_add(time.ticks_us(), 1000)
 
    dht_pendiente = False
    intentos_dht = 0
    seq_dht = 0
    for seq in range(N_BLOQUES):
        t_obj, ultimo, rell = capturar_bloque(
            leer, trama, OFF_DATOS, MUESTRAS_BLOQUE, PERIODO_US, t_obj, ultimo)
        total_rellenos += rell

        # El DHT11 se lee DESPUÉS de capturar y ANTES de enviar: así el WiFi lleva
        # 0.5 s sin transmitir y no altera los tiempos de microsegundos del sensor.
        if seq % BLOQUES_POR_DHT == 0:
            dht_pendiente, intentos_dht, seq_dht = True, 0, seq
        if dht_pendiente and (seq - seq_dht) % 2 == 0:   # reintentos cada 1 s (mínimo del DHT11)
            temp, hum, err = leer_dht(sensor)
            intentos_dht += 1
            if temp is not None or intentos_dht >= 3:
                if temp is not None:
                    lecturas_ok += 1
                else:
                    print("DHT11 sin lectura:", err)
                enviar_json(sock, T_DHT, {
                    "sesion": sesion, "t_ms": time.ticks_diff(time.ticks_ms(), t0),
                    "muestra": (seq + 1) * MUESTRAS_BLOQUE, "temp": temp, "hum": hum,
                    "intentos": intentos_dht, "error": err,
                })
                dht_pendiente = False

        struct.pack_into("<BIIH", trama, 0, T_AUDIO, 6 + 2 * MUESTRAS_BLOQUE, seq, rell)
        sock.write(trama)
 
    enviar_json(sock, T_FIN, {
        "sesion": sesion, "duracion_real_ms": time.ticks_diff(time.ticks_ms(), t0),
        "muestras_rellenadas": total_rellenos, "lecturas_dht_ok": lecturas_ok,
    })
    print("Fin de sesión. Muestras rellenadas:", total_rellenos)
 
 
def main():
    time.sleep(3)   # margen para interrumpir desde Thonny / mpremote
    machine.freq(240000000)
    leer = configurar_adc()
    sensor = dht.DHT11(machine.Pin(PIN_DHT))
 
    while True:                 # no termina el programa si el WiFi falla al arrancar
        try:
            conectar_wifi()
            break
        except OSError as e:
            print(e, "- reintento en 30 s")
            time.sleep(30)
    sincronizar_hora()
 
    proximo = time.ticks_ms()
    while True:
        espera = time.ticks_diff(proximo, time.ticks_ms())
        if espera > 0:
            print("Siguiente captura en", espera // 1000, "s")
            time.sleep_ms(espera)
        proximo = time.ticks_add(proximo, PERIODO_CICLO_S * 1000)
 
        sock = None
        try:
            conectar_wifi()
            sock = conectar_pc()
            ejecutar_sesion(sock, leer, sensor)
        except Exception as e:
            print("Error en la sesión:", e)
        finally:
            if sock:
                sock.close()
 
 
main()