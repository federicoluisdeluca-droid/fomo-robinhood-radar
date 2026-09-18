# Wrapper para Render (plan Free / Web Service).
#
# fomo-radar run y fomo-radar bot no abren ningun puerto HTTP -- son procesos
# de fondo. Render, en un "Web Service", mata el servicio si no detecta un
# puerto abierto. Este archivo abre un puerto minimo para que Render este
# contento, y lanza los dos procesos reales de fomo-radar por atras.
#
# FIX WAL: run y bot son dos procesos separados escribiendo al mismo SQLite.
# Sin WAL, uno puede quedar esperando al otro en silencio. Se activa una vez
# al arrancar, es una propiedad del archivo, no de cada conexion.
#
# SIN SHELL: el plan gratis no da acceso a terminal, asi que "score --export"
# y "score --import" (que se corren desde la linea de comandos) no se pueden
# ejecutar a mano. Como "fomo-radar run" ya escribe pending_scores.json solo
# en cada ciclo, este wrapper agrega dos paginas web para leer ese archivo y
# devolver la puntuacion, todo desde el navegador, sin terminal:
#   /pending  -> muestra pending_scores.json para copiar
#   /import   -> formulario para pegar el resultado puntuado y aplicarlo
#
# Start Command en Render: python3 server_fomo_radar.py

import glob
import html
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "10000"))
PROCESOS = []
PENDING_FILE = "pending_scores.json"
SCORED_FILE = "scored.json"


def activar_wal():
    # Busca los .db/.sqlite/.sqlite3 en el directorio de trabajo (los crea
    # "fomo-radar init") y les activa WAL + un timeout de espera generoso,
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
            print("[wrapper] no se pudo activar WAL en " + ruta + ": " + str(exc), flush=True)
    if not encontrados:
        print("[wrapper] todavia no hay archivo de base de datos (normal en el primer arranque)", flush=True)


def lanzar(nombre, comando):
    # Arranca un comando de fomo-radar y lo reinicia solo si se cae.
    while True:
        print("[wrapper] arrancando: " + nombre, flush=True)
        try:
            p = subprocess.Popen(comando, stdout=sys.stdout, stderr=sys.stderr)
            PROCESOS.append(p)
            p.wait()
            print("[wrapper] " + nombre + " termino con codigo " + str(p.returncode) + ", reintentando en 10s", flush=True)
        except Exception as exc:
            print("[wrapper] error lanzando " + nombre + ": " + str(exc), flush=True)
        time.sleep(10)


FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Importar puntaje</title></head>
<body style="font-family: sans-serif; max-width: 700px; margin: 40px auto;">
<h2>Pegar el resultado puntuado</h2>
<p>Pegue aca el JSON que le devolvio el chat de IA (la version puntuada de
pending_scores.json) y apriete Enviar. Esto corre
<code>fomo-radar score --import</code> por usted, sin terminal.</p>
<form method="POST" action="/import">
<textarea name="datos" rows="20" style="width:100%%;font-family:monospace;"></textarea>
<br><br>
<button type="submit" style="padding:10px 20px;font-size:16px;">Enviar</button>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _texto(self, cuerpo, tipo="text/plain; charset=utf-8", codigo=200):
        datos = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        if self.path.startswith("/pending"):
            if os.path.exists(PENDING_FILE):
                with open(PENDING_FILE, "r", encoding="utf-8") as f:
                    self._texto(f.read(), "application/json; charset=utf-8")
            else:
                self._texto("Todavia no existe " + PENDING_FILE + ". Esperar al proximo ciclo de fomo-radar run.")
        elif self.path.startswith("/import"):
            self._texto(FORM_HTML, "text/html; charset=utf-8")
        else:
            self._texto("fomo-radar wrapper: OK. Rutas: /pending  /import")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/import"):
            self._texto("no encontrado", codigo=404)
            return
        largo = int(self.headers.get("Content-Length", 0))
        crudo = self.rfile.read(largo).decode("utf-8", errors="replace")
        # viene como application/x-www-form-urlencoded desde el <textarea name="datos">
        datos = crudo
        if crudo.startswith("datos="):
            from urllib.parse import unquote_plus
            datos = unquote_plus(crudo[len("datos="):])
        try:
            json.loads(datos)  # valida que sea JSON antes de escribirlo
        except Exception as exc:
            self._texto("Eso no parece JSON valido: " + str(exc), codigo=400)
            return
        with open(SCORED_FILE, "w", encoding="utf-8") as f:
            f.write(datos)
        try:
            resultado = subprocess.run(
                ["fomo-radar", "score", "--import", SCORED_FILE],
                capture_output=True, text=True, timeout=60,
            )
            salida = resultado.stdout + "\n" + resultado.stderr
        except Exception as exc:
            salida = "Error corriendo la importacion: " + str(exc)
        pagina = "<pre>" + html.escape(salida) + "</pre><p><a href=\"/import\">Volver</a></p>"
        self._texto(pagina, "text/html; charset=utf-8")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    activar_wal()

    threading.Thread(target=lanzar, args=("fomo-radar run", ["fomo-radar", "run"]), daemon=True).start()

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=lanzar, args=("fomo-radar bot", ["fomo-radar", "bot"]), daemon=True).start()
    else:
        print("[wrapper] TELEGRAM_BOT_TOKEN no seteado, no arranco el bot", flush=True)

    print("[wrapper] sirviendo en el puerto " + str(PORT) + " (/pending, /import)", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
