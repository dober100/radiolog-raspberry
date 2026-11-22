"""
core/remoto/autoconfig.py -- RadioLog
Autoconfiguración del servidor de radio cuando va en una Raspberry Pi con la imagen de RadioLog: encuentra sola la radio (por el puerto USB), la tarjeta
de sonido de la radio, saca una clave fácil de teclear y lee los ajustes opcionales que el usuario deja en un fichero de texto. Sin Qt: pyserial (y
sounddevice, que es opcional) y nada más.

Solo comandos de lectura: «ID;» a cada puerto, con DTR y RTS siempre en bajo (no se pulsa el PTT ni la clave).
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Callable, Optional

# «ID;» de las Yaesu que el servidor maneja de forma nativa -> modelo del servidor. (Las demás Yaesu contestan, pero el servidor aún no las maneja.)
ID_YAESU_A_MODELO = {"0761": "ftdx10", "0670": "ft-991a", "0800": "ft-710"}
NOMBRES = {"ftdx10": "Yaesu FTDX10", "ft-991a": "Yaesu FT-991A", "ft-710": "Yaesu FT-710"}
BAUDIOS = (38400, 19200, 9600, 4800)                  # el de fábrica de la FTDX10 es 38400; el resto, por si lo cambió en el menú de la radio
_RE_ID = re.compile(r"ID(\d{4});")

# Letras y cifras sin confusión posible (sin 0/O, 1/l/I): para que quien teclee la clave en RadioLog no se equivoque.
_ALFABETO_CLAVE = "abcdefghjkmnpqrstuvwxyz23456789"


def clave_legible() -> str:
    """Clave de 12 caracteres en tres grupos de cuatro (p. ej. «k7mq-x4ec-9ahp»): fácil de leer y de teclear."""
    c = "".join(secrets.choice(_ALFABETO_CLAVE) for _ in range(12))
    return "-".join(c[i:i + 4] for i in range(0, 12, 4))


def leer_ajustes(ruta) -> dict[str, str]:
    """Lee un fichero de ajustes de texto: líneas «clave = valor»; «#» comenta. Un valor vacío cuenta como no puesto. Fichero que falta -> {}."""
    ajustes: dict[str, str] = {}
    try:
        texto = Path(ruta).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ajustes
    for linea in texto.splitlines():
        linea = linea.split("#", 1)[0].strip()
        if "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        k, v = k.strip().lower(), v.strip().strip('"').strip("'")
        if k and v:
            ajustes[k] = v
    return ajustes


# ── radio ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def puertos_serie(listar: Optional[Callable] = None) -> list[str]:
    """Los puertos serie USB del equipo, en orden (en Linux /dev/ttyUSB0, /dev/ttyACM0…)."""
    if listar is None:
        from serial.tools import list_ports
        listar = list_ports.comports
    nombres = []
    for p in listar():
        d = str(getattr(p, "device", p))
        if d.startswith(("/dev/ttyUSB", "/dev/ttyACM", "COM")):
            nombres.append(d)
    return sorted(nombres, key=lambda x: (len(x), x))


def sondear_id(puerto: str, baudios: int, abrir: Optional[Callable] = None, timeout: float = 0.4) -> Optional[str]:
    """Manda «ID;» (solo lectura) y devuelve la respuesta, o None si no contesta. DTR/RTS en bajo desde el primer instante."""
    ser = None
    try:
        if abrir is None:
            from core.serial_util import abrir_puerto
            abrir = abrir_puerto
        ser = abrir(puerto, baudios, timeout)
        try:
            ser.reset_input_buffer()
        except Exception:
            pass
        ser.write(b"ID;")
        datos = ser.read_until(b";")
        texto = bytes(datos).decode("ascii", errors="ignore").strip()
        return texto or None
    except Exception:
        return None
    finally:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass


def buscar_radio(puertos: Optional[list[str]] = None, baudios: tuple = BAUDIOS, sondear: Callable = sondear_id) -> Optional[dict]:
    """Primera Yaesu que contesta a «ID;». Devuelve {puerto, baudios, id, modelo}; `modelo` es None si es una Yaesu que el servidor aún no maneja.
    None si ninguna contesta."""
    for puerto in (puertos if puertos is not None else puertos_serie()):
        for b in baudios:
            m = _RE_ID.search(sondear(puerto, b) or "")
            if m:
                return {"puerto": puerto, "baudios": b, "id": m.group(1), "modelo": ID_YAESU_A_MODELO.get(m.group(1))}
    return None


# ── puerto de la clave de CW ─────────────────────────────────────────────────────────────────────────────────────────
# La FTDX10 muestra por USB DOS puertos del mismo dispositivo: el «Enhanced» (el del CAT, contesta a «ID;») y el «Standard» (PTT y clave por RTS/DTR). Solo en los
# modelos de esta lista, y solo si el «otro puerto» se puede identificar SIN DUDA, se elige solo; en cualquier otro caso se pide configurarlo a mano.
MODELOS_CON_CLAVE_AUTO = ("ftdx10",)


def _ruta_usb(p) -> Optional[str]:
    """El dispositivo USB físico de un puerto (su «location» sin el número de interfaz: «1-1.2:1.0» -> «1-1.2»), o None si no se sabe."""
    loc = getattr(p, "location", None)
    return str(loc).split(":")[0] if loc else None


def puerto_clave_auto(puerto_cat: str, modelo: str, listar: Optional[Callable] = None) -> tuple[Optional[str], str]:
    """(puerto, motivo): el puerto de la clave de CW de la radio que está en `puerto_cat`, o (None, por_qué_no). Solo se devuelve un puerto cuando es INEQUÍVOCO:
    el modelo es de los que tienen su clave en el segundo puerto, y hay exactamente UN puerto más del mismo dispositivo USB (mismo fabricante, producto, número de
    serie y conector físico). No se sondea nada: no se abre ningún puerto."""
    modelo = str(modelo or "").lower()
    if modelo not in MODELOS_CON_CLAVE_AUTO:
        return None, f"con este modelo ({modelo or '?'}) el puerto de la clave no se elige solo: ponlo a mano"
    try:
        if listar is None:
            from serial.tools import list_ports
            listar = list_ports.comports
        puertos = list(listar())
    except Exception as exc:
        return None, f"no se pueden listar los puertos serie ({exc})"
    cat = next((p for p in puertos if str(getattr(p, "device", "")) == str(puerto_cat)), None)
    if cat is None:
        return None, f"no encuentro el puerto de la radio ({puerto_cat})"
    vid, pid = getattr(cat, "vid", None), getattr(cat, "pid", None)
    if vid is None or pid is None:
        return None, "no se puede saber a qué dispositivo USB pertenece el puerto de la radio"
    ruta, serie = _ruta_usb(cat), getattr(cat, "serial_number", None)
    if ruta is None and not serie:
        return None, "no se puede saber qué puertos son del mismo dispositivo USB"
    iguales = []
    for p in puertos:
        if str(getattr(p, "device", "")) == str(puerto_cat):
            continue
        if getattr(p, "vid", None) != vid or getattr(p, "pid", None) != pid:
            continue
        ruta_p, serie_p = _ruta_usb(p), getattr(p, "serial_number", None)
        if ruta is not None and ruta_p is not None:
            if ruta_p != ruta:
                continue
        elif not serie or serie_p != serie:                  # sin ruta física, solo vale un número de serie idéntico y no vacío
            continue
        if serie and serie_p and serie_p != serie:
            continue
        iguales.append(str(p.device))
    if not iguales:
        return None, "la radio solo muestra un puerto USB: no hay otro para la clave"
    if len(iguales) > 1:
        return None, f"hay varios puertos candidatos ({', '.join(sorted(iguales))}): ponlo a mano"
    return iguales[0], ""


# ── audio ────────────────────────────────────────────────────────────────────────────────────────────────────────────
_PREFERIDAS = ("usb audio codec", "usb audio", "usb")
_DESCARTAR = ("hdmi", "monitor", "bcm2835", "headphones")


def elegir_audio(nombres: list[str]) -> str:
    """De la lista de tarjetas de sonido, la de la radio (las Yaesu salen como «USB Audio CODEC»). '' si no hay ninguna USB."""
    utiles = [n for n in nombres if not any(d in n.lower() for d in _DESCARTAR)]
    for clave in _PREFERIDAS:
        for n in utiles:
            if clave in n.lower():
                return n
    return ""


def buscar_audio(listar: Optional[Callable] = None) -> tuple[str, str]:
    """(entrada, salida) con el nombre de la tarjeta de sonido de la radio; ('', '') si no hay o no se puede mirar el audio."""
    try:
        if listar is None:
            from core.remoto import audio as AU
            listar = AU.listar
        return elegir_audio([d["nombre"] for d in listar(True)]), elegir_audio([d["nombre"] for d in listar(False)])
    except Exception:
        return "", ""


# ── lo que se enseña en la página ────────────────────────────────────────────────────────────────────────────────────
RADIO_JSON_CADUCA_S = 30.0                # si el servidor lleva más de esto sin actualizar su estado, se considera que no está funcionando


def estado_visible(base: dict, radio: dict | None, ahora: float | None = None) -> dict:
    """Une lo fijo del arranque (`base`: clave, puerto de red, audio…) con lo que el servidor sabe de la radio AHORA (`radio`: conectada o no, puerto, modelo)
    y lo traduce a un estado y un mensaje en lenguaje llano: «arrancando», «sin_radio», «sin_soporte» o «listo»."""
    import time as _t
    ahora = _t.time() if ahora is None else ahora
    res = dict(base or {})
    if not base:
        res.update(estado="arrancando", mensaje="Arrancando… un momento.")
        return res
    if not radio or ahora - float(radio.get("actualizado", 0)) > RADIO_JSON_CADUCA_S:
        res.update(estado="arrancando", mensaje="Arrancando el servidor… un momento.")
        return res
    modelo = str(radio.get("modelo", ""))
    res.update(modelo=modelo, radio=NOMBRES.get(modelo, modelo), puerto_serie=radio.get("puerto", ""), baudios=radio.get("baudios", 0))
    if radio.get("conectada"):
        res.update(estado="listo", mensaje=f"Todo listo: {NOMBRES.get(modelo, modelo)} conectada.")
    elif radio.get("id_sin_soporte"):
        res.update(estado="sin_soporte", mensaje=f"He encontrado una radio (código {radio['id_sin_soporte']}) que este servidor todavía no maneja. "
                                                 "De momento: Yaesu FTDX10, FT-991A y FT-710.")
    else:
        res.update(estado="sin_radio", mensaje="El servidor está en marcha, pero no encuentro la radio. Compruebe que está encendida y que el cable USB "
                                               "está conectado a la Raspberry. En cuanto se encienda, se conectará sola.")
    return res


# ── decisión conjunta ───────────────────────────────────────────────────────────────────────────────────────────────
def decidir(ajustes: dict, radio: Optional[dict], audio: tuple[str, str]) -> dict:
    """Con los ajustes del usuario (mandan siempre), la radio encontrada y el audio, qué se va a arrancar. Devuelve un dict con `estado`
    («listo», «sin_radio» o «sin_soporte») y un `mensaje` en lenguaje llano."""
    modelo = ajustes.get("modelo", "").lower() or (radio or {}).get("modelo") or ""
    puerto = ajustes.get("puerto_serie") or (radio or {}).get("puerto") or ""
    try:
        baudios = int(ajustes.get("baudios") or (radio or {}).get("baudios") or 38400)
    except ValueError:
        baudios = 38400
    res = {"modelo": modelo, "radio": NOMBRES.get(modelo, ""), "puerto_serie": puerto, "baudios": baudios,
           "audio_entrada": ajustes.get("audio_entrada", audio[0]), "audio_salida": ajustes.get("audio_salida", audio[1])}
    if not puerto:
        res.update(estado="sin_radio", mensaje="No encuentro la radio. Compruebe que está encendida y que el cable USB está conectado a la Raspberry.")
    elif not modelo:
        codigo = (radio or {}).get("id", "")
        extra = f" (código {codigo})" if codigo else ""
        res.update(estado="sin_soporte", mensaje="He encontrado una radio" + extra + " que este servidor todavía no maneja. De momento: Yaesu FTDX10, FT-991A y FT-710.")
    else:
        res.update(estado="listo", mensaje=f"Todo listo: {NOMBRES.get(modelo, modelo)} conectada.")
    return res
