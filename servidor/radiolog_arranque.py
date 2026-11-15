#!/usr/bin/env python3
"""
tools/radiolog_arranque.py -- RadioLog
Arranque automático del servidor de radio (lo lanza el servicio de la imagen de la Raspberry Pi, sin que nadie teclee nada):
  1. lee los ajustes opcionales del usuario (radiolog.txt, en la partición que se ve desde Windows),
  2. busca la tarjeta de sonido de la radio,
  3. saca (la primera vez) y guarda una clave fácil de teclear,
  4. arranca radiolog_servidor.py SIEMPRE, aunque la radio esté apagada: el servidor busca la radio él solo (por USB) cada pocos segundos y se conecta
     en cuanto se enciende; así RadioLog, desde lejos, ve que la Raspberry ya está encendida y espera a la radio,
  5. si el servidor se para, lo vuelve a arrancar.

Variables de entorno (para pruebas o instalaciones distintas): RADIOLOG_AJUSTES (fichero de ajustes), RADIOLOG_DATOS (carpeta de datos).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
for _cand in (_AQUI, _AQUI.parent):
    if (_cand / "core" / "remoto").is_dir():
        sys.path.insert(0, str(_cand))
        break

from core.remoto import autoconfig as AC  # noqa: E402

DIR_DATOS = Path(os.environ.get("RADIOLOG_DATOS", "/var/lib/radiolog"))
CANDIDATOS_AJUSTES = ("/boot/firmware/radiolog.txt", "/boot/radiolog.txt")
ESPERA_RADIO_S = 5.0
PUERTO_RED = 8765


def ruta_ajustes() -> Path:
    env = os.environ.get("RADIOLOG_AJUSTES")
    if env:
        return Path(env)
    for c in CANDIDATOS_AJUSTES:
        if Path(c).exists():
            return Path(c)
    return Path(CANDIDATOS_AJUSTES[0])


def obtener_clave(ajustes: dict, datos: Path) -> str:
    """La del usuario si la puso en radiolog.txt; si no, la guardada; si no hay, se saca una nueva y se guarda (la misma para siempre)."""
    if len(ajustes.get("clave", "")) >= 8:
        return ajustes["clave"]
    f = datos / "clave.txt"
    try:
        c = f.read_text(encoding="utf-8").strip()
        if len(c) >= 8:
            return c
    except OSError:
        pass
    c = AC.clave_legible()
    datos.mkdir(parents=True, exist_ok=True)
    f.write_text(c + "\n", encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return c


def escribir_estado(datos: Path, estado: dict) -> None:
    """Escritura atómica: la página nunca lee un fichero a medias."""
    datos.mkdir(parents=True, exist_ok=True)
    estado = dict(estado, actualizado=time.time())
    tmp = datos / "estado.json.tmp"
    tmp.write_text(json.dumps(estado, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, datos / "estado.json")


AUDIO_DE_FABRICA = "USB Audio CODEC"        # así se llama la tarjeta de sonido de las Yaesu; aparece cuando la radio se enciende


def preparar(datos: Path, ajustes_ruta: Path | None = None, buscar_audio=AC.buscar_audio) -> dict:
    """Decide con qué parámetros arrancar el servidor y lo deja escrito en estado.json (la página lo combina con lo que el servidor sabe de la radio).
    El servidor arranca SIEMPRE, aunque la radio esté apagada: ella se busca sola cuando se enciende. Solo con puerto y modelo fijados en radiolog.txt
    no se busca."""
    ajustes = AC.leer_ajustes(ajustes_ruta or ruta_ajustes())
    clave = obtener_clave(ajustes, datos)
    fijo = bool(ajustes.get("puerto_serie") and ajustes.get("modelo"))
    entrada, salida = ajustes.get("audio_entrada", ""), ajustes.get("audio_salida", "")
    if not (entrada or salida):
        entrada, salida = buscar_audio()
    try:
        baudios = int(ajustes.get("baudios") or 38400)
    except ValueError:
        baudios = 38400
    try:
        puerto_red = int(ajustes.get("puerto", PUERTO_RED))
    except ValueError:
        puerto_red = PUERTO_RED
    cfg = {"modo": "fijo" if fijo else "auto", "modelo": ajustes.get("modelo", "").lower(), "puerto_serie": ajustes.get("puerto_serie", ""), "baudios": baudios,
           "audio_entrada": entrada or AUDIO_DE_FABRICA, "audio_salida": salida or AUDIO_DE_FABRICA, "puerto_red": puerto_red, "clave": clave,
           "redes_permitidas": ajustes.get("redes_permitidas", ""), "estado": "arrancando", "mensaje": "Arrancando el servidor… un momento."}
    escribir_estado(datos, cfg)
    return cfg


def orden_servidor(est: dict, datos: Path) -> list[str]:
    """La línea de órdenes de radiolog_servidor.py para esta configuración."""
    auto = est.get("modo") != "fijo"
    o = [sys.executable, str(_AQUI / "radiolog_servidor.py"), "--puerto-serie", "auto" if auto else est["puerto_serie"], "--baudios", str(est["baudios"]),
         "--modelo", est.get("modelo") or "ftdx10", "--puerto", str(est["puerto_red"]), "--clave-fichero", str(datos / "clave_en_uso.txt"),
         "--carpeta-cert", str(datos / "cert"), "--log-fichero", str(datos / "servidor.log"),
         "--emparejar-fichero", str(datos / "emparejar.json"), "--estado-radio-fichero", str(datos / "radio.json")]
    if est.get("audio_entrada"):
        o += ["--audio-entrada", est["audio_entrada"]]
    if est.get("audio_salida"):
        o += ["--audio-salida", est["audio_salida"]]
    if est.get("redes_permitidas"):
        o += ["--redes-permitidas", est["redes_permitidas"]]
    return o


def main() -> int:
    DIR_DATOS.mkdir(parents=True, exist_ok=True)
    while True:
        est = preparar(DIR_DATOS)
        (DIR_DATOS / "clave_en_uso.txt").write_text(est["clave"] + "\n", encoding="utf-8")
        try:
            os.chmod(DIR_DATOS / "clave_en_uso.txt", 0o600)
        except OSError:
            pass
        try:
            (DIR_DATOS / "radio.json").unlink()                      # lo de la vez anterior no vale
        except OSError:
            pass
        print(f"Arrancando el servidor ({'radio fija' if est['modo'] == 'fijo' else 'búsqueda automática de la radio'})", flush=True)
        proc = subprocess.Popen(orden_servidor(est, DIR_DATOS))
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            return 0
        time.sleep(ESPERA_RADIO_S)                                    # se ha parado: se vuelve a arrancar solo


if __name__ == "__main__":
    sys.exit(main())
