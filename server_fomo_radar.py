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
# MEMORIA: el plan free de Render no muestra el grafico de Metrics, asi que
# este wrapper mide su propia memoria (via /proc) y la loguea cada 2 minutos,
# para poder ver si el contenedor muere por quedarse sin RAM (limite: 512 MB).
#
# PANEL: sin acceso a terminal (plan gratis de Render), esta pagina junta en
# un solo lugar las tres tareas que se hacen a mano en este proyecto:
#   - ver pending_scores.json para copiarlo a un chat de IA
#   - pegar el resultado puntuado (corre "score --import" por vos)
#   - agregar un trader por @usuario O por wallet (resuelve el handle solo,
#     via fomoapi.io, antes de correr "discover --add")
#
# NOTA TECNICA: este archivo evita a proposito los bloques de texto con
# comillas triples (""") para HTML/CSS largos -- un pegado anterior en el
# editor web de GitHub corrompio uno y tiro un SyntaxError en el deploy.
# Todo el HTML se arma con listas de lineas + join(), que es a prueba de eso.
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
import urllib.error
import urllib.parse
import urllib.request
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


def memoria_total_mb():
    # Lee VmRSS de /proc para el wrapper y cada subproceso vivo (fomo-radar run,
    # fomo-radar bot). Sin esto no hay forma de ver la memoria real en el plan
    # gratis de Render, que no muestra el grafico de Metrics.
    total_kb = 0
    pids = [os.getpid()] + [p.pid for p in PROCESOS if p.poll() is None]
    for pid in pids:
        try:
            with open("/proc/%s/status" % pid) as f:
                for linea in f:
                    if linea.startswith("VmRSS:"):
                        total_kb += int(linea.split()[1])
                        break
        except Exception:
            pass
    return total_kb / 1024.0


def monitor_memoria():
    # Log cada 2 minutos. El plan free de Render da 512 MB -- si esto se acerca
    # o supera ese numero antes de que el contenedor muera, confirma la causa.
    while True:
        try:
            mb = memoria_total_mb()
            print("[wrapper] memoria total aprox: %.0f MB (limite del plan free: 512 MB)" % mb, flush=True)
        except Exception as exc:
            print("[wrapper] no se pudo medir memoria: %s" % exc, flush=True)
        time.sleep(120)


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


def es_wallet(valor):
    return valor.lower().startswith("0x") and len(valor) == 42


def resolver_wallet(entrada):
    # Devuelve (wallet, error). Si "entrada" ya es una wallet, la devuelve tal
    # cual. Si es un @usuario, la resuelve via fomoapi.io (misma fuente que
    # usa el resto del proyecto) y devuelve la wallet EVM (Robinhood Chain).
    valor = entrada.strip().lstrip("@")
    if es_wallet(valor):
        return valor, None
    key = os.environ.get("FOMOAPI_KEY", "")
    if not key:
        return None, "No se puede resolver un @usuario sin FOMOAPI_KEY configurada en Render. Pegue la wallet (0x...) directamente."
    try:
        req = urllib.request.Request(
            "https://api.fomoapi.io/v2/users/" + urllib.parse.quote(valor),
            headers={"authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            datos = json.loads(resp.read().decode("utf-8"))
        wallet = (datos.get("wallets") or {}).get("evm")
        if not wallet:
            return None, "fomoapi.io conoce a @" + valor + " pero no tiene una wallet EVM (Robinhood Chain) para esa cuenta."
        return wallet, None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, "fomoapi.io no conoce a @" + valor + "."
        if exc.code == 401:
            return None, "FOMOAPI_KEY invalida (401)."
        if exc.code == 402:
            return None, "Sin creditos en fomoapi.io para resolver el handle (402)."
        return None, "Error consultando fomoapi.io: HTTP " + str(exc.code)
    except Exception as exc:
        return None, "Error consultando fomoapi.io: " + str(exc)


# Colores del club (celeste y blanco), sin usar el escudo real (derechos de
# autor). Fondo negro pedido, con esos colores como acento.
# Armado con lista de lineas + join(), NO con comillas triples -- ver nota
# tecnica arriba del archivo.
ESTILO_LINEAS = [
    "<style>",
    "body { font-family: -apple-system, Arial, sans-serif; max-width: 720px; margin: 0 auto; padding: 0 16px 30px; background: #000; color: #eaeaea; }",
    ".franja { height: 10px; margin: 0 -16px 24px; background: repeating-linear-gradient(90deg, #6ecdf0 0 22px, #ffffff 22px 44px); }",
    "h1 { font-size: 24px; color: #6ecdf0; letter-spacing: 0.5px; }",
    "h2 { font-size: 17px; margin-top: 36px; border-top: 1px solid #234; padding-top: 20px; color: #6ecdf0; }",
    "textarea, input[type=text] { width: 100%; font-family: monospace; font-size: 14px; box-sizing: border-box; padding: 8px; background: #111; color: #eaeaea; border: 1px solid #345; border-radius: 4px; }",
    "textarea { height: 160px; }",
    "button { padding: 10px 18px; font-size: 15px; margin-top: 10px; cursor: pointer; background: #6ecdf0; color: #000; border: none; border-radius: 6px; font-weight: bold; }",
    ".ayuda { color: #aab; font-size: 14px; }",
    "a.boton { display: inline-block; padding: 8px 14px; background: #6ecdf0; border-radius: 6px; text-decoration: none; color: #000; margin-top: 6px; font-weight: bold; }",
    "pre { background: #111; padding: 12px; overflow-x: auto; white-space: pre-wrap; color: #dde; border: 1px solid #345; border-radius: 4px; }",
    "</style>",
]
ESTILO = chr(10).join(ESTILO_LINEAS)

PAGINA_INICIO_LINEAS = [
    "<!doctype html>",
    "<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
    "<title>Panel Satelite</title>" + ESTILO + "</head>",
    "<body>",
    "<div class=\"franja\"></div>",
    "<h1>Panel del bot</h1>",
    "<p class=\"ayuda\">Todo lo que se hace a mano en este proyecto, en un solo lugar. No hace falta terminal.</p>",
    "",
    "<h2>1. Ver traders para puntuar</h2>",
    "<p class=\"ayuda\">",
    "El bot va descubriendo wallets nuevas siguiendo una lista curada de traders de",
    "Robinhood Chain, pero por si solo no puede juzgar si son buenos o malos -- eso",
    "necesita criterio, no solo numeros. Este boton muestra esa lista cruda",
    "(pending_scores.json). Copiela entera junto con las instrucciones que trae",
    "adentro y pegela en un chat de IA (por ejemplo, conmigo); la IA le va a",
    "devolver un puntaje de 0 a 100 por cada wallet.",
    "</p>",
    "<a class=\"boton\" href=\"/pending\" target=\"_blank\">Ver pending_scores.json</a>",
    "",
    "<h2>2. Pegar el resultado puntuado</h2>",
    "<p class=\"ayuda\">",
    "Una vez que la IA evaluo la lista del paso 1, le devuelve un puntaje por cada",
    "trader. Pegue esa respuesta aca y aplique -- recien despues de este paso esos",
    "traders empiezan a contar para /signals, /fresh y el resto de las funciones",
    "del bot. Sin este paso, es como si no existieran todavia para el.",
    "</p>",
    "<form method=\"POST\" action=\"/import\">",
    "<textarea name=\"datos\" placeholder=\"[{&quot;address&quot;: ...}]\"></textarea>",
    "<br><button type=\"submit\">Aplicar puntuacion</button>",
    "</form>",
    "",
    "<h2>3. Agregar un trader al roster</h2>",
    "<p class=\"ayuda\">",
    "El bot por default solo sigue a la lista curada externa (la de \"trenches\").",
    "Si conoce o encuentra a alguien mas que le interese seguir por su cuenta,",
    "escriba su <b>@usuario</b> aca abajo (o pegue directamente la wallet 0x... si",
    "ya la tiene) y el boton resuelve el handle y lo agrega solo. Resolver un",
    "@usuario tiene un costo de creditos de fomoapi.io mas alto que el resto del",
    "bot, asi que no lo use a lo loco. Una vez agregado, todavia no tiene puntaje",
    "-- para eso hay que repetir despues los pasos 1 y 2.",
    "</p>",
    "<form method=\"POST\" action=\"/agregar\">",
    "<input type=\"text\" name=\"entrada\" placeholder=\"@usuario o 0x...\">",
    "<br><button type=\"submit\">Agregar al roster</button>",
    "</form>",
    "",
    "</body></html>",
]
PAGINA_INICIO = chr(10).join(PAGINA_INICIO_LINEAS)


class Handler(BaseHTTPRequestHandler):
    def _texto(self, cuerpo, tipo="text/plain; charset=utf-8", codigo=200):
        datos = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def _pagina_resultado(self, titulo, salida):
        partes = [
            "<!doctype html><html><head><meta charset=\"utf-8\">" + ESTILO + "</head><body>",
            "<div class=\"franja\"></div>",
            "<h1>" + html.escape(titulo) + "</h1>",
            "<pre>" + html.escape(salida) + "</pre>",
            "<p><a class=\"boton\" href=\"/\">Volver al inicio</a></p>",
            "</body></html>",
        ]
        self._texto(chr(10).join(partes), "text/html; charset=utf-8")

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
                salida = resultado.stdout + chr(10) + resultado.stderr
            except Exception as exc:
                salida = "Error corriendo la importacion: " + str(exc)
            self._pagina_resultado("Puntuacion aplicada", salida)
            return

        if self.path.startswith("/agregar"):
            entrada = campos.get("entrada", campos.get("wallet", "")).strip()
            if not entrada:
                self._pagina_resultado("Falta el dato", "Escriba un @usuario o pegue una wallet 0x...")
                return
            wallet, error = resolver_wallet(entrada)
            if error:
                self._pagina_resultado("No se pudo resolver", error)
                return
            try:
                resultado = subprocess.run(
                    ["fomo-radar", "discover", "--add", wallet],
                    capture_output=True, text=True, timeout=60,
                )
                salida = "Resuelto a: " + wallet + chr(10) + chr(10) + resultado.stdout + chr(10) + resultado.stderr
            except Exception as exc:
                salida = "Resuelto a: " + wallet + chr(10) + chr(10) + "Error agregando la wallet: " + str(exc)
            self._pagina_resultado("Trader agregado", salida)
            return

        self._texto("no encontrado", codigo=404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    activar_wal()

    threading.Thread(target=monitor_memoria, daemon=True).start()

    threading.Thread(target=lanzar, args=("fomo-radar run", ["fomo-radar", "run"]), daemon=True).start()

    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=lanzar, args=("fomo-radar bot", ["fomo-radar", "bot"]), daemon=True).start()
    else:
        print("[wrapper] TELEGRAM_BOT_TOKEN no seteado, no arranco el bot", flush=True)

    print("[wrapper] sirviendo panel en el puerto " + str(PORT), flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
