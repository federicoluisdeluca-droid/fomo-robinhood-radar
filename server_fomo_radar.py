"""
Wrapper para Render (plan Free / Web Service).

fomo-radar run y fomo-radar bot no abren ningún puerto HTTP -- son procesos
de fondo. Render, en un "Web Service", mata el servicio si no detecta un
puerto abierto. Este archivo abre un puerto mínimo solo para que Render
esté contento, y lanza los dos procesos reales de fomo-radar por atrás.

FIX: run y bot son dos procesos SEPARADOS escribiendo al mismo SQLite. Sin
WAL, SQLite deja escribir a uno solo por vez; el otro puede quedar esperando
sin errores visibles (silencio total en el log, bot sin responder). WAL es
una propiedad del ARCHIVO, no de cada conexión -- activarlo una vez alcanza
para
