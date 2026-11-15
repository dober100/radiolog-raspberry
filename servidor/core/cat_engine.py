"""
RadioLog - CAT Engine
Capa abstracta para control de radio.

Backends disponibles:
  - simulator     : CAT Simulado (sin radio real)
  - directo       : CAT directo Yaesu por puerto serie (FTDX10, FT-991A, FTDX101...)
  - generic       : CAT genérico via preset OmniRig texto (Kenwood, Elecraft...)
  - civ           : CAT nativo Icom CI-V (IC-706, IC-7300, IC-705...)
  - hamlib        : Hamlib via rigctld TCP
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from core.i18n import t

try:
    import serial
except ImportError:
    serial = None


# ─────────────────────────────────────────────────────────────────────────────
# Capacidades por radio
# ─────────────────────────────────────────────────────────────────────────────
#
# Cada backend devuelve un dict con sus capacidades.
# El Rig Control construye la UI dinámicamente desde este dict.
#
# sliders — lista de dicts:
#   key    : str  — clave interna (af_gain, rf_gain, squelch, mic_gain, power)
#   label  : str  — etiqueta visible
#   min/max: int  — rango
#   unit   : str  — sufijo opcional ("W")
#   style  : str  — "blue" | "amber" | "green"
#
# function_groups — lista de grupos; cada grupo es un dict:
#   name   : str  — etiqueta visible del grupo ("AGC", "Atenuador"…)
#   items  : list — lista de dicts de botones:
#     key    : str  — clave interna
#     type   : str  — "toggle" | "cycle" | "action"
#   label  : str  — para toggle/action
#   options: dict — para cycle: {valor_int: "LABEL"}
#   style  : str  — "" | "amber" | "green"

CAP_YAESU_FTDX10 = {
    "name": "Yaesu FTDX10",
    "smeter": True,
    "vfo_b": True,
    "modes": ["USB","LSB","CW","FT8","FT4","RTTY","PSK31",
              "AM","FM","DATA-U","DATA-L","C4FM"],
    "sliders": [
        {"key":"af_gain",  "label":"AF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"rf_gain",  "label":"RF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"squelch",  "label":"SQUELCH",  "min":0,  "max":255, "style":"blue"},
        {"key":"mic_gain", "label":"MIC GAIN", "min":0,  "max":100, "style":"blue"},
        {"key":"power",    "label":"RF POWER", "min":5,  "max":100, "style":"amber","unit":"W"},
    ],
    "function_groups": [
        {"name": "AGC", "items": [
            {"key":"agc","type":"cycle","style":"amber",
             "options":{0:"AGC OFF",1:"AGC FAST",2:"AGC MID",3:"AGC SLOW",4:"AGC AUTO"}},
            {"key":"nb","type":"toggle","label":"NB"},
            {"key":"nr","type":"toggle","label":"NR"},
        ]},
        {"name": "Atenuador", "items": [
            {"key":"att","type":"cycle",
             "options":{0:"ATT OFF",1:"ATT 6dB",2:"ATT 12dB",3:"ATT 18dB"}},
        ]},
        {"name": "Preamp", "items": [
            {"key":"ipa","type":"cycle",
             "options":{0:"IPO",1:"AMP 1",2:"AMP 2"}},
            {"key":"tune","type":"action","label":"TUNE","style":"green"},
        ]},
    ],
}

CAP_YAESU_FT991A = {
    "name": "Yaesu FT-991A",
    "smeter": True,
    "vfo_b": True,
    "modes": ["USB","LSB","CW","FT8","FT4","RTTY","PSK31",
              "AM","FM","DATA-U","DATA-L","C4FM"],
    "sliders": [
        {"key":"af_gain",  "label":"AF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"rf_gain",  "label":"RF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"squelch",  "label":"SQUELCH",  "min":0,  "max":255, "style":"blue"},
        {"key":"mic_gain", "label":"MIC GAIN", "min":0,  "max":100, "style":"blue"},
        {"key":"power",    "label":"RF POWER", "min":5,  "max":100, "style":"amber","unit":"W"},
    ],
    "function_groups": [
        {"name": "AGC", "items": [
            {"key":"agc","type":"cycle","style":"amber",
             "options":{0:"AGC OFF",1:"AGC FAST",2:"AGC MID",3:"AGC SLOW"}},
            {"key":"nb","type":"toggle","label":"NB"},
            {"key":"nr","type":"toggle","label":"NR"},
        ]},
        {"name": "Atenuador", "items": [
            {"key":"att","type":"cycle",
             "options":{0:"ATT OFF",1:"ATT 6dB",2:"ATT 12dB",3:"ATT 18dB"}},
        ]},
        {"name": "Preamp", "items": [
            {"key":"ipa","type":"cycle",
             "options":{0:"IPO",1:"AMP 1",2:"AMP 2"}},
            {"key":"tune","type":"action","label":"TUNE","style":"green"},
        ]},
    ],
}

CAP_YAESU_FT710 = {
    "name": "Yaesu FT-710",
    "smeter": True,
    "vfo_b": True,
    "modes": ["USB","LSB","CW","FT8","FT4","RTTY","PSK31","AM","FM","DATA-U","DATA-L"],
    "sliders": [
        {"key":"af_gain",  "label":"AF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"rf_gain",  "label":"RF GAIN",  "min":0,  "max":255, "style":"blue"},
        {"key":"squelch",  "label":"SQUELCH",  "min":0,  "max":255, "style":"blue"},
        {"key":"mic_gain", "label":"MIC GAIN", "min":0,  "max":100, "style":"blue"},
        {"key":"power",    "label":"RF POWER", "min":5,  "max":100, "style":"amber","unit":"W"},
    ],
    "function_groups": [
        {"name": "AGC", "items": [
            {"key":"agc","type":"cycle","style":"amber",
             "options":{0:"AGC OFF",1:"AGC FAST",2:"AGC MID",3:"AGC SLOW"}},
            {"key":"nb","type":"toggle","label":"NB"},
            {"key":"nr","type":"toggle","label":"NR"},
        ]},
        {"name": "Atenuador", "items": [
            {"key":"att","type":"cycle",
             "options":{0:"ATT OFF",1:"ATT 6dB",2:"ATT 12dB",3:"ATT 18dB"}},
        ]},
        {"name": "Preamp", "items": [
            {"key":"ipa","type":"cycle",
             "options":{0:"IPO",1:"AMP 1",2:"AMP 2"}},
            {"key":"tune","type":"action","label":"TUNE","style":"green"},
        ]},
    ],
}

# El simulador usa el conjunto completo del FTDX10 para poder testear todo
CAP_SIMULATOR = dict(CAP_YAESU_FTDX10)
CAP_SIMULATOR["name"] = "CAT Simulator"


# ─────────────────────────────────────────────────────────────────────────────
# Estado CAT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CatState:
    connected: bool = False
    frequency_mhz: float = 14.074000
    mode: str = "USB"
    backend_name: str = "Simulator"
    last_error: str = ""
    smeter: int = 0
    po_w: float = 0.0          # potencia de salida (vatios) que mide la radio al transmitir (Yaesu FTDX10: RM5)
    swr: float = 1.0           # ROE que mide la radio al transmitir (Yaesu FTDX10: RM6); 1.0 si no se transmite
    barriendo: bool = False    # se está haciendo un barrido de la banda con el receptor (core/barrido_rf.py): sin audio, y no se sondea la radio
    af_gain: int = 75
    rf_gain: int = 255
    squelch: int = 0
    mic_gain: int = 50
    power_w: int = 100
    nb: bool = False
    nr: bool = False
    notch: bool = False        # notch manual (Yaesu: BP00)
    notch_ok: bool = True      # False si la radio no contesta al notch: el botón de la ventana principal se desactiva
    proc: bool = False         # procesador de voz (Yaesu FTDX10: PR0, 0 = apagado / 1 = encendido)
    tuning: bool = False       # el sintonizador de antena está sintonizando (AC)
    vox: bool = False
    att: int = 0
    ipa: int = 0
    agc: int = 4
    ptt: bool = False
    vfob_freq: float = 14.074
    vfob_leida: bool = True   # False = todavía no se ha leído de la radio (el 14.074 de arriba es un valor por defecto, no un dato): la ventana enseña «—»
    vfob_mode: str = "USB"
    active_vfo: int = 0   # 0 = VFO-A activo, 1 = VFO-B activo (RX)
    tx_vfo:    int = 0    # 0 = TX en VFO-A, 1 = TX en VFO-B
    split:     bool = False  # True cuando RX-VFO != TX-VFO
    cw_inverso: bool = False  # CW-L / CW-R (la radio lo lee como «CW», pero el sentido es el contrario)


# ─────────────────────────────────────────────────────────────────────────────
# Clase base
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("radiolog.cat")

# Sondeos seguidos sin respuesta que hacen falta para dar la radio por desconectada (un fallo suelto no desconecta: la radio puede estar
# ocupada, el USB tardar, un CAT TOT corto…). Cada fallo queda en el registro. Con 1 s entre sondeos, 3 son ~3 s.
FALLOS_SONDEO_MAX = 3


class CatBackendBase:
    name = "Base"

    def __init__(self):
        self.state = CatState(backend_name=self.name)
        self._fallos_sondeo = 0

    def _sondeo_perdido(self, donde: str = "") -> bool:
        """Se llama cuando un sondeo no recibió respuesta de la radio. True si ya son demasiados seguidos y hay que dar la conexión por perdida;
        False si todavía se aguanta (la conexión sigue y se vuelve a preguntar en el siguiente sondeo)."""
        self._fallos_sondeo += 1
        log.warning("CAT %s: sondeo sin respuesta (%d/%d) %s", self.name, self._fallos_sondeo, FALLOS_SONDEO_MAX, donde)
        if self._fallos_sondeo < FALLOS_SONDEO_MAX:
            return False
        log.warning("CAT %s: %d sondeos seguidos sin respuesta: se da la radio por desconectada %s", self.name, self._fallos_sondeo, donde)
        return True

    def _sondeo_ok(self) -> None:
        if self._fallos_sondeo:
            log.info("CAT %s: la radio vuelve a responder tras %d sondeos fallidos", self.name, self._fallos_sondeo)
        self._fallos_sondeo = 0

    def connect(self) -> bool:              raise NotImplementedError
    def disconnect(self) -> None:           raise NotImplementedError
    def is_connected(self) -> bool:         return self.state.connected
    def get_frequency(self) -> float:       return self.state.frequency_mhz
    def set_frequency(self, f) -> bool:     raise NotImplementedError
    def get_mode(self) -> str:              return self.state.mode
    def get_mode_cw(self) -> str:
        """Como get_mode(), pero distinguiendo CW inverso (\"CW-R\") cuando la radio lo indica.
        Solo para calcular sintonía: el modo que se muestra y se guarda sigue siendo \"CW\"."""
        modo = self.state.mode
        return "CW-R" if modo == "CW" and self.state.cw_inverso else modo
    def set_mode(self, mode) -> bool:       raise NotImplementedError
    def poll(self) -> bool:                 return self.state.connected
    def get_capabilities(self) -> dict:     return CAP_SIMULATOR

    def get_smeter(self) -> int:            return self.state.smeter
    def get_po(self) -> float:              return float(getattr(self.state, "po_w", 0.0) or 0.0)
    def get_smeter_db(self) -> float:       return smeter_a_db(self.get_smeter())
    def get_swr(self) -> float:             return float(getattr(self.state, "swr", 1.0) or 1.0)
    def get_af_gain(self) -> int:           return self.state.af_gain
    def set_af_gain(self, v) -> None:       self.state.af_gain  = max(0,min(255,int(v)))
    def get_rf_gain(self) -> int:           return self.state.rf_gain
    def set_rf_gain(self, v) -> None:       self.state.rf_gain  = max(0,min(255,int(v)))
    def get_squelch(self) -> int:           return self.state.squelch
    def set_squelch(self, v) -> None:       self.state.squelch  = max(0,min(255,int(v)))
    def get_mic_gain(self) -> int:          return self.state.mic_gain
    def set_mic_gain(self, v) -> None:      self.state.mic_gain = max(0,min(100,int(v)))
    def get_power(self) -> int:             return self.state.power_w
    def set_power(self, v) -> None:         self.state.power_w  = max(5,min(100,int(v)))
    def get_nb(self) -> bool:               return self.state.nb
    def set_nb(self, on) -> None:           self.state.nb  = bool(on)
    def get_nr(self) -> bool:               return self.state.nr
    def set_nr(self, on) -> None:           self.state.nr  = bool(on)
    def get_notch(self) -> bool:            return self.state.notch
    def set_notch(self, on) -> None:        self.state.notch = bool(on)
    def leer_filtros_rx(self) -> None:      pass
    def get_proc(self) -> bool:             return self.state.proc
    def set_proc(self, on) -> None:         self.state.proc = bool(on)
    def get_tuning(self) -> bool:           return self.state.tuning
    def leer_tuner(self) -> None:           pass
    def get_vox(self) -> bool:              return self.state.vox
    def set_vox(self, on) -> None:          self.state.vox = bool(on)
    def get_att(self) -> int:               return self.state.att
    def set_att(self, v) -> None:           self.state.att = max(0,min(3,int(v)))
    def get_ipa(self) -> int:               return self.state.ipa
    def set_ipa(self, v) -> None:           self.state.ipa = max(0,min(2,int(v)))
    def get_agc(self) -> int:               return self.state.agc
    def set_agc(self, v) -> None:           self.state.agc = max(0,min(4,int(v)))
    def set_ptt(self, on) -> None:          self.state.ptt = bool(on)
    def get_vfob_frequency(self) -> float:  return self.state.vfob_freq
    def get_vfob_mode(self) -> str:         return self.state.vfob_mode
    def get_active_vfo(self) -> int:        return self.state.active_vfo
    def get_split(self) -> bool:            return self.state.split
    def set_split(self, on: bool) -> None:
        self.state.split = bool(on)
        self.state.tx_vfo = (1 - self.state.active_vfo) if on else self.state.active_vfo

    def set_vfob_frequency(self, f) -> bool:
        self.state.vfob_freq = float(f); return True

    def vfo_swap(self) -> None:
        self.state.frequency_mhz, self.state.vfob_freq = \
            self.state.vfob_freq, self.state.frequency_mhz
        self.state.mode, self.state.vfob_mode = \
            self.state.vfob_mode, self.state.mode

    def vfo_a_to_b(self) -> None:
        self.state.vfob_freq = self.state.frequency_mhz
        self.state.vfob_mode = self.state.mode

    def vfo_b_to_a(self) -> None:
        self.state.frequency_mhz = self.state.vfob_freq
        self.state.mode = self.state.vfob_mode

    def set_active_vfo(self, vfo: int) -> None:
        """0 = VFO-A, 1 = VFO-B."""
        self.state.active_vfo = vfo & 1

    def tune(self) -> None: pass


# ─────────────────────────────────────────────────────────────────────────────
# Sin configurar (antes: simulador CAT)
# ─────────────────────────────────────────────────────────────────────────────
# El simulador se retiro: fingia una conexion "conectada" en verde con una
# frecuencia inventada aunque no hubiera radio real, lo cual confundia a
# usuarios que esperaban que "Conectar CAT" fallase si no hay nada configurado
# o la radio esta apagada. Este backend sustituye esos dos casos (compania sin
# elegir, o combinacion compania/modelo sin backend real) y SIEMPRE falla al
# conectar, con un mensaje claro en vez de fingir exito.

class CatUnconfigured(CatBackendBase):
    name = "Sin configurar"

    def __init__(self, motivo: str = ""):
        super().__init__()
        self._motivo = motivo

    def get_capabilities(self) -> dict: return CAP_SIMULATOR

    def connect(self) -> bool:
        self.state.connected = False
        self.state.last_error = self._motivo or t("cat.err.not_configured")
        return False

    def disconnect(self) -> None:
        self.state.connected = False

    def set_frequency(self, f) -> bool:
        return False

    def set_mode(self, mode) -> bool:
        return False

    def poll(self) -> bool:
        return False

    def get_smeter(self) -> int: return 0


# ─────────────────────────────────────────────────────────────────────────────
# CAT Directo Yaesu
# ─────────────────────────────────────────────────────────────────────────────

# S-meter de la FTDX10: lo que devuelve la radio (SM0, 0-255) no es lineal en dB. Tabla (valor, dB respecto a S9) con la forma de la FTDX101D
# (la medida por usuarios de Hamlib) y S9 en 152, ajustada con la lectura real de una FTDX10 (en la radio S9+20 cae hacia 183).
_SMETRO_FTDX10 = ((0, -54.0), (24, -48.0), (32, -42.0), (48, -36.0), (65, -30.0), (81, -24.0), (97, -18.0), (113, -12.0),
                  (129, -6.0), (152, 0.0), (255, 60.0))


# Potencia de salida de la FTDX10: valor crudo de RM5 (0-255) -> vatios. Tabla de FLRig (la que usa también Hamlib para esta radio), con el punto de 50 W ajustado a una FTDX10 real (la radio marcaba 50 W donde la tabla daba 52: 147 -> 149); no es lineal.
PO_FTDX10_TABLA = ((0, 0.0), (27, 5.0), (94, 25.0), (149, 50.0), (176, 75.0), (205, 100.0), (255, 143.0))


def po_ftdx10_w(raw: int) -> float:
    raw = max(0, min(255, int(raw)))
    for (r0, w0), (r1, w1) in zip(PO_FTDX10_TABLA, PO_FTDX10_TABLA[1:]):
        if raw <= r1:
            return w0 + (w1 - w0) * (raw - r0) / (r1 - r0)
    return PO_FTDX10_TABLA[-1][1]


# ROE (SWR) de la FTDX10: valor crudo de RM6 (0-255) -> relación de onda estacionaria. Tabla de Hamlib (medida con una FTDX101D por G3VPX).
SWR_FTDX10_TABLA = ((0, 1.0), (26, 1.2), (52, 1.5), (89, 2.0), (126, 3.0), (173, 4.0), (236, 5.0), (255, 25.0))


def swr_ftdx10(raw: int) -> float:
    raw = max(0, min(255, int(raw)))
    for (r0, v0), (r1, v1) in zip(SWR_FTDX10_TABLA, SWR_FTDX10_TABLA[1:]):
        if raw <= r1:
            return v0 + (v1 - v0) * (raw - r0) / (r1 - r0)
    return SWR_FTDX10_TABLA[-1][1]


def po_ftdx10_raw(vatios: float) -> float:
    """Lo contrario de po_ftdx10_w (para colocar la aguja del medidor)."""
    w = max(0.0, float(vatios))
    for (r0, w0), (r1, w1) in zip(PO_FTDX10_TABLA, PO_FTDX10_TABLA[1:]):
        if w <= w1:
            return r0 + (r1 - r0) * (w - w0) / (w1 - w0)
    return float(PO_FTDX10_TABLA[-1][0])


def smetro_ftdx10_db(raw: int) -> float:
    """Valor crudo de la FTDX10 (0-255) -> dB respecto a S9 (S0 = −54, S9 = 0, +60)."""
    raw = max(0, min(255, int(raw)))
    db = _SMETRO_FTDX10[-1][1]
    for (r0, d0), (r1, d1) in zip(_SMETRO_FTDX10, _SMETRO_FTDX10[1:]):
        if raw <= r1:
            db = d0 + (d1 - d0) * (raw - r0) / (r1 - r0)
            break
    return db


def smeter_a_db(v: float) -> float:
    """Escala de los medidores de RadioLog (0-30: 2 unidades por punto S hasta S9 = 18 y 5 dB por unidad por encima) -> dB respecto a S9."""
    v = max(0.0, min(30.0, float(v)))
    return (v - 18.0) * 3.0 if v <= 18.0 else (v - 18.0) * 5.0


def smetro_ftdx10(raw: int) -> int:
    """Valor crudo de la FTDX10 (0-255) -> escala de los medidores de RadioLog (0-30: 2 unidades por punto S hasta S9 = 18 y 5 dB por unidad por encima)."""
    db = smetro_ftdx10_db(raw)
    v = 18.0 + db / 5.0 if db >= 0 else (db + 54.0) / 3.0
    return max(0, min(30, int(round(v))))


class CatDirectYaesu(CatBackendBase):
    """CAT directo Yaesu via puerto serie. FTDX10, FT-991A, FTDX101, FT-710."""

    name = "Yaesu Direct"

    YAESU_A_MODO = {
        "1":"LSB","2":"USB","3":"CW","4":"FM","5":"AM","6":"RTTY",
        "7":"CW","8":"PSK31","9":"RTTY","A":"FM","B":"FM","C":"FT8",
        "D":"AM","E":"C4FM",
    }
    MODO_A_YAESU = {
        "LSB":"1","USB":"2","CW":"3","FM":"4","AM":"5","RTTY":"6",
        "CW-R":"7","DATA-L":"8","RTTY-R":"9","DATA-FM":"A","FM-N":"B",
        "DATA-U":"C","AM-N":"D","C4FM":"E",
        "FT8":"C","FT4":"C","JS8":"C","WSJTX":"C",
        "PSK31":"8","PSK63":"8","OLIVIA":"8","DSTAR":"E","SSB":"2",
    }
    _CAP_MAP = {
        "ftdx10": CAP_YAESU_FTDX10,
        "ft-991a": CAP_YAESU_FT991A, "ft991a": CAP_YAESU_FT991A,
        "ft-710":  CAP_YAESU_FT710,  "ft710":  CAP_YAESU_FT710,
    }

    def __init__(self, port="COM3", baudrate=38400, timeout=0.3, model="ftdx10",
                 flow_cts=False, flow_dtr=False, flow_rts=False):
        super().__init__()
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self.model = model.lower().strip()
        self.flow_cts = bool(flow_cts)
        self.flow_dtr = bool(flow_dtr)
        self.flow_rts = bool(flow_rts)
        self._serial = None
        # RLock (no Lock): _cmd_read/_cmd_set cierran el puerto en caso de
        # excepcion llamando a _cerrar_puerto(), que tambien toma este lock
        # -- con un Lock normal eso se autobloquearia. El lock evita que un
        # hilo de poll en background (MainWindow) y Rig Control (hilo GUI)
        # escriban/lean el mismo puerto serie a la vez.
        self._lock = threading.RLock()
        self.state.backend_name = f"Yaesu Direct ({port})"

    # Modelos en los que PR0 se ha comprobado con la radio real (valores 0 = apagado / 1 = encendido; otras Yaesu usan otros valores)
    _MODELOS_PROC = {"ftdx10"}
    _MODELOS_PO = {"ftdx10"}          # el medidor de potencia de salida (RM5) con calibración conocida

    def get_capabilities(self) -> dict:
        cap = dict(self._CAP_MAP.get(self.model, CAP_YAESU_FTDX10))
        cap["proc"] = self.model in self._MODELOS_PROC
        cap["po"] = self.model in self._MODELOS_PO
        cap["swr"] = self.model in self._MODELOS_PO
        cap["filtros_rx"] = True      # NB, DNR y notch se mandan de verdad a la radio (teclas RX de la ventana principal)
        return cap

    # ── Conexión ─────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        if serial is None:
            self.state.last_error = "pyserial no instalado. Ejecuta: pip install pyserial"
            return False
        try:
            from core.serial_util import abrir_puerto       # DTR/RTS sin pulsar al abrir
            self._serial = abrir_puerto(self.port, self.baudrate, self.timeout, rtscts=self.flow_cts,
                                        dtr=self.flow_dtr, rts=self.flow_rts, serial_mod=serial)
            # pyserial deja RTS/DTR en alto por defecto al abrir el puerto
            # (_rts_state/_dtr_state = True en serialutil.py) aunque no se
            # pidan aqui. En equipos donde el PTT esta cableado o configurado
            # para dispararse por RTS/DTR, eso deja el equipo en TX hasta
            # desconectar el CAT. Forzamos explicitamente a bajo salvo que el
            # usuario haya activado el flow control.
            self._serial.dtr = True if self.flow_dtr else False
            self._serial.rts = True if (self.flow_rts and not self.flow_cts) else False
            self._serial.reset_input_buffer()
            if self._cmd_read("FA") is None:
                self._cerrar_puerto()
                self.state.last_error = (
                    f"Radio no responde en {self.port} a {self.baudrate} baud.\n"
                    "Comprueba cable USB, radio encendida, CAT RATE y puerto COM."
                )
                return False
            self.state.connected = True
            self.state.last_error = ""
            self._leer_estado()
            self._leer_estado_rig()
            return True
        except Exception as e:
            self.state.last_error = str(e)
            self.state.connected = False
            self._cerrar_puerto()
            return False

    def disconnect(self) -> None:
        self.state.connected = False
        self._cerrar_puerto()

    def _cerrar_puerto(self) -> None:
        with self._lock:
            if self._serial:
                try: self._serial.close()
                except Exception: pass
                self._serial = None

    # ── Serie ────────────────────────────────────────────────────────────────

    def _enviar(self, cmd: str) -> None:
        if self._serial and self._serial.is_open:
            self._serial.write(cmd.encode("ascii"))

    def _leer_hasta_punto_coma(self) -> str:
        if not self._serial or not self._serial.is_open: return ""
        # Limite duro de tiempo total (ademas del timeout por byte de
        # pyserial) y de longitud. Sin esto, si llegaba algun byte suelto
        # justo antes de cada timeout individual, el bucle podia seguir
        # "vivo" indefinidamente -- el poll CAT corre en el hilo de la GUI
        # (ver CATMixin._poll_cat), asi que eso congelaba toda la
        # aplicacion sin lanzar ninguna excepcion (nada quedaba en el log).
        # Mismo patron que ya usan los backends CIV (_leer_trama, con
        # deadline) y generico (_leer_hasta, con max_bytes).
        deadline = time.monotonic() + self.timeout * 3
        buf = b""
        while time.monotonic() < deadline:
            ch = self._serial.read(1)
            if not ch: break
            buf += ch
            if ch == b";": break
            if len(buf) > 128:  # una respuesta Yaesu nunca es tan larga
                break
        return buf.decode("ascii", errors="ignore")

    def _cmd_read(self, cmd: str) -> str | None:
        with self._lock:
            try:
                self._serial.reset_input_buffer()
                self._enviar(f"{cmd};")
                resp = self._leer_hasta_punto_coma()
                return resp if resp and resp.startswith(cmd) else None
            except Exception as e:
                self.state.last_error = str(e)
                self.state.connected = False
                # Sin esto el handle del puerto se quedaba abierto tras un
                # fallo de E/S -- el siguiente intento de abrir el mismo
                # puerto podia fallar con "Acceso denegado" hasta reiniciar
                # RadioLog.
                self._cerrar_puerto()
                return None

    def _cmd_set(self, cmd: str) -> None:
        with self._lock:
            try: self._enviar(cmd)
            except Exception as e:
                self.state.last_error = str(e)
                self.state.connected = False
                self._cerrar_puerto()

    def _cmd_read_int(self, cmd: str, start: int, default: int = 0) -> int:
        resp = self._cmd_read(cmd)
        if resp and len(resp) > start:
            try: return int(resp[start:-1])
            except ValueError: pass
        return default

    def _sleep(self) -> None: time.sleep(0.025)

    # ── Lectura ──────────────────────────────────────────────────────────────

    # Modelos con arquitectura MAIN/SUB band (FTDX10, FTDX101, FT-710):
    # NO soportan los comandos antiguos FR/FT para leer el VFO activo --
    # segun el manual CAT oficial de Yaesu, estos modelos usan VS (VFO
    # Select) + ST (split). Solo la FT-991A conserva el juego antiguo
    # FR/FT (coherente con que ya usaba FT2/FT3 para split, distinto de
    # ST0/ST1 de los demas -- ver _SPLIT_CMD). Antes se mandaba siempre
    # FR/FT sin importar el modelo: en FTDX10/FT-710 la radio no
    # reconoce el comando, la lectura fallaba en silencio y el VFO
    # activo se quedaba clavado en VFO-A para siempre, sin importar el
    # VFO que se seleccionara en la propia radio.
    _MODELOS_VS = ("ftdx10", "ftdx101d", "ft710", "ft-710")

    def _leer_estado(self) -> bool:
        """Devuelve True si la radio respondio de verdad al comando FA
        (mismo comando que ya usa connect() para validar la conexion). Si
        el puerto sigue abierto pero la radio esta apagada/en standby
        (comun por USB: el chip se queda enumerado aunque el firmware de
        la radio no conteste a nada), _cmd_read() no lanza excepcion --
        solo devuelve None tras el timeout -- y antes eso se ignoraba en
        silencio, dejando poll() creer que el sondeo habia ido bien
        indefinidamente (indicador CAT clavado en verde con la radio
        apagada)."""
        respondio = False
        if self.model in self._MODELOS_VS:
            # VFO activo (RX): VS0 = VFO-A activo, VS1 = VFO-B activo
            resp = self._cmd_read("VS")
            if resp and len(resp) >= 3:
                try: self.state.active_vfo = int(resp[2:-1]) & 1
                except ValueError: pass
            self._sleep()

            # Split: ST0 = split OFF, ST1 = split ON
            resp = self._cmd_read("ST")
            if resp and len(resp) >= 3:
                try:
                    split_on = int(resp[2:-1]) != 0
                    self.state.split = split_on
                    self.state.tx_vfo = (1 - self.state.active_vfo) if split_on else self.state.active_vfo
                except ValueError: pass
            self._sleep()
        else:
            # RX VFO: FR0 = VFO-A, FR1 = VFO-B
            resp = self._cmd_read("FR")
            if resp and len(resp) >= 3:
                try: self.state.active_vfo = int(resp[2:-1]) & 1
                except ValueError: pass
            self._sleep()

            # TX VFO: FT0 = TX en VFO-A, FT1 = TX en VFO-B → detecta split
            resp = self._cmd_read("FT")
            if resp and len(resp) >= 3:
                try:
                    self.state.tx_vfo = int(resp[2:-1]) & 1
                    self.state.split = (self.state.tx_vfo != self.state.active_vfo)
                except ValueError: pass
            self._sleep()

        # Frecuencias VFO-A y VFO-B
        resp = self._cmd_read("FA")
        respondio = bool(resp)
        if resp and len(resp) >= 3:
            try: self.state.frequency_mhz = int(resp[2:-1]) / 1_000_000.0
            except ValueError: pass
        self._sleep()
        resp = self._cmd_read("FB")
        if resp and len(resp) >= 3:
            try: self.state.vfob_freq = int(resp[2:-1]) / 1_000_000.0
            except ValueError: pass
        self._sleep()

        # Modo del VFO RX activo (MD0 devuelve el modo del VFO en RX)
        resp = self._cmd_read("MD0")
        if resp and len(resp) >= 5:
            codigo_leido = resp[3:-1].upper()
            if self.state.active_vfo == 0:
                self.state.cw_inverso = (codigo_leido == "7")     # 7 = CW-L, 3 = CW-U
            modo_actual = self.state.mode if self.state.active_vfo == 0 else self.state.vfob_mode
            # Varios modos de la app (FT8/FT4/JS8/WSJTX) comparten el mismo
            # codigo de radio "C" (DATA-USB) -- la propia radio no puede
            # distinguirlos. Si el modo ya activo sigue correspondiendo al
            # mismo codigo que se acaba de leer, se deja como esta en vez de
            # forzarlo siempre a "FT8" (la primera coincidencia del
            # diccionario inverso), que es lo que pisaba "FT4" cada poll.
            if self.MODO_A_YAESU.get(modo_actual) == codigo_leido:
                modo = modo_actual
            else:
                modo = self.YAESU_A_MODO.get(codigo_leido, codigo_leido)
            if self.state.active_vfo == 0:
                self.state.mode = modo
            else:
                self.state.vfob_mode = modo

        return respondio

    def _leer_estado_rig(self) -> None:
        if not self.state.connected: return
        p = self._sleep
        p(); self.state.af_gain  = self._cmd_read_int("AG0",3,self.state.af_gain)
        p(); self.state.rf_gain  = self._cmd_read_int("RG0",3,self.state.rf_gain)
        p(); self.state.squelch  = self._cmd_read_int("SQ0",3,self.state.squelch)
        p(); self.state.mic_gain = self._cmd_read_int("MG", 2,self.state.mic_gain)
        p(); self.state.power_w  = self._cmd_read_int("PC", 2,self.state.power_w)
        p(); self.state.nb       = self._cmd_read_int("NB0",3,0) != 0
        p(); self.state.nr       = self._cmd_read_int("NR0",3,0) != 0
        p(); self._leer_notch()
        p(); self._leer_proc()
        p(); self.state.vox      = self._cmd_read_int("VX",2,0) != 0
        p(); self.state.att      = self._cmd_read_int("RA0",3,0)
        p(); self.state.ipa      = self._cmd_read_int("PA0",3,0)
        p(); self.state.agc      = self._cmd_read_int("GT0",3,4)

    # ── API ──────────────────────────────────────────────────────────────────

    def get_smeter(self) -> int:
        # SM0 en FTDX10: respuesta SM0XXX; (3 dígitos, escala 0-255)
        # Longitud mínima: 7 chars ("SM0" + 3 dígitos + ";")
        if not self._serial or not self._serial.is_open:
            return self.state.smeter
        self._sleep()
        resp = self._cmd_read("SM0")
        if resp and len(resp) >= 7:
            try:
                raw = int(resp[3:-1])              # 0-255
                val = smetro_ftdx10(raw) if self.model == "ftdx10" else min(30, raw * 30 // 255)     # a la escala de los medidores (0-30)
                self.state.smeter = val
                self._leer_po_de_paso()
                return val
            except ValueError:
                pass
        return self.state.smeter

    def _leer_po_de_paso(self) -> None:
        """La potencia de salida (RM5, solo lectura) se mira cada 2 lecturas del S-meter, y en cada una mientras se está transmitiendo."""
        if self.model not in self._MODELOS_PO:
            return
        self._po_n = getattr(self, "_po_n", 0) + 1
        if not (self.state.ptt or self.state.po_w > 0.5 or self._po_n % 2 == 0):
            return
        self._sleep()
        resp = self._cmd_read("RM5")
        if resp and len(resp) >= 7:
            try:
                self.state.po_w = po_ftdx10_w(int(resp[3:6]))
            except ValueError:
                pass
        if self.state.ptt or self.state.po_w > 0.5:               # la ROE solo tiene sentido transmitiendo
            self._sleep()
            resp = self._cmd_read("RM6")
            if resp and len(resp) >= 7:
                try:
                    self.state.swr = swr_ftdx10(int(resp[3:6]))
                except ValueError:
                    pass
        elif self.state.swr != 1.0:
            self.state.swr = 1.0

    def get_po(self) -> float:
        return float(self.state.po_w)

    def get_swr(self) -> float:
        return float(self.state.swr)

    def get_smeter_db(self) -> float:
        """S-meter en dB respecto a S9, con más finura que el medidor (para el barrido de banda). Solo se pregunta SM0."""
        if self.model != "ftdx10":
            return smeter_a_db(self.get_smeter())
        if not self._serial or not self._serial.is_open:
            return smeter_a_db(self.state.smeter)
        resp = self._cmd_read("SM0")
        if resp and len(resp) >= 7:
            try:
                return smetro_ftdx10_db(int(resp[3:-1]))
            except ValueError:
                pass
        return smeter_a_db(self.state.smeter)

    def get_frequency(self) -> float:  return self.state.frequency_mhz
    def get_mode(self) -> str:         return self.state.mode

    def set_frequency(self, f) -> bool:
        if not self.state.connected: return False
        try:
            hz = int(round(f * 1_000_000))
            self._cmd_set(f"FA{hz:09d};")
            self.state.frequency_mhz = f; return True
        except Exception as e:
            self.state.last_error = str(e); return False

    def set_vfob_frequency(self, f) -> bool:
        if not self.state.connected: return False
        try:
            hz = int(round(f * 1_000_000))
            self._cmd_set(f"FB{hz:09d};")
            self.state.vfob_freq = f; return True
        except Exception as e:
            self.state.last_error = str(e); return False

    def set_mode(self, mode) -> bool:
        if not self.state.connected: return False
        code = self.MODO_A_YAESU.get(mode.strip().upper())
        if code is None:
            self.state.last_error = f"Modo no soportado: {mode}"; return False
        self._cmd_set(f"MD0{code};"); self.state.mode = mode; return True

    def leer_af_gain(self) -> int:
        self.state.af_gain = self._cmd_read_int("AG0", 3, self.state.af_gain)
        return self.state.af_gain

    def set_af_gain(self, v) -> None:
        v = max(0,min(255,int(v))); self.state.af_gain  = v; self._cmd_set(f"AG0{v:03d};")
    def set_rf_gain(self, v) -> None:
        v = max(0,min(255,int(v))); self.state.rf_gain  = v; self._cmd_set(f"RG0{v:03d};")

    def set_squelch(self, v) -> None:
        v = max(0,min(255,int(v))); self.state.squelch  = v; self._cmd_set(f"SQ0{v:03d};")
    def set_mic_gain(self, v) -> None:
        v = max(0,min(100,int(v))); self.state.mic_gain = v; self._cmd_set(f"MG{v:03d};")
    def set_power(self, v) -> None:
        v = max(5,min(100,int(v))); self.state.power_w  = v; self._cmd_set(f"PC{v:03d};")
    def set_nb(self, on) -> None:
        self.state.nb = bool(on); self._cmd_set(f"NB0{'1' if on else '0'};")
    def set_nr(self, on) -> None:
        self.state.nr = bool(on); self._cmd_set(f"NR0{'1' if on else '0'};")
    def set_notch(self, on) -> None:
        self.state.notch = bool(on); self._cmd_set(f"BP00{'001' if on else '000'};")
    def set_proc(self, on) -> None:
        if self.model not in self._MODELOS_PROC:
            return
        self.state.proc = bool(on); self._cmd_set(f"PR0{'1' if on else '0'};")
    def _leer_proc(self) -> None:
        if self.model not in self._MODELOS_PROC:
            return
        resp = self._cmd_read("PR0")
        if resp and len(resp) > 3:
            try:
                self.state.proc = int(resp[3:-1]) != 0
            except ValueError:
                pass
    def _leer_notch(self) -> None:
        """Notch manual (BP00 -> «BP00xxx;», 000 apagado / 001 encendido). Si la radio no contesta, la tecla NOTCH queda desactivada en vez de fingir."""
        resp = self._cmd_read("BP00")
        try:
            valor = int(resp[4:-1]) if resp and len(resp) > 4 else None
        except ValueError:
            valor = None
        self.state.notch_ok = valor is not None
        if valor is not None:
            self.state.notch = valor != 0
    def leer_filtros_rx(self) -> None:
        """Relee NB, DNR y notch de la radio (por si se tocaron en el panel de la propia radio). Si una lectura falla se deja el valor que había."""
        if not self.state.connected:
            return
        p = self._sleep
        for campo, cmd in (("nb", "NB0"), ("nr", "NR0")):
            p()
            resp = self._cmd_read(cmd)
            try:
                if resp and len(resp) > 3:
                    setattr(self.state, campo, int(resp[3:-1]) != 0)
            except ValueError:
                pass
        p()
        self._leer_notch()
        p()
        resp = self._cmd_read("VX")
        try:
            if resp and len(resp) > 2:
                self.state.vox = int(resp[2:-1]) != 0
        except ValueError:
            pass
        p()
        self._leer_proc()
    def set_vox(self, on) -> None:
        self.state.vox = bool(on); self._cmd_set(f"VX{'1' if on else '0'};")
    def set_att(self, v) -> None:
        v = max(0,min(3,int(v))); self.state.att = v; self._cmd_set(f"RA0{v};")
    def set_ipa(self, v) -> None:
        v = max(0,min(2,int(v))); self.state.ipa = v; self._cmd_set(f"PA0{v};")
    def set_agc(self, v) -> None:
        v = max(0,min(4,int(v))); self.state.agc = v; self._cmd_set(f"GT0{v:02d};")
    def set_ptt(self, on) -> None:
        self.state.ptt = bool(on); self._cmd_set("TX1;" if on else "TX0;")

    def get_split(self) -> bool: return self.state.split
    # Comandos split por modelo (segun definiciones .ini de cada radio)
    _SPLIT_CMD = {
        "ftdx10":   ("ST1;", "ST0;"),
        "ftdx101d": ("ST1;", "ST0;"),
        "ft710":    ("ST1;", "ST0;"),
        "ft-710":   ("ST1;", "ST0;"),
        "ft991a":   ("FT3;", "FT2;"),
        "ft-991a":  ("FT3;", "FT2;"),
    }

    def set_split(self, on: bool) -> None:
        on = bool(on)
        # Obtener comandos correctos para este modelo
        cmd_on, cmd_off = self._SPLIT_CMD.get(
            self.model, (f"FT{1 - self.state.active_vfo};", f"FT{self.state.active_vfo};")
        )
        self._cmd_set(cmd_on if on else cmd_off)
        self.state.tx_vfo = (1 - self.state.active_vfo) if on else self.state.active_vfo
        self.state.split = on

    def vfo_swap(self) -> None:
        """Intercambia frecuencia Y modo de VFO-A/VFO-B leyendo y
        reescribiendo FA/FB/MD0/MD1 directamente -- NO envia "SV;" (el
        comando "SWAP VFO" documentado en el manual CAT del FTDX10).
        Motivo (bug real, encontrado 2026-09-06 -- el usuario reportó
        "se cambia pero al segundo vuelve como estaba"): probado en
        vivo contra un FTDX10 real conectado por USB, "SV;" NO TIENE
        NINGÚN EFECTO en este radio/firmware -- FA/FB se quedan
        exactamente igual varios segundos después de enviarlo, pese a
        estar documentado como soportado ("O X X X" en la tabla de
        comandos). Lo que el usuario veía era: el estado LOCAL de
        RadioLog se intercambiaba en Python (efecto visual inmediato),
        pero la radio real nunca cambiaba -- el siguiente poll (1s
        después) releía los valores reales (sin cambiar) y "revertía"
        el intercambio en pantalla.

        Este método SÍ funciona y persiste, verificado en el mismo
        radio real (FA/FB comprobados varios segundos después, sin
        revertir). El código de modo se copia tal cual entre VFOs, sin
        decodificar su significado -- MD0/MD1 devuelven "MD0X;"/"MD1X;"
        (X = un carácter de código de modo propio de Yaesu)."""
        freq_a = self._cmd_read("FA")
        freq_b = self._cmd_read("FB")
        modo_a = self._cmd_read("MD0")
        modo_b = self._cmd_read("MD1")
        if not (freq_a and freq_b and len(freq_a) >= 11 and len(freq_b) >= 11):
            return
        hz_a = freq_a[2:-1]
        hz_b = freq_b[2:-1]
        self._cmd_set(f"FA{hz_b};")
        self._sleep()
        self._cmd_set(f"FB{hz_a};")
        self._sleep()
        if modo_a and modo_b and len(modo_a) >= 4 and len(modo_b) >= 4:
            cod_a = modo_a[3]
            cod_b = modo_b[3]
            self._cmd_set(f"MD0{cod_b};")
            self._sleep()
            self._cmd_set(f"MD1{cod_a};")
            self._sleep()
        self.state.frequency_mhz, self.state.vfob_freq = \
            self.state.vfob_freq, self.state.frequency_mhz
        self.state.mode, self.state.vfob_mode = \
            self.state.vfob_mode, self.state.mode

    def vfo_a_to_b(self) -> None:
        # OJO: "AB;" no se ha verificado contra hardware real de la
        # misma forma que vfo_swap() -- comparte el mismo riesgo ("SV;"
        # tampoco funcionaba pese a estar documentado). Sin reportes del
        # usuario sobre este botón concreto, no se ha tocado -- si se
        # confirma el mismo problema, el arreglo sería análogo (leer
        # FA/MD0 y escribir FB/MD1 directamente en vez de fiarse de "AB;").
        self._cmd_set("AB;")
        self.state.vfob_freq = self.state.frequency_mhz
        self.state.vfob_mode = self.state.mode

    def vfo_b_to_a(self) -> None:
        self._cmd_set("BA;")
        self.state.frequency_mhz = self.state.vfob_freq
        self.state.mode = self.state.vfob_mode

    def set_active_vfo(self, vfo: int) -> None:
        """Alterna cuál de las dos VFO está activa (0=A, 1=B) -- igual
        que el botón físico "A/B" del propio equipo: NO mueve ninguna
        frecuencia entre A y B (a diferencia de vfo_swap()), solo
        cambia cuál de las dos se está usando ahora. Corrección
        2026-09-06: el usuario pidió que el botón "⇄" de V4 hiciera
        esto en vez de intercambiar contenidos (lo que hacía antes,
        confundiéndose con el botón físico del radio, que es esta
        función). La clase base (CatBackendBase.set_active_vfo) solo
        cambiaba el estado cacheado en Python, sin avisar a la radio --
        aquí sí se envía el comando real.

        Comando "VS" (VFO SELECT), documentado como soportado en
        Set/Read/Ans/AI y CONFIRMADO en vivo contra un FTDX10 real
        conectado (2026-09-06): "VS1;" cambia de verdad la VFO activa
        y se mantiene sin revertir -- a diferencia de "SV;" (swap), que
        resultó no tener ningún efecto pese a estar documentado. FTDX10/
        FTDX101D/FT-710 usan "VS"; el resto de modelos de esta clase
        usan "FR" (mismo comando que ya lee _leer_estado() para el VFO
        activo -- ver _MODELOS_VS)."""
        vfo = vfo & 1
        if self.model in self._MODELOS_VS:
            self._cmd_set(f"VS{vfo};")
        else:
            self._cmd_set(f"FR{vfo};")
        self.state.active_vfo = vfo

    def tune(self) -> None:
        self.state.tuning = True
        self._cmd_set("AC002;")

    def leer_tuner(self) -> None:
        """¿Sigue sintonizando? AC -> «AC00x;»: x = 0 apagado, 1 encendido, 2 sintonizando. Si la radio no contesta se da por terminado."""
        resp = self._cmd_read("AC")
        self.state.tuning = bool(resp and len(resp) >= 6 and resp[-2] == "2")

    def poll(self) -> bool:
        if not self.state.connected or not self._serial or not self._serial.is_open:
            self.state.connected = False; return False
        try:
            if not self._leer_estado():
                # Puerto abierto pero la radio no ha respondido a nada --
                # apagada o en standby por USB. Sin esto poll() devolvia
                # True igualmente y el indicador CAT se quedaba en verde
                # indefinidamente con la radio apagada.
                # Un fallo suelto no desconecta: hacen falta FALLOS_SONDEO_MAX seguidos.
                if not self._sondeo_perdido(f"en {self.port} a {self.baudrate} baudios"):
                    return True
                self.state.last_error = "Radio no responde (¿apagada?)"
                self.state.connected = False
                self._cerrar_puerto()
                return False
            self._sondeo_ok()
            return True
        except Exception as e:
            self.state.last_error = str(e); self.state.connected = False
            self._cerrar_puerto()
            return False

    def poll_rig_control(self) -> bool:
        if not self.state.connected: return False
        try:
            if not self._leer_estado():
                if not self._sondeo_perdido(f"en {self.port} a {self.baudrate} baudios (Rig Remote Control)"):
                    return True
                self.state.last_error = "Radio no responde (¿apagada?)"
                self.state.connected = False
                self._cerrar_puerto()
                return False
            self._sondeo_ok()
            self._sleep(); self.get_smeter(); return True
        except Exception as e:
            self.state.last_error = str(e); self.state.connected = False
            self._cerrar_puerto()
            return False


# -----------------------------------------------------------------------
# CatEngine — fachada publica
# -----------------------------------------------------------------------

class CatEngine:
    # Intervalo minimo real entre sondeos por puerto serie. Varias
    # ventanas (Principal, Activacion, CB) comparten el mismo CatEngine,
    # cada una con su propio QTimer de 1s llamando a poll() de forma
    # independiente. Sin este throttle, dos ventanas abiertas a la vez
    # con CAT conectado pueden escribir/leer casi simultaneamente en el
    # mismo puerto fisico, cruzando comandos y respuestas -- eso produce
    # timeouts/reintentos que se notan como congelados periodicos de la
    # interfaz (confirmado: pasa con CAT conectado y Principal +
    # Activacion abiertas a la vez). Con el throttle, solo la primera
    # llamada en esa ventana de tiempo toca de verdad el puerto; las
    # demas devuelven el estado ya leido.
    _POLL_MIN_INTERVALO = 0.8

    def __init__(self, backend: str = "", **kwargs):
        self.backend_key = backend
        self.backend = self._create_backend(backend, **kwargs)
        self._ultimo_poll_ts = 0.0
        self._barrido_hilo = None
        self._barrido_cancelar_ev = threading.Event()
        self._barrido_res = None

    def _create_backend(self, backend: str, **kwargs) -> CatBackendBase:
        b = (backend or "").lower().strip()
        if b in ("directo", "direct", "direct_yaesu", "yaesu"):
            return CatDirectYaesu(
                port     = kwargs.get("port",    "COM3"),
                baudrate = int(kwargs.get("baudrate", 38400)),
                model    = kwargs.get("model",   "ftdx10"),
                flow_cts = bool(kwargs.get("flow_cts", False)),
                flow_dtr = bool(kwargs.get("flow_dtr", False)),
                flow_rts = bool(kwargs.get("flow_rts", False)),
            )
        if b == "generic":
            from core.cat_backend_generic import CatBackendGeneric
            preset = kwargs.get("preset")
            if preset and preset.get("compatible"):
                return CatBackendGeneric(
                    preset   = preset,
                    port     = kwargs.get("port",    "COM3"),
                    baudrate = int(kwargs.get("baudrate", 38400)),
                    flow_cts = bool(kwargs.get("flow_cts", False)),
                    flow_dtr = bool(kwargs.get("flow_dtr", False)),
                    flow_rts = bool(kwargs.get("flow_rts", False)),
                    baud_alternativos = tuple(kwargs.get("baud_alternativos", ()) or ()),
                )
        if b == "civ":
            from core.cat_backend_civ import CatBackendCIV
            preset = kwargs.get("preset")
            if preset:
                return CatBackendCIV(
                    preset   = preset,
                    port     = kwargs.get("port",     "COM3"),
                    baudrate = int(kwargs.get("baudrate", 9600)),
                    flow_cts = bool(kwargs.get("flow_cts", False)),
                    flow_dtr = bool(kwargs.get("flow_dtr", False)),
                    flow_rts = bool(kwargs.get("flow_rts", False)),
                )
        if b == "hamlib":
            from core.cat_backend_hamlib import CatBackendHamlib
            return CatBackendHamlib(
                host    = kwargs.get("host",    "localhost"),
                port    = int(kwargs.get("port", 4532)),
                timeout = float(kwargs.get("timeout", 3.0)),
            )
        if b == "remoto":
            from core.remoto.cliente import CatBackendRemoto
            return CatBackendRemoto(
                host    = kwargs.get("host", "127.0.0.1"),
                port    = int(kwargs.get("port", 8765)),
                clave   = kwargs.get("clave", ""),
                cifrado = bool(kwargs.get("cifrado", True)),
            )
        if b == "flex":
            from core.cat_backend_flex import CatBackendFlex
            return CatBackendFlex(
                port    = int(kwargs.get("port", 5002)),
                timeout = float(kwargs.get("timeout", 2.0)),
            )
        return CatUnconfigured(motivo=kwargs.get("motivo", ""))

    def get_capabilities(self) -> dict:      return self.backend.get_capabilities()
    def connect(self) -> bool:               return self.backend.connect()
    def disconnect(self) -> None:            self.backend.disconnect()
    def is_connected(self) -> bool:          return self.backend.is_connected()
    def get_frequency(self) -> float:        return self.backend.get_frequency()
    def set_frequency(self, f) -> bool:      return self.backend.set_frequency(f)
    def get_mode(self) -> str:               return self.backend.get_mode()
    def get_mode_cw(self) -> str:
        f = getattr(self.backend, "get_mode_cw", None)
        return f() if f is not None else self.backend.get_mode()
    def set_mode(self, mode) -> bool:        return self.backend.set_mode(mode)

    @property
    def barriendo(self) -> bool:
        return bool(getattr(getattr(self.backend, "state", None), "barriendo", False))

    def get_smeter_db(self) -> float:
        f = getattr(self.backend, "get_smeter_db", None)
        return float(f()) if f is not None else smeter_a_db(self.backend.get_smeter())

    # ── barrido de banda con el receptor (panorama / cascada de RF; ver core/barrido_rf.py) ─────────────────────────────────
    def barrido_iniciar(self, centro_mhz: float, span_khz: float, paso_khz: float) -> bool:
        """Empieza un barrido sin esperar (radio local: en un hilo; radio remota: se lo pide al servidor). El resultado se recoge con barrido_resultado()."""
        f = getattr(self.backend, "barrido_iniciar", None)
        if f is not None:
            return bool(f(centro_mhz, span_khz, paso_khz))
        from core import barrido_rf
        if self.barriendo or (self._barrido_hilo is not None and self._barrido_hilo.is_alive()):
            return False
        if barrido_rf.comprobar_parametros(centro_mhz, span_khz, paso_khz):
            return False
        self._barrido_cancelar_ev = threading.Event()
        self._barrido_res = None

        def _hacer():
            self._barrido_res = barrido_rf.barrer(self, centro_mhz, span_khz, paso_khz, cancelar=self._barrido_cancelar_ev)

        self._barrido_hilo = threading.Thread(target=_hacer, name="barrido-rf", daemon=True)
        self._barrido_hilo.start()
        return True

    def barrido_cancelar(self) -> None:
        f = getattr(self.backend, "barrido_cancelar", None)
        if f is not None:
            f()
        else:
            self._barrido_cancelar_ev.set()

    def barrido_resultado(self):
        """El último barrido terminado (diccionario de core/barrido_rf.barrer) o None; se entrega una sola vez."""
        f = getattr(self.backend, "barrido_resultado", None)
        if f is not None:
            return f()
        r, self._barrido_res = self._barrido_res, None
        return r

    def poll(self) -> bool:
        if self.barriendo:                                    # barriendo: la radio está ocupada y el VFO se mueve a propósito
            return self.backend.state.connected
        ahora = time.monotonic()
        if (ahora - self._ultimo_poll_ts) < self._POLL_MIN_INTERVALO:
            # Sondeo reciente (de otra ventana) -- no volver a tocar el
            # puerto serie, devolver el estado ya conocido.
            return self.backend.state.connected
        ok = self.backend.poll()
        self._ultimo_poll_ts = time.monotonic()
        return ok
    def poll_rig_control(self) -> bool:
        if self.barriendo:
            return self.backend.state.connected
        if hasattr(self.backend, "poll_rig_control"):
            return self.backend.poll_rig_control()
        return self.backend.poll()
    def refresh_rig_params(self) -> None:
        if hasattr(self.backend, "_leer_estado_rig"):
            self.backend._leer_estado_rig()

    def get_smeter(self) -> int:             return self.backend.get_smeter()
    def get_po(self) -> float:
        f = getattr(self.backend, "get_po", None)
        return float(f()) if f is not None else 0.0

    def get_swr(self) -> float:
        f = getattr(self.backend, "get_swr", None)
        return float(f()) if f is not None else 1.0
    def get_af_gain(self) -> int:            return self.backend.get_af_gain()
    def leer_af_gain(self) -> int:
        """Pregunta a la radio su ganancia de audio actual (si el motor sabe) y la deja en el estado."""
        f = getattr(self.backend, "leer_af_gain", None)
        return int(f()) if f is not None else int(self.backend.get_af_gain())
    def set_af_gain(self, v) -> None:        self.backend.set_af_gain(v)
    def get_rf_gain(self) -> int:            return self.backend.get_rf_gain()
    def set_rf_gain(self, v) -> None:        self.backend.set_rf_gain(v)
    def get_squelch(self) -> int:            return self.backend.get_squelch()
    def set_squelch(self, v) -> None:        self.backend.set_squelch(v)
    def get_mic_gain(self) -> int:           return self.backend.get_mic_gain()
    def set_mic_gain(self, v) -> None:       self.backend.set_mic_gain(v)
    def get_power(self) -> int:              return self.backend.get_power()
    def set_power(self, v) -> None:          self.backend.set_power(v)
    def get_nb(self) -> bool:                return self.backend.get_nb()
    def set_nb(self, on) -> None:            self.backend.set_nb(on)
    def get_nr(self) -> bool:                return self.backend.get_nr()
    def set_nr(self, on) -> None:            self.backend.set_nr(on)
    def get_notch(self) -> bool:             return self.backend.get_notch()
    def set_notch(self, on) -> None:         self.backend.set_notch(on)
    def leer_filtros_rx(self) -> None:       self.backend.leer_filtros_rx()
    def get_proc(self) -> bool:              return self.backend.get_proc()
    def set_proc(self, on) -> None:          self.backend.set_proc(on)
    def get_tuning(self) -> bool:            return self.backend.get_tuning()
    def leer_tuner(self) -> None:            self.backend.leer_tuner()
    def audio_disponible(self) -> bool:      return bool(getattr(self.backend, "audio_disponible", lambda: False)())
    def audio_rx(self, on) -> bool:          return bool(getattr(self.backend, "audio_rx", lambda on: False)(on))
    def audio_tx_disponible(self) -> bool:   return bool(getattr(self.backend, "audio_tx_disponible", lambda: False)())
    def audio_tx(self, on) -> bool:          return bool(getattr(self.backend, "audio_tx", lambda on: False)(on))
    def enviar_audio_tx(self, pcm) -> bool:  return bool(getattr(self.backend, "enviar_audio_tx", lambda pcm: False)(pcm))
    def poner_receptor_audio(self, funcion) -> None:
        if hasattr(self.backend, "on_audio"):
            self.backend.on_audio = funcion
    def notch_disponible(self) -> bool:      return bool(getattr(getattr(self.backend, "state", None), "notch_ok", True))
    def get_vox(self) -> bool:                return self.backend.get_vox()
    def set_vox(self, on) -> None:            self.backend.set_vox(on)
    def get_att(self) -> int:                return self.backend.get_att()
    def set_att(self, v) -> None:            self.backend.set_att(v)
    def get_ipa(self) -> int:                return self.backend.get_ipa()
    def set_ipa(self, v) -> None:            self.backend.set_ipa(v)
    def get_agc(self) -> int:                return self.backend.get_agc()
    def set_agc(self, v) -> None:            self.backend.set_agc(v)
    def set_ptt(self, on) -> None:           self.backend.set_ptt(on)
    def get_ptt(self) -> bool:
        """Estado real de PTT del backend activo -- nunca lanza (si el
        backend no expone .state, o no tiene .ptt, se asume False).
        Añadido para corregir core/cat_relay.py::get_ptt (forma larga),
        que accedia directamente a cat.state.ptt sin pasar por el
        envoltorio CatEngine, y fallaba con AttributeError."""
        backend = getattr(self, "backend", None)
        state = getattr(backend, "state", None)
        return bool(getattr(state, "ptt", False))
    def get_vfob_frequency(self) -> float:   return self.backend.get_vfob_frequency()
    def set_vfob_frequency(self, f) -> bool: return self.backend.set_vfob_frequency(f)
    def get_vfob_mode(self) -> str:          return self.backend.get_vfob_mode()
    def get_active_vfo(self) -> int:         return self.backend.get_active_vfo()
    def set_active_vfo(self, vfo: int) -> None: self.backend.set_active_vfo(vfo)
    def get_split(self) -> bool:             return self.backend.get_split()
    def set_split(self, on: bool) -> None:   self.backend.set_split(on)
    def vfo_swap(self) -> None:              self.backend.vfo_swap()
    def vfo_a_to_b(self) -> None:            self.backend.vfo_a_to_b()
    def vfo_b_to_a(self) -> None:            self.backend.vfo_b_to_a()
    def tune(self) -> None:                  self.backend.tune()

    def get_status_text(self) -> str:
        s = self.backend.state
        status = "Conectado" if s.connected else "Desconectado"
        return f"{s.backend_name}: {status} · {s.frequency_mhz:.6f} MHz · {s.mode}"

    def last_error(self) -> str:
        return self.backend.state.last_error
