"""
core/barrido_rf.py -- RadioLog
Barrido del receptor por CAT: emula el «scope» de las radios de un solo receptor (p. ej. la FTDX1200), que lo dibujan moviendo el receptor por la banda
y midiendo la señal en cada paso. Aquí se mueve el VFO por pasos, se lee el S-meter en cada uno y se devuelve una fila de espectro (en dB respecto a S9)
para el panorama y la cascada de RF de Control de equipo. No es una SDR: es un S-meter muestreado.

Es igual que en la radio: mientras se barre NO hay audio (se baja la ganancia de audio y al acabar se restaura), y no se barre nunca transmitiendo.
Pase lo que pase, al terminar se devuelve la radio a su frecuencia y su ganancia de audio.
Sin Qt: lo usan el motor CAT (radio local) y el servidor de la radio remota.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("radiolog.barrido")

MAX_PUNTOS = 600            # un barrido más largo que esto tardaría demasiado
MIN_PASO_KHZ = 0.1
MAX_SPAN_KHZ = 1000.0
ASENTAR_S = 0.04            # lo que se espera tras cambiar de frecuencia para que el S-meter se estabilice


def comprobar_parametros(centro_mhz: float, span_khz: float, paso_khz: float) -> str:
    """Texto del problema, o «» si los datos del barrido valen."""
    if not (0.1 <= centro_mhz <= 1000.0):
        return "La frecuencia central del barrido no es válida"
    if not (paso_khz >= MIN_PASO_KHZ):
        return "El paso del barrido es demasiado pequeño"
    if not (paso_khz <= span_khz <= MAX_SPAN_KHZ):
        return "El ancho del barrido no es válido"
    if span_khz / paso_khz + 1 > MAX_PUNTOS:
        return f"Demasiados puntos (máximo {MAX_PUNTOS}): sube el paso o baja el ancho"
    if centro_mhz - span_khz / 2000.0 <= 0.0:
        return "El barrido se sale de la banda"
    return ""


def frecuencias(centro_mhz: float, span_khz: float, paso_khz: float) -> list[float]:
    """Las frecuencias (MHz, al hercio) que se visitan, de menor a mayor, con el centro en medio."""
    n = int(round(span_khz / paso_khz)) + 1
    ini_hz = round(centro_mhz * 1_000_000 - (n - 1) * paso_khz * 1000.0 / 2.0)
    return [round(ini_hz + i * paso_khz * 1000.0) / 1_000_000 for i in range(n)]


def barrer(motor, centro_mhz: float, span_khz: float, paso_khz: float, cancelar: Optional[threading.Event] = None,
           progreso: Optional[Callable[[int, int], None]] = None, asentar_s: float = ASENTAR_S, dormir=time.sleep) -> dict:
    """Hace un barrido y devuelve {"ok", "msg", "completo", "centro_hz", "paso_hz", "db": [...]}.
    `motor` es un CatEngine (o algo con sus métodos: is_connected, get_ptt, get_frequency, set_frequency, get_smeter_db y, si se puede, get/set_af_gain)."""
    res = {"ok": False, "msg": "", "completo": False, "centro_hz": int(round(centro_mhz * 1e6)), "paso_hz": int(round(paso_khz * 1000)), "f0_hz": 0, "db": []}
    error = comprobar_parametros(centro_mhz, span_khz, paso_khz)
    if error:
        res["msg"] = error
        return res
    if not motor.is_connected():
        res["msg"] = "La radio no está conectada"
        return res
    if motor.get_ptt():
        res["msg"] = "No se barre transmitiendo"
        return res
    estado = getattr(getattr(motor, "backend", None), "state", None)
    if estado is not None and getattr(estado, "barriendo", False):
        res["msg"] = "Ya hay un barrido en marcha"
        return res
    f0 = float(motor.get_frequency())
    lista = frecuencias(centro_mhz, span_khz, paso_khz)
    ganancia0 = None
    if estado is not None:
        estado.barriendo = True
    try:
        try:                                                              # como la radio: sin audio mientras barre
            leer = getattr(motor, "leer_af_gain", None)                   # el valor REAL de la radio (el del estado podría estar sin actualizar)
            ganancia0 = int(leer() if leer is not None else motor.get_af_gain())
            motor.set_af_gain(0)
        except Exception:
            ganancia0 = None
        res["ok"] = True
        res["centro_hz"] = int(round((lista[0] + lista[-1]) / 2 * 1e6))
        res["f0_hz"] = int(round(lista[0] * 1e6))
        for i, f in enumerate(lista):
            if cancelar is not None and cancelar.is_set():
                res["msg"] = "Barrido cancelado"
                break
            if motor.get_ptt():
                res["msg"] = "Se ha empezado a transmitir: barrido parado"
                break
            if motor.set_frequency(f) is False:
                res["ok"] = False
                res["msg"] = "La radio no ha aceptado el cambio de frecuencia"
                break
            if estado is not None:
                estado.frequency_mhz = f0                                # la pantalla no sigue al barrido
            dormir(asentar_s)
            res["db"].append(round(float(motor.get_smeter_db()), 1))
            if progreso is not None:
                try:
                    progreso(i + 1, len(lista))
                except Exception:
                    pass
        else:
            res["completo"] = True
    except Exception as e:
        log.warning("El barrido ha fallado: %s", e, exc_info=True)
        res["ok"] = False
        res["msg"] = "El barrido ha fallado"
    finally:
        try:
            motor.set_frequency(f0)                                      # SIEMPRE se devuelve la radio a donde estaba
        except Exception:
            log.error("No se pudo devolver la radio a su frecuencia tras el barrido", exc_info=True)
        if ganancia0 is not None:
            try:
                motor.set_af_gain(ganancia0)
            except Exception:
                log.error("No se pudo restaurar la ganancia de audio tras el barrido", exc_info=True)
        if estado is not None:
            estado.frequency_mhz = f0
            estado.barriendo = False
    return res
