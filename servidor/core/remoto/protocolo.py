"""
core/remoto/protocolo.py -- RadioLog
Protocolo del mando remoto: una línea de JSON por mensaje sobre una conexión TCP.

  servidor -> cliente  {"t":"hola","v":1,"reto":"<hex>"}
  cliente  -> servidor {"t":"auth","resp":"<hmac del reto del servidor>","reto":"<hex del cliente>"}
  servidor -> cliente  {"t":"ok","resp":"<hmac del reto del cliente>","caps":{...},"modelo":"..."}     (o {"t":"error","msg":"..."} y cierra)
  después:
    servidor -> cliente  {"t":"estado","s":{...campos de CatState...},"radio":true|false}
    cliente  -> servidor {"t":"cmd","id":n,"op":"set_frequency","a":[7.1]}        (solo las órdenes de ORDENES)
    servidor -> cliente  {"t":"resp","id":n,"ok":true|false,"msg":"..."}
    cliente  -> servidor {"t":"ping","ts":<reloj del cliente>}  /  servidor -> cliente {"t":"pong","ts":<el mismo>}  (el cliente mide así la latencia)
  servidor -> cliente  {"t":"audio","n":seq,"d":"<base64 PCM16 8 kHz mono>"}      audio de recepción (tras la orden audio_rx)
  cliente  -> servidor {"t":"audio_tx","n":seq,"d":"<base64 PCM16 8 kHz mono>"}   audio de transmisión (el servidor solo lo reproduce con PTT puesto)

La clave nunca viaja: cada lado demuestra que la tiene con un HMAC-SHA256 del reto del otro, unido a la huella del certificado TLS de la conexión.
Por defecto todo va cifrado con TLS (certificado propio del servidor, ver certificado.py). Sin cifrar solo se admite si se pide a propósito, para red local.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import fields

VERSION_PROTOCOLO = 1
PUERTO_DEFECTO = 8765
MAX_LINEA = 256 * 1024                 # una línea más larga que esto es un error o un ataque: se corta la conexión

# Órdenes que el cliente puede mandar -> (método del motor CAT, tipos de los argumentos). Nada fuera de esta lista se ejecuta nunca.
ORDENES: dict[str, tuple[str, tuple[type, ...]]] = {
    "set_frequency":      ("set_frequency",      (float,)),
    "set_vfob_frequency": ("set_vfob_frequency", (float,)),
    "set_mode":           ("set_mode",           (str,)),
    "set_active_vfo":     ("set_active_vfo",     (int,)),
    "set_split":          ("set_split",          (bool,)),
    "vfo_swap":           ("vfo_swap",           ()),
    "vfo_a_to_b":         ("vfo_a_to_b",         ()),
    "vfo_b_to_a":         ("vfo_b_to_a",         ()),
    "set_af_gain":        ("set_af_gain",        (int,)),
    "set_rf_gain":        ("set_rf_gain",        (int,)),
    "set_squelch":        ("set_squelch",        (int,)),
    "set_mic_gain":       ("set_mic_gain",       (int,)),
    "set_power":          ("set_power",          (int,)),
    "set_nb":             ("set_nb",             (bool,)),
    "set_nr":             ("set_nr",             (bool,)),
    "set_notch":          ("set_notch",          (bool,)),
    "set_proc":           ("set_proc",           (bool,)),
    "set_vox":            ("set_vox",            (bool,)),
    "set_att":            ("set_att",            (int,)),
    "set_ipa":            ("set_ipa",            (int,)),
    "set_agc":            ("set_agc",            (int,)),
    "tune":               ("tune",               ()),
    "set_ptt":            ("set_ptt",            (bool,)),      # el servidor lo vigila (ver servidor.py)
    "refrescar":          ("refresh_rig_params", ()),
    "leer_filtros":       ("leer_filtros_rx",    ()),
    "leer_tuner":         ("leer_tuner",         ()),
    "barrido":            ("__barrido__",        (float, float, float)),   # centro (MHz), ancho (kHz), paso (kHz): la atiende el servidor (core/barrido_rf.py)
    "barrido_cancelar":   ("__barrido_cancelar__", ()),
    "audio_rx":           ("__audio_rx__",       (bool,)),      # la atiende el propio servidor (arranca/para la captura de audio)
    "audio_tx":           ("__audio_tx__",       (bool,)),      # abre/cierra la salida de audio hacia la radio (el audio solo pasa con PTT puesto)
}


def _campos_estado() -> list[str]:
    from core.cat_engine import CatState
    return [f.name for f in fields(CatState)]


def estado_a_dict(estado) -> dict:
    """Los campos de CatState como diccionario sencillo de JSON."""
    return {n: getattr(estado, n) for n in _campos_estado() if hasattr(estado, n)}


def aplicar_estado(estado, datos: dict) -> None:
    """Pone en `estado` (un CatState) los campos recibidos; ignora los que no conoce."""
    conocidos = set(_campos_estado())
    for k, v in (datos or {}).items():
        if k in conocidos:
            setattr(estado, k, v)


def normalizar_caps(caps):
    """JSON convierte las claves numéricas de los diccionarios en texto («options»: {0: …}); aquí se devuelven a número."""
    if isinstance(caps, dict):
        salida = {}
        for k, v in caps.items():
            if isinstance(k, str) and k.lstrip("-").isdigit():
                k = int(k)
            salida[k] = normalizar_caps(v)
        return salida
    if isinstance(caps, list):
        return [normalizar_caps(x) for x in caps]
    return caps


def codificar(mensaje: dict) -> bytes:
    return (json.dumps(mensaje, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def decodificar(linea: bytes) -> dict:
    d = json.loads(linea.decode("utf-8"))
    if not isinstance(d, dict):
        raise ValueError("mensaje no válido")
    return d


class LectorLineas:
    """Trocea lo que llega por el socket en líneas completas."""

    def __init__(self):
        self._buf = b""

    def alimentar(self, datos: bytes) -> list[bytes]:
        self._buf += datos
        if len(self._buf) > MAX_LINEA and b"\n" not in self._buf:
            raise ValueError("línea demasiado larga")
        *lineas, self._buf = self._buf.split(b"\n")
        return [x for x in lineas if x.strip()]


# ── autenticación por clave compartida ───────────────────────────────────────────────────────────────────────────────────────

def reto_nuevo() -> str:
    return secrets.token_hex(16)


def respuesta(clave: str, reto_hex: str, rol: bytes, enlace: bytes = b"") -> str:
    """HMAC-SHA256 del reto con la clave; `rol` (b'cliente' / b'servidor') evita que una respuesta se reutilice en el sentido contrario.

    `enlace` es la huella (SHA-256) del certificado TLS con el que se habla. Cada lado usa la que ve su conexión: si alguien se pone en medio con otro
    certificado, las huellas no coinciden y la autenticación falla aunque el intruso retransmita los mensajes. Sin cifrado va vacío."""
    return hmac.new(clave.encode("utf-8"), bytes.fromhex(reto_hex) + rol + enlace, hashlib.sha256).hexdigest()


def comprobar(clave: str, reto_hex: str, rol: bytes, recibida: str, enlace: bytes = b"") -> bool:
    try:
        return hmac.compare_digest(respuesta(clave, reto_hex, rol, enlace), str(recibida))
    except (ValueError, TypeError):
        return False


def clave_nueva() -> str:
    """Clave aleatoria de 160 bits, fácil de copiar (grupos de cuatro)."""
    c = secrets.token_hex(20)
    return "-".join(c[i:i + 4] for i in range(0, len(c), 4))


def clave_de_entorno() -> str:
    return os.environ.get("RADIOLOG_CLAVE", "")
