#!/usr/bin/env python3
"""
tools/radiolog_servidor.py -- RadioLog
Servidor de radio: se deja junto a la radio (un PC o una Raspberry Pi) y RadioLog se conecta a él desde otro sitio.

Ejemplos:
    python radiolog_servidor.py --nueva-clave                          # saca una clave aleatoria (cópiala en RadioLog y aquí)
    python radiolog_servidor.py --puerto-serie /dev/ttyUSB0 --modelo ftdx10 --clave-fichero clave.txt
    python radiolog_servidor.py --puerto-serie COM3 --modelo ftdx10 --clave "mi-clave-larga"

Radios admitidas en esta etapa: Yaesu FTDX10, FT-991A y FT-710 por CAT directo (las que RadioLog ya maneja así).
Sin pantalla y sin PyQt: Python 3.9+ y pyserial (pip install pyserial). Para el audio, además numpy y sounddevice (en Raspberry Pi: sudo apt install libportaudio2).
Todo va cifrado con TLS (certificado propio que se crea la primera vez; hace falta el programa openssl o «pip install cryptography»). Para limitarlo a tu red o a Tailscale: --redes-permitidas.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Funciona desde el repositorio (tools/) y desde la carpeta que se copia a la Raspberry Pi (el script junto a core/).
_AQUI = Path(__file__).resolve().parent
for _cand in (_AQUI, _AQUI.parent):
    if (_cand / "core" / "remoto").is_dir():
        sys.path.insert(0, str(_cand))
        break

MODELOS = ("ftdx10", "ft-991a", "ft-710")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Servidor de radio de RadioLog")
    ap.add_argument("--nueva-clave", action="store_true", help="muestra una clave nueva y sale")
    ap.add_argument("--puerto-serie", help="puerto de la radio (COM3, /dev/ttyUSB0…), o «auto»: el servidor arranca aunque la radio esté apagada y la busca solo por USB cada pocos segundos")
    ap.add_argument("--estado-radio-fichero", default="", help="fichero donde el servidor deja escrito cómo está la radio (lo lee la página de la Raspberry)")
    ap.add_argument("--baudios", type=int, default=38400)
    ap.add_argument("--modelo", choices=MODELOS, default="ftdx10")
    ap.add_argument("--escucha", default="0.0.0.0", help="dirección en la que escucha (por defecto, todas)")
    ap.add_argument("--puerto", type=int, default=8765, help="puerto de red (por defecto 8765)")
    ap.add_argument("--clave", default="", help="clave compartida (mejor --clave-fichero o la variable RADIOLOG_CLAVE)")
    ap.add_argument("--clave-fichero", default="", help="fichero de texto con la clave")
    ap.add_argument("--audio-entrada", default="", help="tarjeta de sonido de la radio, por un trozo de su nombre (ej. \"USB AUDIO CODEC\"); sin esto no hay audio")
    ap.add_argument("--audio-salida", default="", help="tarjeta de sonido por la que se manda la voz a la radio (trozo de su nombre); sin esto no se puede transmitir audio")
    ap.add_argument("--tx-pico", type=float, default=0.8, help="tope de amplitud del audio que se manda a la radio, de 0.05 a 1 (por defecto 0.8)")
    ap.add_argument("--tx-ganancia", type=float, default=1.0, help="ganancia del audio de transmisión en el servidor (por defecto 1)")
    ap.add_argument("--listar-audio", action="store_true", help="muestra las tarjetas de sonido que ve el sistema y sale")
    ap.add_argument("--max-tx", type=float, default=180.0, help="tiempo máximo seguido transmitiendo, en segundos (por defecto 180)")
    ap.add_argument("--sin-cifrar", action="store_true", help="NO cifrar (solo para una red local de confianza; por defecto todo va cifrado con TLS)")
    ap.add_argument("--carpeta-cert", default="", help="donde se guarda el certificado del servidor (por defecto ~/.radiolog-servidor)")
    ap.add_argument("--redes-permitidas", default="", help="solo aceptar conexiones de estas redes, separadas por comas (ej. 192.168.1.0/24,100.64.0.0/10)")
    ap.add_argument("--sin-descubrimiento", action="store_true", help="NO anunciarse en la red local (por defecto, RadioLog puede encontrar el servidor con «Buscar en mi red»)")
    ap.add_argument("--puerto-descubrimiento", type=int, default=8766, help="puerto UDP en el que contesta a «¿hay servidor?» (por defecto 8766)")
    ap.add_argument("--emparejar-fichero", default="", help="fichero con la hora límite del emparejamiento (lo escribe la página de la Raspberry)")
    ap.add_argument("--emparejar", type=float, default=0.0, help="abre el emparejamiento estos segundos al arrancar: el primer RadioLog que pulse «Buscar en mi red» recibe la clave sin teclearla")
    ap.add_argument("--log-fichero", default="", help="guarda también el registro del servidor en este fichero (se rota solo)")
    ap.add_argument("--detalle", action="store_true", help="más información en pantalla")
    a = ap.parse_args(argv)

    from core.remoto import protocolo as P
    if a.nueva_clave:
        print(P.clave_nueva())
        return 0
    if a.listar_audio:
        from core.remoto import audio as AU
        print("ENTRADAS:")
        for d in AU.listar(True):
            print(f"  {d['nombre']}  [{d['api']}]")
        print("SALIDAS:")
        for d in AU.listar(False):
            print(f"  {d['nombre']}  [{d['api']}]")
        return 0

    clave = a.clave
    if not clave and a.clave_fichero:
        clave = Path(a.clave_fichero).read_text(encoding="utf-8").strip()
    clave = clave or P.clave_de_entorno()
    if not a.puerto_serie:
        ap.error("falta --puerto-serie")
    if len(clave) < 8:
        ap.error("falta la clave (mínimo 8 caracteres): --clave, --clave-fichero o la variable RADIOLOG_CLAVE. Para sacar una: --nueva-clave")

    logging.basicConfig(level=logging.DEBUG if a.detalle else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if a.log_fichero:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(a.log_fichero, maxBytes=500_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    from core.cat_engine import CatEngine
    from core.remoto.servidor import ServidorRadio

    auto = a.puerto_serie.lower() == "auto"
    motor = CatEngine(backend="directo", port="" if auto else a.puerto_serie, baudrate=a.baudios, model=a.modelo)
    buscar = None
    if auto:
        from core.remoto.autoconfig import buscar_radio as buscar
    srv = ServidorRadio(motor, clave, host=a.escucha, puerto=a.puerto, max_tx_s=a.max_tx, audio_entrada=a.audio_entrada or None,
                        audio_salida=a.audio_salida or None, tx_pico=a.tx_pico, tx_ganancia=a.tx_ganancia,
                        cifrado=not a.sin_cifrar, carpeta_cert=Path(a.carpeta_cert) if a.carpeta_cert else Path.home() / ".radiolog-servidor",
                        redes_permitidas=[r for r in a.redes_permitidas.split(",") if r.strip()],
                        emparejar_fichero=a.emparejar_fichero or None, buscar_radio=buscar, estado_radio_fichero=a.estado_radio_fichero or None)
    srv.iniciar()
    if a.emparejar > 0:
        srv.abrir_emparejamiento(a.emparejar)
    anuncio = None
    if not a.sin_descubrimiento:
        from core.remoto.autoconfig import NOMBRES
        from core.remoto.descubrimiento import Respondedor
        anuncio = Respondedor(srv.puerto, radio=NOMBRES.get(a.modelo, a.modelo), cifrado=not a.sin_cifrar, puerto_udp=a.puerto_descubrimiento, emparejando=srv.emparejamiento_abierto,
                              redes_permitidas=[r for r in a.redes_permitidas.split(",") if r.strip()])
        anuncio.iniciar()
    print(f"Servidor de radio en marcha: {a.escucha}:{srv.puerto} · radio {'(búsqueda automática)' if auto else a.modelo + ' en ' + a.puerto_serie}. Ctrl+C para parar.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        if anuncio is not None:
            anuncio.detener()
        srv.detener()
        try:
            motor.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
