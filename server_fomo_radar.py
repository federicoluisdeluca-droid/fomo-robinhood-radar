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
# PANEL: sin acceso a terminal (plan gratis de Render), esta pagina junta en
# un solo lugar las tres tareas que se hacen a mano en este proyecto:
#   - ver pending_scores.json para copiarlo a un chat de IA
#   - pegar el resultado puntuado (corre "score --import" por vos)
#   - agregar una wallet nueva al roster (corre "discover --add" por vos)
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
from urllib.parse import unquote_plus

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


ESTILO = """
<style>
body { font-family: -apple-system, Arial, sans-serif; max-width: 720px; margin: 30px auto; padding: 0 16px; color: #222; }
h1 { font-size: 22px; }
h2 { font-size: 17px; margin-top: 36px; border-top: 1px solid #ddd; padding-top: 20px; }
textarea, input[type=text] { width: 100%; font-family: monospace; font-size: 14px; box-sizing: border-box; padding: 8px; }
textarea { height: 160px; }
button { padding: 10px 18px; font-size: 15px; margin-top: 10px; cursor: pointer; }
.ayuda { color: #666; font-size: 14px; }
a.boton { display: inline-block; padding: 8px 14px; background: #eee; border-radius: 6px; text-decoration: none; color: #222; margin-top: 6px; }
pre { background: #f5f5f5; padding: 12px; overflow-x: auto; white-space: pre-wrap; }
</style>
"""

PAGINA_INICIO = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Panel Satelite</title>%s</head>
<body>
<h1>Panel del bot</h1>
<p class="ayuda">Todo lo que se hace a mano en este proyecto, en un solo lugar. No hace falta terminal.</p>

<h2>1. Ver traders para puntuar</h2>
<p class="ayuda">
El bot va descubriendo wallets nuevas siguiendo una lista curada de traders de
Robinhood Chain, pero por si solo no puede juzgar si son buenos o malos -- eso
necesita criterio, no solo numeros. Este boton muestra esa lista cruda
(pending_scores.json). Copiela entera junto con las instrucciones que trae
adentro y pegela en un chat de IA (por ejemplo, conmigo); la IA le va a
devolver un puntaje de 0 a 100 por cada wallet.
</p>
<a class="boton" href="/pending" target="_blank">Ver pending_scores.json</a>

<h2>2. Pegar el resultado puntuado</h2>
<p class="ayuda">
Una vez que la IA evaluo la lista del paso 1, le devuelve un puntaje por cada
trader. Pegue esa respuesta aca y aplique -- recien despues de este paso esos
traders empiezan a contar para /signals, /fresh y el resto de las funciones
del bot. Sin este paso, es como si no existieran todavia para el.
</p>
<form method="POST" action="/import">
<textarea name="datos" placeholder="[{&quot;address&quot;: ...}]"></textarea>
<br><button type="submit">Aplicar puntuacion</button>
</form>

<h2>3. Agregar un trader al roster</h2>
<p class="ayuda">
El bot por default solo sigue a la lista curada externa (la de "trenches").
Si conoce o encuentra a alguien mas que le interese seguir por su cuenta, este
boton le dice al bot "empeza a rastrear tambien a esta wallet". Ojo: pide la
<b>direccion de la wallet</b> (algo como 0x1234...), no el @usuario de fomo o
Twitter. Si solo tiene el handle, busquelo antes en fomo.family o GMGN para
conseguir la direccion. Una vez agregada, todavia no tiene puntaje -- para eso
hay que repetir despues los pasos 1 y 2.
</p>
<form method="POST" action="/agregar">
<input type="text" name="wallet" placeholder="0x...">
<br><button type="submit">Agregar al roster</button>
</form>

</body></html>""" % ESTILO


class Handler(BaseHTTPRequestHandler):
    def _texto(self, cuerpo, tipo="text/plain; charset=utf-8", codigo=200):
        datos = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _pagina_resultado(self, titulo, salida):
        pagina = (
            "<!doctype html><html><head><meta charset=\"utf-8\">" + ESTILO + "</head><body>"
            + "<h1>" + html.escape(titulo) + "</h1>"
            + "<pre>" + html.escape(salida) + "</pre>"
            + "<p><a class=\"boton\" href=\"/\">Volver al inicio</a></p>"
            + "</body></html>"
        )
        self._texto(pagina, "text/html; charset=utf-8")

    def _leer_form(self):
        largo = int(self.headers.get("Content-Length", 0))
        crudo = self.rfile.read(largo).decode("utf-8", errors="replace")
        campos = {}
        for parte in crudo.split("&"):
            if "=" in parte:
                clave, valor = parte.split("=", 1)
                campos[clave] = unquote_plus(valor)
        return campos

    def do_GET(self):
        if self.path.startswith("/pending"):
            if os.path.exists(PENDING_FILE):
                with open(PENDING_FILE, "r", encoding="utf-8") as f:
                    self._texto(f.read(), "application/json; charset=utf-8")
            else:
                self._texto("Todavia no existe " + PENDING_FILE + ". Esperar al proximo ciclo de fomo-radar run.")
        elif self.path in ("/", "/inicio", "/panel"):
            self._texto(PAGINA_INICIO, "text/html; charset=utf-8")
        else:
            self._texto(PAGINA_INICIO, "text/html; charset=utf-8")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        campos = self._leer_form()

        if self.path.startswith("/import"):
            datos = campos.get("datos", "")
            try:
                json.loads(datos)
            except Exception as exc:
                self._pagina_resultado("Eso no es JSON valido", str(exc))
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
            self._pagina_resultado("Puntuacion aplicada", salida)
            return

        if self.path.startswith("/agregar"):
            wallet = campos.get("wallet", "").strip()
            if not wallet.startswith("0x") or len(wallet) != 42:
                self._pagina_resultado(
                    "Eso no parece una wallet valida",
                    "Se espera una direccion 0x... de 42 caracteres. Recibido: " + wallet,
                )
                return
            try:
                resultado = subprocess.run(
                    ["fomo-radar", "discover", "--add", wallet],
                    capture_output=True, text=True, timeout=60,
                )
                salida = resultado.stdout + "\n" + resultado.stderr
            except Exception as exc:
                salida = "Error agregando la wallet: " + str(exc)
            self._pagina_resultado("Wallet agregada", salida)
            return

        self._texto("no encontrado", codigo=404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    activar_wal()

    threading.Thread(target=lanzar, args=("fomo-radar run", ["fomo-radar", "run"]), daemon=True).start()

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=lanzar, args=("fomo-radar bot", ["fomo-radar", "bot"]), daemon=True).start()
    else:
        print("[wrapper] TELEGRAM_BOT_TOKEN no seteado, no arranco el bot", flush=True)

    print("[wrapper] sirviendo panel en el puerto " + str(PORT), flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
