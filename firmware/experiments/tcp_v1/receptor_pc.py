"""
receptor_pc.py — corre en la COMPUTADORA (Windows, Linux o macOS, Python 3.8+).
Solo usa la biblioteca estándar.

Escucha al ESP32 por TCP y por cada sesión de 10 minutos guarda:
  datos/<dispositivo>/<sesion>/
      audio_crudo.u16     muestras del ADC tal cual (uint16 little-endian, 8 kHz)
      audio.wav           mismo audio, mono 16 bits 8 kHz, sin componente DC
      temp_hum.csv        lecturas del DHT11 cada 6 s
      sesion.json         metadatos (fs, duración real, muestras rellenadas, etc.)

Uso:
    python receptor_pc.py                 # puerto 5005, carpeta ./datos
    python receptor_pc.py --puerto 5005 --carpeta D:/mediciones

Recuerda permitir el puerto en el firewall de la PC.
"""
import argparse
import array
import csv
import json
import os
import socket
import struct
import sys
import threading
import time
import wave

T_INICIO, T_AUDIO, T_DHT, T_FIN = 0x53, 0x41, 0x54, 0x45


def leer_exacto(conn, n):
    buf = bytearray(n)
    vista = memoryview(buf)
    leidos = 0
    while leidos < n:
        r = conn.recv_into(vista[leidos:], n - leidos)
        if r == 0:
            raise ConnectionError("conexión cerrada")
        leidos += r
    return bytes(buf)


def crudo_a_wav(ruta_crudo, ruta_wav, fs, ganancia=1.0):
    """Convierte uint16 (ADC) a WAV de 16 bits con signo, quitando el nivel DC."""
    datos = array.array("H")
    with open(ruta_crudo, "rb") as f:
        datos.frombytes(f.read())
    if sys.byteorder == "big":
        datos.byteswap()
    if not datos:
        return
    media = sum(datos) / len(datos)
    salida = array.array("h", (
        max(-32768, min(32767, int((x - media) * ganancia))) for x in datos))
    if sys.byteorder == "big":
        salida.byteswap()
    with wave.open(ruta_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(salida.tobytes())


class Sesion:
    def __init__(self, carpeta_base, info):
        self.info = info
        self.fs = int(info.get("fs", 8000))
        self.n_bloque = int(info.get("muestras_bloque", 4000))
        disp = info.get("dispositivo", "esp32")
        nombre = info.get("sesion") or time.strftime("%Y%m%dT%H%M%S")
        self.carpeta = os.path.join(carpeta_base, disp, nombre)
        os.makedirs(self.carpeta, exist_ok=True)
        self.ruta_crudo = os.path.join(self.carpeta, "audio_crudo.u16")
        self.f_audio = open(self.ruta_crudo, "wb")
        self.f_csv = open(os.path.join(self.carpeta, "temp_hum.csv"), "w", newline="")
        self.csv = csv.writer(self.f_csv)
        self.csv.writerow(["hora_pc", "t_ms", "muestra_audio", "temperatura_c", "humedad_pct"])
        self.bloques = 0
        self.ultimo_seq = -1
        self.huecos = 0
        self.inicio_pc = time.strftime("%Y-%m-%d %H:%M:%S")

    def audio(self, payload):
        seq, rellenos = struct.unpack_from("<IH", payload, 0)
        muestras = payload[6:]
        # Si faltara algún bloque, se escribe en su posición correcta (deja ceros).
        self.f_audio.seek(seq * self.n_bloque * 2)
        self.f_audio.write(muestras)
        if seq != self.ultimo_seq + 1:
            self.huecos += 1
        self.ultimo_seq = seq
        self.bloques += 1

    def dht(self, d):
        self.csv.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), d.get("t_ms"),
                           d.get("muestra"), d.get("temp"), d.get("hum")])
        self.f_csv.flush()

    def cerrar(self, fin=None, completa=True):
        self.f_audio.close()
        self.f_csv.close()
        crudo_a_wav(self.ruta_crudo, os.path.join(self.carpeta, "audio.wav"), self.fs)
        meta = dict(self.info)
        meta.update({
            "inicio_pc": self.inicio_pc,
            "fin_pc": time.strftime("%Y-%m-%d %H:%M:%S"),
            "bloques_recibidos": self.bloques,
            "huecos_detectados": self.huecos,
            "completa": completa,
            "fin": fin,
        })
        with open(os.path.join(self.carpeta, "sesion.json"), "w") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        print(f"[OK] Sesión guardada en {self.carpeta} "
              f"({self.bloques} bloques, completa={completa})")


def atender(conn, direccion, carpeta):
    print("Conexión de", direccion)
    sesion = None
    try:
        while True:
            tipo, largo = struct.unpack("<BI", leer_exacto(conn, 5))
            payload = leer_exacto(conn, largo)
            if tipo == T_INICIO:
                if sesion:
                    sesion.cerrar(completa=False)
                sesion = Sesion(carpeta, json.loads(payload))
                print("Nueva sesión:", sesion.carpeta)
            elif sesion is None:
                continue
            elif tipo == T_AUDIO:
                sesion.audio(payload)
            elif tipo == T_DHT:
                d = json.loads(payload)
                sesion.dht(d)
                print(f"  T={d.get('temp')} °C  H={d.get('hum')} %  "
                      f"({d.get('t_ms', 0) / 1000:.0f} s)")
            elif tipo == T_FIN:
                sesion.cerrar(fin=json.loads(payload), completa=True)
                sesion = None
    except (ConnectionError, OSError) as e:
        print("Conexión terminada:", e)
    finally:
        if sesion:
            sesion.cerrar(completa=False)
        conn.close()


def main():
    p = argparse.ArgumentParser(description="Receptor de datos del ESP32")
    p.add_argument("--puerto", type=int, default=5005)
    p.add_argument("--carpeta", default="datos")
    a = p.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.puerto))
    srv.listen(4)
    print(f"Esperando al ESP32 en el puerto {a.puerto}. Datos en: {os.path.abspath(a.carpeta)}")
    try:
        while True:
            conn, direccion = srv.accept()
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            threading.Thread(target=atender, args=(conn, direccion, a.carpeta),
                             daemon=True).start()
    except KeyboardInterrupt:
        print("\nDetenido.")


if __name__ == "__main__":
    main()
