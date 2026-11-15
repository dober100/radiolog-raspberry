#!/usr/bin/env python3
"""
tools/radiolog_panel.py -- RadioLog
La «página de datos» de la Raspberry Pi: abriendo http://radiolog.local (o la dirección IP de la Raspberry) desde cualquier móvil u ordenador de la casa se ve
si la radio está encontrada y exactamente qué hay que escribir en RadioLog (dirección, puerto y clave), con letra grande. Solo Python estándar.

Lee estado.json que escribe radiolog_arranque.py. No cambia nada: es solo una ventana.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
for _cand in (_AQUI, _AQUI.parent):
    if (_cand / "core" / "remoto").is_dir():
        sys.path.insert(0, str(_cand))
        break
from core.remoto import autoconfig as AC  # noqa: E402

DIR_DATOS = Path(os.environ.get("RADIOLOG_DATOS", "/var/lib/radiolog"))

_COLOR = {"listo": ("#14532d", "#bbf7d0", "✔"), "sin_radio": ("#78350f", "#fde68a", "…"), "arrancando": ("#1e3a8a", "#bfdbfe", "…"),
          "sin_soporte": ("#7f1d1d", "#fecaca", "✖")}


def _leer_json(ruta: Path):
    try:
        d = json.loads(ruta.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def leer_estado(datos: Path = DIR_DATOS) -> dict:
    """Lo fijo del arranque (estado.json) combinado con cómo está la radio ahora (radio.json, lo escribe el servidor)."""
    return AC.estado_visible(_leer_json(datos / "estado.json") or {}, _leer_json(datos / "radio.json"))


def ips_locales() -> list[str]:
    """Direcciones IP de esta máquina en la red local (sin mandar nada a internet)."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                    # UDP: no manda ningún paquete, solo elige la interfaz de salida
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [i for i in ips if not i.startswith("127.")]


SEGUNDOS_EMPAREJAR = 120


def emparejamiento_restante(datos: Path = DIR_DATOS) -> int:
    """Segundos que le quedan al emparejamiento abierto (0 = cerrado)."""
    try:
        hasta = float(json.loads((datos / "emparejar.json").read_text(encoding="utf-8")).get("hasta", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0
    return max(0, int(hasta - time.time()))


def abrir_emparejamiento(datos: Path = DIR_DATOS, segundos: int = SEGUNDOS_EMPAREJAR) -> None:
    """Escribe la hora límite: el servidor, al ver el fichero, entrega la clave UNA vez al RadioLog que la pida. Escritura atómica."""
    datos.mkdir(parents=True, exist_ok=True)
    tmp = datos / "emparejar.json.tmp"
    tmp.write_text(json.dumps({"hasta": time.time() + segundos}), encoding="utf-8")
    os.replace(tmp, datos / "emparejar.json")


def ultimo_emparejado(datos: Path = DIR_DATOS, maximo_s: int = 300) -> dict | None:
    """El emparejamiento que se acaba de completar (lo escribe el servidor), si fue hace poco."""
    try:
        d = json.loads((datos / "emparejado.json").read_text(encoding="utf-8"))
        return d if time.time() - float(d.get("cuando", 0)) < maximo_s else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def pagina(estado: dict, nombre_equipo: str, ips: list[str], emparejar_restante: int = 0, emparejado: dict | None = None) -> str:
    """El HTML de la página (autocontenido, se refresca solo cada 5 segundos)."""
    e = html.escape
    fondo, texto, icono = _COLOR.get(estado.get("estado", ""), _COLOR["sin_radio"])
    listo = estado.get("estado") == "listo"
    en_marcha = estado.get("estado") in ("listo", "sin_radio", "sin_soporte")          # el servidor ya contesta: se puede emparejar aunque la radio aún no esté
    filas = ""
    if listo:
        audio = estado.get("audio_entrada") or ""
        filas += f"<p>Radio: <b>{e(estado.get('radio', ''))}</b> <small>(puerto {e(estado.get('puerto_serie', ''))})</small></p>"
        filas += (f"<p>Audio: <b>{e(audio)}</b></p>" if audio else
                  "<p>Audio: <b>no encontrado</b> <small>(sin él RadioLog no recibe ni manda sonido; compruebe el cable de audio USB)</small></p>")
    direccion = f"{nombre_equipo}.local"
    otras = " · ".join(e(i) for i in ips)
    datos = ""
    if en_marcha:
        clave = e(estado.get("clave", ""))
        if emparejado:
            accion = f'<div class="ok">✔ Emparejado con el equipo {e(str(emparejado.get("ip", "")))}. Ya puede cerrar esta página y conectar en RadioLog.</div>'
        elif emparejar_restante > 0:
            accion = (f'<div class="espera">Emparejando… quedan {int(emparejar_restante)} segundos.<br>'
                      f'Ahora, en RadioLog, pulse <b>«Buscar en mi red»</b>.</div>')
        else:
            accion = ('<form method="post" action="/emparejar"><button class="grande" type="submit">Emparejar con RadioLog</button></form>'
                      '<p class="peq">Después, en el plazo de 2 minutos, pulse «Buscar en mi red» en RadioLog.</p>')
        datos = f"""
<div class="caja">
 <h2>Conectar RadioLog (sin teclear la clave)</h2>
 <ol class="pasos">
  <li>En RadioLog: <b>Configuración → Radio / CAT → Método: Radio remota</b>.</li>
  <li>Aquí: pulse <b>Emparejar con RadioLog</b>.</li>
  <li>En RadioLog: pulse <b>«Buscar en mi red»</b>. Se rellena todo solo.</li>
 </ol>
 {accion}
</div>
<details class="caja">
 <summary>Prefiero teclear los datos a mano</summary>
 <table>
  <tr><td>Dirección</td><td><b>{e(direccion)}</b></td></tr>
  <tr><td>Puerto</td><td><b>{int(estado.get('puerto_red', 8765))}</b></td></tr>
  <tr><td>Clave</td><td><b id="clave">{clave}</b> <button onclick="copiar()">Copiar</button></td></tr>
 </table>
 <p class="peq">Si «{e(direccion)}» no funciona en su ordenador, use la dirección numérica: <b>{otras or 'sin red'}</b></p>
</details>"""
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5"><title>RadioLog · Servidor de radio</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px;font-size:20px}}
 h1{{font-size:26px;margin:0 0 14px}} h2{{font-size:22px;margin:0 0 6px}}
 .estado{{background:{fondo};color:{texto};border-radius:12px;padding:16px;font-size:24px;margin-bottom:16px}}
 .caja{{background:#1e293b;border-radius:12px;padding:16px;margin-top:16px}}
 .ok{{background:#14532d;color:#bbf7d0;border-radius:10px;padding:14px;font-size:22px}}
 .espera{{background:#78350f;color:#fde68a;border-radius:10px;padding:14px;font-size:22px}}
 .pasos{{padding-left:22px;line-height:1.5}} summary{{cursor:pointer;font-size:20px;color:#cbd5e1}}
 table{{width:100%;border-collapse:collapse;margin-top:10px}} td{{padding:10px 6px;border-bottom:1px solid #334155;font-size:26px}} td:first-child{{color:#94a3b8;width:34%}}
 .peq,small{{color:#94a3b8;font-size:16px}} button{{font-size:18px;padding:6px 14px;margin-left:10px}}
 button.grande{{font-size:24px;padding:14px 24px;margin:6px 0;background:#16a34a;color:#fff;border:0;border-radius:10px}}
 #clave{{font-family:Consolas,monospace;letter-spacing:1px}}
</style></head><body>
<h1>RadioLog · Servidor de radio</h1>
<div class="estado">{icono} {e(estado.get('mensaje', ''))}</div>
{filas}{datos}
<script>function copiar(){{navigator.clipboard&&navigator.clipboard.writeText(document.getElementById('clave').textContent)}}</script>
</body></html>"""


class _Manejador(BaseHTTPRequestHandler):
    datos = DIR_DATOS

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            cuerpo = pagina(leer_estado(self.datos), socket.gethostname(), ips_locales(), emparejamiento_restante(self.datos),
                            ultimo_emparejado(self.datos)).encode("utf-8")
            tipo = "text/html; charset=utf-8"
        elif self.path.split("?")[0] == "/estado.json":
            e = leer_estado(self.datos)
            e.pop("clave", None)                           # la clave solo está en la página, no en el JSON
            cuerpo, tipo = json.dumps(e, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self):
        if self.path.split("?")[0] != "/emparejar":
            self.send_error(404)
            return
        abrir_emparejamiento(self.datos)
        self.send_response(303)                            # de vuelta a la página, que ya dice «Emparejando…»
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):                             # sin ruido en el registro
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Página de datos del servidor de radio de RadioLog")
    ap.add_argument("--puerto", type=int, default=80)
    ap.add_argument("--escucha", default="0.0.0.0")
    a = ap.parse_args(argv)
    ThreadingHTTPServer((a.escucha, a.puerto), _Manejador).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
