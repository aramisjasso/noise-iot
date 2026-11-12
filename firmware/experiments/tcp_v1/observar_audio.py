#!/usr/bin/env python3
"""
leer_u16.py — abre el audio crudo del ESP32 (audio_crudo.u16), lo grafica y,
si se pide, lo exporta a CSV.

Formato del archivo: enteros sin signo de 16 bits, little-endian, mono,
8000 muestras/s, valores del ADC (0-1023 a 10 bits, 0-4095 a 12 bits). No tiene encabezado.

Requisitos:  pip install numpy matplotlib

Uso:
    python leer_u16.py "C:\\datos_esp32\\esp32-01\\20261108T061500Z\\audio_crudo.u16"
    python leer_u16.py archivo.u16 --csv            # además genera audio_crudo.csv
    python leer_u16.py archivo.u16 --inicio 60 --dur 2   # grafica 2 s a partir del segundo 60
"""
import argparse
import json
import os

import numpy as np
import matplotlib.pyplot as plt

p = argparse.ArgumentParser()
p.add_argument("archivo", nargs="?", help="si se omite, se abre una ventana para elegirlo")
p.add_argument("--fs", type=int, default=None, help="frecuencia de muestreo (por defecto la de sesion.json o 8000)")
p.add_argument("--inicio", type=float, default=0, help="segundo desde el que se grafica el detalle")
p.add_argument("--dur", type=float, default=0.05, help="segundos del detalle (0.05 = 50 ms)")
p.add_argument("--csv", action="store_true", help="exportar a CSV (tiempo_s, valor_adc)")
a = p.parse_args()

# Sin ruta (p. ej. al usar el botón "Run" del editor): ventana para elegir el archivo
if not a.archivo:
    import tkinter as tk
    from tkinter import filedialog
    raiz = tk.Tk()
    raiz.withdraw()
    raiz.attributes("-topmost", True)
    a.archivo = filedialog.askopenfilename(
        title="Elige el audio crudo del ESP32",
        filetypes=[("Audio crudo ESP32", "*.u16"), ("Todos los archivos", "*.*")])
    raiz.destroy()
    if not a.archivo:
        raise SystemExit("No se eligió ningún archivo.")

# Frecuencia de muestreo: se toma de sesion.json si está junto al archivo
fs = a.fs
bits = None
meta = os.path.join(os.path.dirname(os.path.abspath(a.archivo)), "sesion.json")
if os.path.exists(meta):
    with open(meta, encoding="utf-8") as f:
        info = json.load(f)
    fs = fs or info.get("fs")
    bits = info.get("bits_adc")
fs = fs or 8000

# ---- Lectura: esta línea es todo lo que hace falta para cargar el archivo
x = np.fromfile(a.archivo, dtype="<u2")          # uint16 little-endian
if bits is None:                                 # sin sesion.json: se deduce de los valores
    bits = 12 if x.max() > 1023 else 10
tope = (1 << bits) - 1
t = np.arange(x.size) / fs

print(f"Muestras: {x.size:,}   Duración: {x.size / fs:.1f} s   fs: {fs} Hz")
print(f"ADC de {bits} bits (0-{tope})")
print(f"Mín: {x.min()}   Máx: {x.max()}   Media (nivel DC): {x.mean():.1f}")
ac = x.astype(np.float64) - x.mean()             # señal sin nivel DC
print(f"RMS (sin DC): {np.sqrt(np.mean(ac ** 2)):.2f} cuentas ADC")
if x.max() >= tope or x.min() <= 0:
    print(f"Aviso: hay muestras saturadas (0 o {tope}). Baja la ganancia o la sensibilidad.")

if a.csv:
    salida = os.path.splitext(a.archivo)[0] + ".csv"
    np.savetxt(salida, np.column_stack([t, x]), delimiter=",", fmt=["%.6f", "%d"],
               header="tiempo_s,valor_adc", comments="")
    print("CSV guardado en", salida)

# ---- Gráficas
fig, ax = plt.subplots(3, 1, figsize=(11, 9))

# 1) Señal completa (reducida para que la gráfica sea ligera)
paso = max(1, x.size // 200_000)
ax[0].plot(t[::paso], x[::paso], lw=0.4)
ax[0].set(title="Señal completa", xlabel="Tiempo (s)", ylabel=f"ADC (0-{tope})")

# 2) Detalle
i0 = int(a.inicio * fs)
i1 = min(x.size, i0 + max(2, int(a.dur * fs)))
ax[1].plot(t[i0:i1], x[i0:i1], lw=0.8, marker=".", ms=2)
ax[1].set(title=f"Detalle: {a.dur} s desde {a.inicio} s", xlabel="Tiempo (s)", ylabel="ADC")

# 3) Espectro (promedio de ventanas de 1 s)
n = fs
nvent = ac.size // n
if nvent:
    ventanas = ac[:nvent * n].reshape(nvent, n) * np.hanning(n)
    espectro = np.mean(np.abs(np.fft.rfft(ventanas, axis=1)) ** 2, axis=0)
    f = np.fft.rfftfreq(n, 1 / fs)
    ax[2].semilogy(f, espectro + 1e-12, lw=0.7)
    ax[2].set(title="Espectro de potencia (promedio)", xlabel="Frecuencia (Hz)",
              ylabel="Potencia", xlim=(0, fs / 2))

for e in ax:
    e.grid(alpha=0.3)
fig.tight_layout()
plt.show()