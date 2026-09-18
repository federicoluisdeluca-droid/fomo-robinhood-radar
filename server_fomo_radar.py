"""
Wrapper para Render (plan Free / Web Service).

fomo-radar run y fomo-radar bot no abren ningún puerto HTTP -- son procesos
de fondo. Render, en un "Web Service", mata el servicio si no detecta un
puerto abierto. Este archivo abre un puerto mínimo solo para que Render
esté contento, y lanza los dos procesos reales de fomo-radar por atrás.

Start Command en Render: python3 server.py
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "10000"))
PROCESOS = []


def lanzar(nombre, comando):
    """Arranca un comando de fomo-radar y lo reinicia solo si se cae."""
    while True:
        print(f"[wrapper] arrancando: {nombre} -> {comando}", flush=True)
        try:
            p = subprocess.Popen(comando, stdout=sys.stdout, stderr=sys.stderr)
            PROCESOS.append(p)
            p.wait()
            print(f"[wrapper] {nombre} terminó con código {p.returncode}, reintentando en 10s", flush=True)
        except Exception as exc:
            print(f"[wrapper] error lanzando {nombre}: {exc}", flush=True)
        time.sleep(10)


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"fomo-radar wrapper: OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # no ensuciar el log con cada ping de UptimeRobot


if __name__ == "__main__":
    # fomo-radar run: el loop principal (discover -> track -> score -> analyze)
    threading.Thread(target=lanzar, args=("fomo-radar run", ["fomo-radar", "run"]), daemon=True).start()
    # fomo-radar bot: el bot de Telegram, si TELEGRAM_BOT_TOKEN está seteado
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        threading.Thread(target=lanzar, args=("fomo-radar bot", ["fomo-radar", "bot"]), daemon=True).start()
    else:
        print("[wrapper] TELEGRAM_BOT_TOKEN no seteado, no arranco el bot", flush=True)

    print(f"[wrapper] sirviendo ping en el puerto {PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()
