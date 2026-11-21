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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
for _cand in (_AQUI, _AQUI.parent):
    if (_cand / "core" / "remoto").is_dir():
        sys.path.insert(0, str(_cand))
        break
from core.remoto import autoconfig as AC  # noqa: E402
from core.remoto import tailscale as TS  # noqa: E402

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


def estado_para_asistente(datos: Path = DIR_DATOS) -> dict:
    """Lo que el asistente de RadioLog necesita saber de esta Raspberry sin que el usuario abra esta página: cómo está respecto a Tailscale (incluido el enlace para
    autorizarla), si ya se emparejó alguna vez, si hay un emparejamiento abierto ahora y si la VPN está probada de punta a punta. Nada secreto (la clave nunca sale)."""
    return {"tailscale": tailscale_estado(),
            "enlazada": ultimo_emparejado(datos, maximo_s=10 ** 12) is not None,
            "emparejando_s": emparejamiento_restante(datos),
            "vpn_probada": bool(_leer_json(datos / "vpn_probada.json"))}


def tailscale_estado() -> dict:
    """Cómo está esta Raspberry respecto a Tailscale (ver core/remoto/tailscale.py). Función aparte para poder sustituirla en las pruebas."""
    return TS.estado_propio()


URL_CONSOLA_TAILSCALE = "https://login.tailscale.com/admin/machines"


def pasos_pagina(estado: dict, ts: dict, enlazada: bool, vpn_probada: dict | None) -> list[dict]:
    """Los pasos que hay que dar, EN ORDEN, y cuáles están ya hechos: {id, titulo, hecho, fuera}. `fuera` = solo hace falta para usar la radio desde fuera de casa.
    El primero que no está hecho es el que la página enseña grande («AHORA»)."""
    corriendo = ts.get("estado") == "Running"
    return [
        {"id": "enlazar", "titulo": "Enlazar la Raspberry con RadioLog", "hecho": bool(enlazada), "fuera": False},
        {"id": "vpn", "titulo": "Unir la Raspberry a Tailscale (la VPN)", "hecho": corriendo, "fuera": True},
        {"id": "caducidad", "titulo": "Que la clave de la VPN no caduque", "hecho": corriendo and not ts.get("caduca"), "fuera": True},
        {"id": "prueba", "titulo": "Probar desde RadioLog por la VPN", "hecho": bool(vpn_probada), "fuera": True},
    ]


def _cuerpo_enlazar(emparejar_restante: int) -> str:
    if emparejar_restante > 0:
        return ('<div class="enlace ambar">⏳ Emparejando… quedan ' + str(int(emparejar_restante)) + ' segundos<br>'
                '<small>RadioLog recoge la clave solo (con el asistente de VPN abierto). Si no, pulse el botón verde <b>«Buscar en mi red local»</b>.</small></div>')
    return ('<ol class="mini"><li>En RadioLog: <b>Configuración → Radio / CAT</b> y elija <b>«Radio remota»</b>.</li>'
            '<li>Aquí: pulse el <b>botón azul</b>.</li>'
            '<li>En RadioLog: pulse el botón verde <b>«Buscar en mi red local»</b>. Se rellena todo solo.</li></ol>'
            '<form method="post" action="/emparejar"><button class="enlace azul parpadeo" type="submit">Emparejar con RadioLog<br>'
            '<small>Pulse aquí para enlazar</small></button></form>')


def _cuerpo_vpn(ts: dict) -> str:
    e = html.escape
    if not ts.get("instalado"):
        return '<div class="enlace ambar">Esta imagen no trae Tailscale: hace falta una imagen más nueva.</div>'
    if ts.get("url_login"):
        return ('<div class="enlace ambar">Abra este enlace <b>en su ordenador o móvil</b> e inicie sesión con su cuenta de Tailscale:<br>'
                f'<a href="{e(ts["url_login"])}" target="_blank" style="color:#fef3c7;word-break:break-all">{e(ts["url_login"])}</a></div>')
    return ('<p>Esto solo hace falta para usar la radio <b>fuera de casa</b>. Necesita una cuenta gratuita de Tailscale.</p>'
            '<form method="post" action="/tailscale/conectar"><button class="enlace azul" type="submit">Conectar a Tailscale</button></form>')


def _cuerpo_caducidad(ts: dict) -> str:
    e = html.escape
    nombre = e(ts.get("nombre_vpn") or ts.get("nombre") or "radiolog")
    fecha = f" Ahora caduca el <b>{e(ts['caduca'][:10])}</b>." if ts.get("caduca") else ""
    return ('<p>Si no se hace, a los 6 meses la VPN dejaría de funcionar y no se podría arreglar a distancia.' + fecha + '</p>'
            '<ol class="mini"><li>Pulse el botón de abajo e inicie sesión con su cuenta de Tailscale.</li>'
            f'<li>Busque «<b>{nombre}</b>» en la lista.</li>'
            '<li>Pulse los tres puntos <b>⋯</b> y elija <b>«Disable key expiry»</b>.</li></ol>'
            f'<a class="enlace azul" href="{URL_CONSOLA_TAILSCALE}" target="_blank" style="text-decoration:none">Abrir la consola de Tailscale</a>')


def _cuerpo_prueba() -> str:
    return ('<ol class="mini"><li>En RadioLog: <b>Configuración → Radio / CAT</b>, botón violeta <b>«Asistente: usar la radio desde fuera de casa»</b>.</li>'
            '<li>Pulse <b>«Siguiente»</b> hasta el final (todo se guarda y se prueba solo).</li>'
            '<li>Pulse <b>«Conectar»</b> en RadioLog una vez.</li></ol>'
            '<p class="peq">Con esto la Raspberry comprueba que RadioLog llega por la VPN, y se marcará «lista para llevar».</p>')


def _linea_hecha(p: dict, ts: dict, emparejado: dict | None) -> str:
    e = html.escape
    if p["id"] == "enlazar":
        extra = f" (equipo {e(str(emparejado.get('ip', '')))})" if emparejado else ""
        return "Enlazada con RadioLog" + extra
    if p["id"] == "vpn":
        nombre = f" como «{e(ts['nombre'])}» ({e(ts['ip'])})" if ts.get("nombre") and ts.get("ip") else ""
        return "Unida a Tailscale" + nombre
    if p["id"] == "caducidad":
        return "La clave de la VPN no caduca"
    return "Probada desde RadioLog por la VPN"


def paso_ahora(pasos: list[dict], foco: str = "", emparejado: dict | None = None) -> int | None:
    """Cuál de los pasos se enseña grande («AHORA»): el primero que falta, salvo que quien abre la página pida otro con ?paso=… (el asistente de RadioLog manda a
    «vpn» para unir la Raspberry a Tailscale, o a «enlazar» para volver a emparejar). Si el paso pedido ya está hecho, se sigue con el siguiente que falte
    (así, tras unirla a Tailscale, toca la caducidad); «enlazar» se puede repetir (reemparejar) mientras no se acabe de enlazar."""
    primero = next((i for i, p in enumerate(pasos) if not p["hecho"]), None)
    ids = [p["id"] for p in pasos]
    if foco not in ids:
        return primero
    i = ids.index(foco)
    if not pasos[i]["hecho"] or (foco == "enlazar" and not emparejado):
        return i
    siguiente = next((k for k in range(i + 1, len(pasos)) if not pasos[k]["hecho"]), None)
    return siguiente if siguiente is not None else primero


def _bloque_pasos(estado: dict, ts: dict, enlazada: bool, vpn_probada: dict | None, emparejar_restante: int, emparejado: dict | None, foco: str = "") -> str:
    """Todo lo que el usuario tiene que hacer, como una lista de pasos: el que toca AHORA, grande y con su botón; los hechos, en una línea verde; los que faltan, en gris."""
    pasos = pasos_pagina(estado, ts, enlazada, vpn_probada)
    ahora = paso_ahora(pasos, foco, emparejado)
    total = len(pasos)
    hechos = sum(1 for p in pasos if p["hecho"])
    cuerpos = {"enlazar": lambda: _cuerpo_enlazar(emparejar_restante), "vpn": lambda: _cuerpo_vpn(ts), "caducidad": lambda: _cuerpo_caducidad(ts),
               "prueba": _cuerpo_prueba}
    sal = ['<div class="sugerencia">💡 Lo más fácil: en RadioLog, <b>Configuración → Radio / CAT → «Radio remota» → botón violeta «Asistente: usar la radio desde fuera de casa»</b> '
           'hace todos estos pasos por usted. Esta página sirve para ver cómo va y como ayuda si algo falla.</div>']
    if emparejado:
        sal.append(f'<div class="enlace verde">✔ ENLAZADO<br><small>Emparejado con el equipo {html.escape(str(emparejado.get("ip", "")))}. '
                   f'Ya puede cerrar esta página y conectar en RadioLog.</small></div>')
    if ahora is None:
        if estado.get("estado") == "listo":
            sal.append('<div class="enlace verde" style="font-size:30px">✔ LISTA PARA LLEVAR<br><small>Todo comprobado. Ya puede llevársela: la radio, el cable USB y '
                       'el cable de audio, conectados a la Raspberry.</small></div>')
        else:
            sal.append('<div class="enlace ambar">Casi: falta que la radio esté conectada y encendida.</div>')
    else:
        sal.append(f'<div class="progreso">Paso <b>{ahora + 1}</b> de {total}'
                   f'<div class="barra"><i style="width:{int(100 * hechos / total)}%"></i></div></div>')
    sal.append('<ol class="pasos">')
    fuera_marcado = False
    for i, p in enumerate(pasos):
        if p["fuera"] and not fuera_marcado:
            fuera_marcado = True
            nota = "✔ Ya puede usar la radio desde su casa." if pasos[0]["hecho"] else "Lo siguiente es solo para usarla desde fuera de casa."
            sal.append(f'</ol><div class="sep">{nota}<br><small>Para usarla desde otro sitio, siga:</small></div><ol class="pasos" start="{i + 1}">')
        if i == ahora:
            sal.append(f'<li class="paso ahora"><div class="cab"><span class="tag">AHORA</span> {html.escape(p["titulo"])}</div>'
                       f'<div class="cuerpo">{cuerpos[p["id"]]()}</div></li>')
        elif p["hecho"]:
            sal.append(f'<li class="paso hecho">✔ {_linea_hecha(p, ts, emparejado)}</li>')
        else:
            sal.append(f'<li class="paso despues">{html.escape(p["titulo"])} <small>· después</small></li>')
    sal.append("</ol>")
    return "".join(sal)


def pagina(estado: dict, nombre_equipo: str, ips: list[str], emparejar_restante: int = 0, emparejado: dict | None = None,
           tailscale: dict | None = None, vpn_probada: dict | None = None, enlazada: bool = False, foco: str = "") -> str:
    """El HTML de la página (autocontenido, se refresca solo): el estado de la radio y, debajo, la lista de pasos con el que toca ahora bien grande.
    `enlazada`: ya se emparejó alguna vez con un RadioLog (aunque hace rato). `foco`: el paso que se quiere ver grande (?paso=vpn / ?paso=enlazar)."""
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
        datos = _bloque_pasos(estado, tailscale or {"instalado": False}, bool(enlazada or emparejado), vpn_probada, emparejar_restante, emparejado, foco)
        datos += f"""
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
<meta http-equiv="refresh" content="{2 if emparejar_restante > 0 else 5}"><title>RadioLog · Servidor de radio</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:18px;font-size:21px;max-width:880px;margin-left:auto;margin-right:auto}}
 h1{{font-size:28px;margin:0 0 14px}}
 .estado{{background:{fondo};color:{texto};border-radius:12px;padding:16px;font-size:24px;margin-bottom:12px}}
 .caja{{background:#1e293b;border-radius:12px;padding:16px;margin-top:20px}}
 summary{{cursor:pointer;font-size:20px;color:#cbd5e1}}
 table{{width:100%;border-collapse:collapse;margin-top:10px}} td{{padding:10px 6px;border-bottom:1px solid #334155;font-size:24px}} td:first-child{{color:#94a3b8;width:34%}}
 .peq,small{{color:#94a3b8;font-size:16px}} button{{font-size:18px;padding:6px 14px;margin-left:10px}}
 #clave{{font-family:Consolas,monospace;letter-spacing:1px}}
 .sugerencia{{background:#1e3a5f;color:#dbeafe;border-radius:12px;padding:12px 16px;margin:12px 0 4px;font-size:19px}}
 .progreso{{font-size:21px;color:#cbd5e1;margin:14px 0 6px}} .barra{{height:12px;background:#334155;border-radius:6px;overflow:hidden;margin-top:6px}}
 .barra i{{display:block;height:100%;background:#22c55e}}
 ol.pasos{{list-style:none;padding:0;margin:0}} .paso{{border-radius:14px;margin:12px 0;padding:14px 18px}}
 .paso.hecho{{background:#14532d;color:#bbf7d0;font-size:20px}}
 .paso.despues{{background:#1e293b;color:#94a3b8;font-size:20px}}
 .paso.ahora{{background:#1e293b;border:3px solid #3b82f6;padding:18px 20px}}
 .cab{{font-size:27px;font-weight:bold;margin-bottom:10px}} .tag{{background:#3b82f6;color:#fff;border-radius:8px;padding:2px 10px;font-size:20px;margin-right:6px}}
 .sep{{text-align:center;color:#cbd5e1;font-size:20px;margin:22px 0 4px;padding-top:14px;border-top:2px dashed #475569}}
 ol.mini{{padding-left:26px;line-height:1.6;font-size:22px;margin:8px 0 14px}}
 .enlace{{display:block;width:100%;box-sizing:border-box;text-align:center;font-size:26px;font-weight:bold;padding:18px 20px;margin:8px 0;border:0;border-radius:12px;color:#fff}}
 .enlace small{{display:block;font-size:17px;font-weight:normal;margin-top:4px;color:inherit;opacity:.92}}
 button.enlace{{margin-left:0;cursor:pointer}}
 .azul{{background:#2563eb}} .ambar{{background:#b45309;color:#fef3c7}} .verde{{background:#15803d;color:#dcfce7}}
 .parpadeo{{animation:lat 1.2s ease-in-out infinite}} @keyframes lat{{50%{{opacity:.55}}}}
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
            foco = (urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("paso") or [""])[0]
            enlazada = ultimo_emparejado(self.datos, maximo_s=10 ** 12) is not None            # ¿se emparejó alguna vez, aunque hace rato?
            cuerpo = pagina(leer_estado(self.datos), socket.gethostname(), ips_locales(), emparejamiento_restante(self.datos), ultimo_emparejado(self.datos),
                            tailscale_estado(), _leer_json(self.datos / "vpn_probada.json"), enlazada, foco).encode("utf-8")
            tipo = "text/html; charset=utf-8"
        elif self.path.split("?")[0] == "/estado.json":
            e = leer_estado(self.datos)
            e.pop("clave", None)                           # la clave solo está en la página, no en el JSON
            e.update(estado_para_asistente(self.datos))
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
        ruta = self.path.split("?")[0]
        if ruta not in ("/emparejar", "/tailscale/conectar"):
            self.send_error(404)
            return
        if ruta == "/tailscale/conectar":
            TS.iniciar_union(socket.gethostname().split(".")[0] or "radiolog", fichero_salida=str(self.datos / "tailscale_up.log"))
            vuelta = "/?paso=vpn"                          # que la página siga en ese paso (si no, volvería al primero)
        else:
            abrir_emparejamiento(self.datos)
            vuelta = "/?paso=enlazar"
        self.send_response(303)                            # de vuelta a la página, que ya dice «Emparejando…»
        self.send_header("Location", vuelta)
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
