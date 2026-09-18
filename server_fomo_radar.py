# Wrapper para Render (plan Free / Web Service).
#
# fomo-radar run y fomo-radar bot no abren ningun puerto HTTP -- son procesos
# de fondo. Render, en un "Web Service", mata el servicio si no detecta un
# puerto abierto. Este archivo abre un puerto minimo solo para que Render
# este contento, y lanza los dos procesos reales de fomo-radar por atras.
#
# FIX: run y bot son dos procesos SEPARADOS escribiendo al mismo SQLite. Sin
# WAL, SQLite deja escribir a uno solo por vez; el otro puede quedar esperando
# sin errores visibles (silencio total en el log, bot sin responder). WAL es
# una propiedad del ARCHIVO, no de cada conexion -- activarlo una vez alcanza
# para las dos conexiones futuras, sin tocar el codigo de fomo-radar.
#
# Start Command en Render: python3 server_fomo_radar.py

import glob
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "10000"))
PROCESOS = []


def activar_wal():
    # Busca los .db/.sqlite/.sqlite3 en el directorio de trabajo (los crea
    # 'fomo-radar init') y les activa WAL + un timeout de espera generoso,
    # para que run y bot no se bloqueen mutuamente en silencio.
    patrones = ["*.db", "*.sqlite", "*.sqlite3", "**/*.db", "**/*.sqlite", "**/*.sqlite3"]
    encontrados = set()
    for patron in patrones:
        encontrados.update(glob.glob(patron, recursive=True))
    for ruta in encontrados:
        try:
            con = sqlite3.connect(ruta, timeout=30)
            modo = con.execute("PRAGMA journal_mode=WAL;").fetchone()
            con.execute("PRAGMA busy_timeout=30000;")
            con.commit()
            con.close()
            print("[wrapper] WAL activado en " + ruta + " (modo: " + str(modo) + ")", flush=True)
        except Exception as exc:
