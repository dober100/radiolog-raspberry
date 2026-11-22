"""
core/cw_keyer.py -- RadioLog
Keyer de CW por puerto serie (línea DTR o RTS), con:

  - envío de texto/macros en morse a la velocidad (WPM) elegida, con la
    temporización estándar PARIS (punto = 1200/WPM ms, raya = 3 puntos,
    espacio entre elementos 1, entre letras 3, entre palabras 7),
  - PTT opcional (callback) antes y después de transmitir,
  - sidetone opcional (core/cw_sidetone.py),
  - entrada de palas por las líneas CTS/DSR/RI/CD del mismo puerto, con
    lógica iambic A/B, bug y clave recta,
  - interrupción inmediata (Esc, o tocar una pala mientras se envía texto).

Todo el hardware pasa por un "backend" (set_key / read_paddles), así que la
lógica se prueba sin puertos reales (ver tests/test_cw_keyer.py).

Seguridad: la línea de manipulación SIEMPRE se suelta al parar, al cerrar o si
algo falla (bloque finally), para no dejar la emisora transmitiendo.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from typing import Callable, Optional

logger = logging.getLogger("radiolog.cw_keyer")

# ── Alfabeto morse ───────────────────────────────────────────────────────────

MORSE: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    ".": ".-.-.-", ",": "--..--", "?": "..--..", "/": "-..-.", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "(": "-.--.", ")": "-.--.-", ":": "---...",
    ";": "-.-.-.", "'": ".----.", '"': ".-..-.", "@": ".--.-.", "!": "-.-.--",
    "&": ".-...",
}

# Prosignos: se escriben entre ángulos, p. ej. "<AR>" o "<SK>".
PROSIGNOS: dict[str, str] = {
    "AR": ".-.-.", "SK": "...-.-", "BT": "-...-", "KN": "-.--.", "AS": ".-...",
    "BK": "-...-.-", "CL": "-.-..-..", "SOS": "...---...", "HH": "........",
    "VA": "...-.-",
}

_RE_TOKEN = re.compile(r"<([A-Za-z]{2,3})>|(\s+)|(.)", re.S)
_RE_RST = re.compile(r"\b([1-5][1-9][1-9])\b")

WPM_MIN, WPM_MAX = 5, 60


def unidad_ms(wpm: float) -> float:
    """Duración del punto en ms (palabra PARIS = 50 unidades)."""
    return 1200.0 / max(1.0, float(wpm))


def abreviar_rst(texto: str) -> str:
    """599 -> 5NN, 579 -> 57N (cut numbers) solo en informes de 3 cifras."""
    return _RE_RST.sub(
        lambda m: m.group(1)[0] + m.group(1)[1:].replace("9", "N"), texto)


def tokenizar(texto: str) -> list[tuple[str, Optional[str]]]:
    """Texto -> lista de (etiqueta, patrón). Patrón None = espacio entre
    palabras. Caracteres sin código se ignoran."""
    out: list[tuple[str, Optional[str]]] = []
    for m in _RE_TOKEN.finditer(str(texto or "")):
        pros, esp, car = m.groups()
        if pros:
            patron = PROSIGNOS.get(pros.upper())
            if patron:
                out.append((f"<{pros.upper()}>", patron))
        elif esp:
            if out and out[-1][1] is not None:
                out.append((" ", None))
        elif car:
            patron = MORSE.get(car.upper())
            if patron:
                out.append((car.upper(), patron))
    while out and out[-1][1] is None:
        out.pop()
    return out


def plan_de_patron(patron: Optional[str], u_ms: float, peso: float = 3.0
                   ) -> list[tuple[bool, float]]:
    """Patrón de una letra -> [(tecla_pulsada, duración_ms)], incluyendo el
    hueco de 3 unidades que la separa de la siguiente letra. Patrón None
    (espacio) añade las 4 unidades que faltan hasta las 7 de palabra."""
    if patron is None:
        return [(False, 4 * u_ms)]
    plan: list[tuple[bool, float]] = []
    for simbolo in patron:
        plan.append((True, (peso if simbolo == "-" else 1.0) * u_ms))
        plan.append((False, 1.0 * u_ms))
    plan.append((False, 2.0 * u_ms))  # 1 + 2 = 3 unidades entre letras
    return plan


# ── Backends ─────────────────────────────────────────────────────────────────

class ClaveSerie:
    """Manipulador por puerto serie: DTR o RTS = clave; CTS/DSR/RI/CD = palas.

    El puerto es el de la SALIDA de la radio, compartido con el PTT y con los demás Studios (core/puerto_radio.py): la
    clave mueve SOLO su línea, así el PTT (otra línea del mismo puerto) puede estar activo a la vez sin pelearse."""

    def __init__(self, puerto: str, linea: str = "dtr", dit: str = "cts",
                 dah: str = "dsr", invertir: bool = False, puerto_radio=None):
        from core.puerto_radio import compartido
        self.puerto = puerto
        self.linea = "rts" if str(linea).lower() == "rts" else "dtr"
        self.lin_dit = dit if dit in ("cts", "dsr", "ri", "cd") else "cts"
        self.lin_dah = dah if dah in ("cts", "dsr", "ri", "cd") else "dsr"
        self.invertir = bool(invertir)
        self._pr = puerto_radio or compartido()
        self._ser = None

    def open(self) -> None:
        # Ambas líneas en su estado de reposo ANTES de abrir: así el puerto no pulsa la clave al abrirse (si otro módulo
        # ya lo tenía abierto, solo se suma un usuario y se respeta cómo estaba).
        self._pr.adquirir(self.puerto, reposo={"dtr": self.invertir, "rts": self.invertir})
        self._ser = self._pr.ser
        self.set_key(False)

    def close(self) -> None:
        try:
            self.set_key(False)
        finally:
            if self._ser is not None:
                self._ser = None
                self._pr.liberar()

    def set_key(self, pulsada: bool) -> None:
        if self._ser is None:
            return
        self._pr.poner(self.linea, bool(pulsada) ^ self.invertir)

    def read_paddles(self) -> tuple[bool, bool]:
        if self._ser is None:
            return (False, False)
        try:
            return (bool(getattr(self._ser, self.lin_dit)),
                    bool(getattr(self._ser, self.lin_dah)))
        except Exception:
            return (False, False)


class ClaveSimulada:
    """Backend sin hardware: registra los cambios de clave (para pruebas y
    para probar la ventana sin radio)."""

    def __init__(self, paddles: Optional[Callable[[], tuple[bool, bool]]] = None,
                 reloj: Callable[[], float] = time.perf_counter):
        self.eventos: list[tuple[float, bool]] = []
        self._paddles = paddles
        self._reloj = reloj
        self.abierta = False

    def open(self) -> None:
        self.abierta = True

    def close(self) -> None:
        self.abierta = False

    def set_key(self, pulsada: bool) -> None:
        self.eventos.append((self._reloj(), bool(pulsada)))

    def read_paddles(self) -> tuple[bool, bool]:
        return self._paddles() if self._paddles else (False, False)


# ── Lógica de palas (pura, sin hilos ni hardware) ───────────────────────────

class Iambic:
    """Máquina de estados de las palas. update(t, dit, dah) devuelve si la
    clave debe estar pulsada en el instante t (segundos).

    Modos:
      iambic_a : alterna mientras se aprietan las dos palas; al soltarlas
                 termina el elemento en curso y para.
      iambic_b : igual, pero al soltar tras apretar las dos añade UN elemento
                 más (el contrario) y recuerda la pala contraria tocada durante
                 un elemento.
      bug      : los puntos se repiten solos; la raya es manual (dura lo que
                 se mantiene la pala).
      straight : clave recta; cualquiera de las dos palas pulsa la clave.
    """

    def __init__(self, unidad_s: Callable[[], float], modo: str = "iambic_a"):
        self._unidad = unidad_s
        self.modo = modo if modo in ("iambic_a", "iambic_b", "bug", "straight") else "iambic_a"
        self.reset()

    def reset(self) -> None:
        self._fase = "reposo"        # reposo | elemento | hueco
        self._fin = 0.0
        self._actual: Optional[str] = None   # 'dit' | 'dah'
        self._ultimo: Optional[str] = None
        self._mem_dit = False
        self._mem_dah = False
        self._apretadas = False       # hubo squeeze durante el elemento
        self.clave = False

    def update(self, t: float, dit: bool, dah: bool) -> bool:
        if self.modo == "straight":
            self.clave = bool(dit or dah)
            return self.clave
        if self.modo == "bug":
            return self._update_bug(t, dit, dah)
        return self._update_iambic(t, dit, dah)

    # -- iambic ---------------------------------------------------------------
    def _empezar(self, t: float, elemento: str) -> None:
        u = self._unidad()
        self._actual = elemento
        self._fase = "elemento"
        self._fin = t + (3.0 * u if elemento == "dah" else u)
        self._mem_dit = self._mem_dah = False
        self._apretadas = False
        self.clave = True

    def _decidir(self, t: float, dit: bool, dah: bool) -> None:
        b = self.modo == "iambic_b"
        req_dit = dit or (b and self._mem_dit)
        req_dah = dah or (b and self._mem_dah)
        opuesto = "dit" if self._ultimo == "dah" else "dah"
        if req_dit and req_dah:
            self._empezar(t, opuesto if self._ultimo else "dit")
        elif req_dit:
            self._empezar(t, "dit")
        elif req_dah:
            self._empezar(t, "dah")
        elif b and self._apretadas:
            self._empezar(t, opuesto)       # elemento extra al soltar el squeeze
        else:
            self._fase = "reposo"
            self._ultimo = None          # la próxima secuencia empieza por punto
            self._actual = None
            self._apretadas = False
            self.clave = False

    def _update_iambic(self, t: float, dit: bool, dah: bool) -> bool:
        if self._fase == "reposo":
            if dit or dah:
                self._decidir(t, dit, dah)
            return self.clave
        # memoria de la pala contraria y detección de squeeze
        if self._actual == "dah" and dit:
            self._mem_dit = True
        if self._actual == "dit" and dah:
            self._mem_dah = True
        if dit and dah:
            self._apretadas = True
        if self._fase == "elemento" and t >= self._fin:
            self._ultimo = self._actual
            self._fase = "hueco"
            self._fin = t + self._unidad()
            self.clave = False
        elif self._fase == "hueco" and t >= self._fin:
            self._decidir(t, dit, dah)
        return self.clave

    # -- bug ------------------------------------------------------------------
    def _update_bug(self, t: float, dit: bool, dah: bool) -> bool:
        if dah:                       # raya manual: dura lo que se mantenga
            self._fase = "reposo"
            self.clave = True
            return True
        if self._fase == "elemento":
            if t >= self._fin:
                self._fase = "hueco"
                self._fin = t + self._unidad()
                self.clave = False
        elif self._fase == "hueco":
            if t >= self._fin:
                self._fase = "reposo"
        if self._fase == "reposo":
            if dit:
                self._fase = "elemento"
                self._fin = t + self._unidad()
                self.clave = True
            else:
                self.clave = False
        return self.clave


# ── El keyer ─────────────────────────────────────────────────────────────────

class CwKeyer:
    """Orquesta backend + sidetone + PTT. Un hilo propio envía el texto en cola
    y, si hay palas, las atiende cuando no hay texto que enviar."""

    def __init__(self, backend, wpm: int = 20, *,
                 ptt: Optional[Callable[[bool], None]] = None,
                 sidetone=None, modo: str = "iambic_a", palas: bool = False,
                 invertir_palas: bool = False, peso: float = 3.0,
                 ptt_antes_ms: float = 30.0, ptt_despues_ms: float = 80.0,
                 ptt_cuelgue_ms: float = 400.0,
                 reloj: Callable[[], float] = time.perf_counter,
                 dormir: Callable[[float], None] = time.sleep):
        self.backend = backend
        self.sidetone = sidetone
        self._ptt_cb = ptt
        self._wpm = self._limitar_wpm(wpm)
        self.peso = float(peso)
        self.palas = bool(palas)
        self.invertir_palas = bool(invertir_palas)
        self.ptt_antes = ptt_antes_ms / 1000.0
        self.ptt_despues = ptt_despues_ms / 1000.0
        self.ptt_cuelgue = ptt_cuelgue_ms / 1000.0
        self._reloj = reloj
        self._dormir = dormir
        self.iambic = Iambic(lambda: unidad_ms(self._wpm) / 1000.0, modo)

        self._cola: deque[tuple[str, Optional[str]]] = deque()
        self._lock = threading.RLock()
        self._despertar = threading.Event()
        self._abortar = threading.Event()
        self._parar_hilo = threading.Event()
        self._hilo: Optional[threading.Thread] = None
        self._clave = False
        self._enviando = False
        self._ptt_activo = False
        self._ultimo_uso = 0.0
        self.on_caracter: Optional[Callable[[str], None]] = None
        self.on_fin: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.error = ""

    # -- parámetros -----------------------------------------------------------
    def fijar_ptt(self, ptt: Optional[Callable[[bool], None]]) -> None:
        """Activa (callback) o desactiva (None) el PTT en caliente. Al desactivarlo
        se suelta el PTT si estaba puesto, para no dejar la radio en TX."""
        if ptt is None:
            try:
                self._liberar_ptt()
            except Exception:
                logger.exception("CW keyer: fallo al soltar PTT")
        self._ptt_cb = ptt

    @staticmethod
    def _limitar_wpm(wpm) -> int:
        try:
            return max(WPM_MIN, min(WPM_MAX, int(wpm)))
        except (TypeError, ValueError):
            return 20

    @property
    def wpm(self) -> int:
        return self._wpm

    def set_wpm(self, wpm) -> None:
        self._wpm = self._limitar_wpm(wpm)

    @property
    def ocupado(self) -> bool:
        with self._lock:
            return bool(self._cola) or self._enviando or self._clave

    @property
    def activo(self) -> bool:
        return self._hilo is not None and self._hilo.is_alive()

    # -- ciclo de vida --------------------------------------------------------
    def iniciar(self) -> bool:
        if self.activo:
            return True
        try:
            self.backend.open()
            self.backend.set_key(False)
            if self.sidetone is not None:
                self.sidetone.open()
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            logger.warning("CW keyer: no se pudo abrir: %s", self.error)
            try:
                self.backend.close()
            except Exception:
                pass
            return False
        self.error = ""
        self._parar_hilo.clear()
        self._abortar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="cw-keyer", daemon=True)
        self._hilo.start()
        return True

    def detener(self) -> None:
        self.parar()
        self._parar_hilo.set()
        self._despertar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=2.0)
        self._hilo = None
        self._soltar_todo()
        try:
            self.backend.close()
        except Exception:
            pass
        if self.sidetone is not None:
            try:
                self.sidetone.close()
            except Exception:
                pass

    # -- envío ----------------------------------------------------------------
    def enviar(self, texto: str, abreviar: bool = False) -> int:
        """Encola texto. Devuelve cuántas letras/espacios se encolaron."""
        if abreviar:
            texto = abreviar_rst(texto)
        items = tokenizar(texto)
        with self._lock:
            if items and self._cola and self._cola[-1][1] is not None:
                self._cola.append((" ", None))      # separa de lo ya encolado
            self._cola.extend(items)
        self._abortar.clear()
        self._despertar.set()
        return len(items)

    def parar(self) -> None:
        """Corta el envío YA: vacía la cola y suelta la clave."""
        with self._lock:
            self._cola.clear()
        self._abortar.set()
        self._despertar.set()
        self._set_clave(False)

    # -- hilo ----------------------------------------------------------------
    def _bucle(self) -> None:
        self._timer_alta_resolucion(True)
        try:
            while not self._parar_hilo.is_set():
                self._paso()
        except Exception as exc:                 # nunca dejar la clave pulsada
            self.error = str(exc) or exc.__class__.__name__
            logger.exception("CW keyer: error en el hilo")
            if self.on_error:
                try:
                    self.on_error(self.error)
                except Exception:
                    pass
        finally:
            self._soltar_todo()
            self._timer_alta_resolucion(False)

    def _paso(self) -> None:
        """Una iteración del hilo: envía una letra si hay cola; si no, atiende
        las palas o espera."""
        with self._lock:
            item = self._cola.popleft() if self._cola else None
        if item is not None:
            self._enviar_item(item)
            with self._lock:
                vacia = not self._cola
            if vacia:
                self._fin_de_texto()
            return
        if self.palas:
            self._atender_palas()
            self._dormir(0.001)
        else:
            self._liberar_ptt_si_toca()
            self._despertar.wait(0.05)
            self._despertar.clear()

    def _enviar_item(self, item: tuple[str, Optional[str]]) -> None:
        self._enviando = True
        try:
            self._enviar_item_interno(item)
        finally:
            self._enviando = False

    def _enviar_item_interno(self, item: tuple[str, Optional[str]]) -> None:
        etiqueta, patron = item
        self._asegurar_ptt()
        t = self._reloj()
        for pulsada, dur_ms in plan_de_patron(patron, unidad_ms(self._wpm), self.peso):
            if self._abortar.is_set():
                break
            if self.palas and self._pala_tocada():
                self.parar()            # break-in: manda la pala
                return
            self._set_clave(pulsada)
            t += dur_ms / 1000.0
            if not self._esperar_hasta(t):
                break
        self._set_clave(False)
        self._ultimo_uso = self._reloj()
        if self.on_caracter and not self._abortar.is_set():
            try:
                self.on_caracter(etiqueta)
            except Exception:
                pass

    def _fin_de_texto(self) -> None:
        self._set_clave(False)
        self._ultimo_uso = self._reloj()
        self._liberar_ptt(retardo=self.ptt_despues)
        if self.on_fin:
            try:
                self.on_fin()
            except Exception:
                pass

    def _esperar_hasta(self, t_obj: float) -> bool:
        """Espera hasta t_obj (reloj del keyer). False si se abortó."""
        while True:
            if self._abortar.is_set():
                return False
            resto = t_obj - self._reloj()
            if resto <= 0:
                return True
            self._dormir(resto - 0.002 if resto > 0.004 else 0)

    # -- palas ---------------------------------------------------------------
    def _leer_palas(self) -> tuple[bool, bool]:
        dit, dah = self.backend.read_paddles()
        return (dah, dit) if self.invertir_palas else (dit, dah)

    def _pala_tocada(self) -> bool:
        dit, dah = self._leer_palas()
        return bool(dit or dah)

    def _atender_palas(self) -> None:
        dit, dah = self._leer_palas()
        t = self._reloj()
        quiere = self.iambic.update(t, dit, dah)
        if quiere and not self._clave:
            self._asegurar_ptt(esperar=False)
        if quiere != self._clave:
            self._set_clave(quiere)
        if quiere:
            self._ultimo_uso = t
        else:
            self._liberar_ptt_si_toca()

    # -- clave / PTT / sidetone ----------------------------------------------
    def _set_clave(self, pulsada: bool) -> None:
        with self._lock:
            if pulsada == self._clave:
                return
            self._clave = pulsada
            try:
                self.backend.set_key(pulsada)
            except Exception as exc:
                self.error = str(exc) or exc.__class__.__name__
                raise
            finally:
                if self.sidetone is not None:
                    try:
                        self.sidetone.set_key(pulsada)
                    except Exception:
                        pass

    def _asegurar_ptt(self, esperar: bool = True) -> None:
        if self._ptt_cb is None or self._ptt_activo:
            return
        try:
            self._ptt_cb(True)
            self._ptt_activo = True
        except Exception:
            logger.exception("CW keyer: fallo al activar PTT")
            return
        if esperar and self.ptt_antes > 0:
            self._esperar_hasta(self._reloj() + self.ptt_antes)

    def _liberar_ptt(self, retardo: float = 0.0) -> None:
        if self._ptt_cb is None or not self._ptt_activo:
            return
        if retardo > 0:
            self._esperar_hasta(self._reloj() + retardo)
        try:
            self._ptt_cb(False)
        except Exception:
            logger.exception("CW keyer: fallo al soltar PTT")
        finally:
            self._ptt_activo = False

    def _liberar_ptt_si_toca(self) -> None:
        if (self._ptt_activo and not self._clave
                and self._reloj() - self._ultimo_uso >= self.ptt_cuelgue):
            self._liberar_ptt()

    def _soltar_todo(self) -> None:
        try:
            self._set_clave(False)
        except Exception:
            pass
        try:
            self._liberar_ptt()
        except Exception:
            pass

    @staticmethod
    def _timer_alta_resolucion(activar: bool) -> None:
        """En Windows el temporizador por defecto tarda ~15 ms; para morse hace
        falta ~1 ms."""
        try:
            import ctypes
            winmm = ctypes.windll.winmm
            (winmm.timeBeginPeriod if activar else winmm.timeEndPeriod)(1)
        except Exception:
            pass


# ── Construcción desde la configuración ──────────────────────────────────────

def _cfg(config, clave: str, defecto: str = "") -> str:
    try:
        valor = config.get(clave, defecto)
    except Exception:
        return defecto
    return defecto if valor is None else str(valor)


def _cfg_bool(config, clave: str, defecto: bool) -> bool:
    return _cfg(config, clave, "1" if defecto else "0").strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def crear_desde_config(config, ptt: Optional[Callable[[bool], None]] = None,
                       simulado: bool = False, radio=None):
    """Crea (sin iniciar) un CwKeyer con los ajustes cwkeyer.* guardados. Con la radio REMOTA (`radio` devuelve su motor CAT) y sin modo de prueba, crea el keyer
    remoto (core/cw_remoto.py): mismo interfaz, pero la clave la mueve el servidor."""
    if radio is not None and not simulado:
        from core.audio_remoto import es_radio_remota
        try:
            remota = es_radio_remota(radio())
        except Exception:
            remota = False
        if remota:
            from core.cw_remoto import CwKeyerRemoto
            return CwKeyerRemoto(radio, wpm=_cfg(config, "cwkeyer.wpm", "20"), linea=_cfg(config, "cwkeyer.line", "dtr"),
                                 invertir=_cfg_bool(config, "cwkeyer.invert", False),
                                 ptt=ptt is not None and _cfg_bool(config, "cwkeyer.ptt_enabled", False))
    from core.cw_sidetone import Sidetone

    from core import radio_salida
    puerto = radio_salida.puerto(config)                  # el puerto es el de la salida de la radio, común a toda la suite
    palas = False                      # las palas ya no se ofrecen: quien las usa las conecta a la radio y lleva su log aparte
    if simulado:
        backend = ClaveSimulada()
    else:
        backend = ClaveSerie(
            puerto,
            linea=_cfg(config, "cwkeyer.line", "dtr"),
            dit=_cfg(config, "cwkeyer.paddle.dit", "cts"),
            dah=_cfg(config, "cwkeyer.paddle.dah", "dsr"),
            invertir=_cfg_bool(config, "cwkeyer.invert", False),
        )
    sidetone = None
    if _cfg_bool(config, "cwkeyer.sidetone.enabled", False):
        try:
            frecuencia = int(float(_cfg(config, "cwkeyer.sidetone.freq", "700")))
        except ValueError:
            frecuencia = 700
        sidetone = Sidetone(frecuencia)
    return CwKeyer(
        backend,
        wpm=_cfg(config, "cwkeyer.wpm", "20"),
        ptt=ptt if _cfg_bool(config, "cwkeyer.ptt_enabled", False) else None,
        sidetone=sidetone,
        modo=_cfg(config, "cwkeyer.mode", "iambic_a"),
        palas=palas,
        invertir_palas=_cfg_bool(config, "cwkeyer.paddle.reverse", False),
    )


# ── Macros ───────────────────────────────────────────────────────────────────

# Banco normal (QSO ordinario). Pensado con lo que se pide en la práctica y en el foro de URE
# («a los que hacen QSOs en CW con el ordenador»): cortas, con «DE» delante del indicativo, sin
# repetir de más, un TU/73 al cerrar, y ambos indicativos en el QSO.
#   · CQ termina en K (llamada general; <KN> solo cuando se contesta a UNA estación).
#   · QRL? para preguntar si la frecuencia está ocupada antes de llamar.
MACROS_DEFECTO: list[str] = [
    "CQ CQ CQ DE {MYCALL} {MYCALL} PSE K",
    "{CALL} DE {MYCALL} {MYCALL} PSE K",
    "{CALL} DE {MYCALL} UR {RST} {RST} <BK>",
    "R [NAME {MYNAME} {MYNAME}] [QTH {MYQTH} {MYQTH}] HW? <BK>",
    "R TU [{NAME}] 73 {CALL} DE {MYCALL} <SK>",
    "{MYCALL}",
    "AGN PSE",
    "QRL?",
]


# Nombre corto de cada macro (lo que se ve en el botón), en el idioma de la aplicación.
def nombres_macros_defecto(concurso: bool = False) -> list[str]:
    from core.i18n import t
    pref = "cwmac.dcname" if concurso else "cwmac.dname"
    return [t(f"{pref}.{i + 1}") for i in range(8)]


# Variables que se pueden usar en una macro, en el orden en que se ofrecen.
VARIABLES_MACRO: tuple[str, ...] = ("CALL", "MYCALL", "RST", "NAME", "MYNAME", "MYQTH", "NR", "EXCH")


def etiqueta_macro(nombre: str, texto: str, largo: int = 34) -> str:
    """Lo que se escribe en el botón de una macro: su nombre, o si no tiene, el
    principio del texto."""
    nombre = (nombre or "").strip()
    if nombre:
        return nombre
    texto = (texto or "").strip()
    return texto if len(texto) <= largo else texto[:largo - 1] + "…"


# Banco de macros para concursos (se usa cuando hay un concurso abierto):
# {NR} es el número de serie del siguiente QSO y {EXCH} el intercambio enviado.
MACROS_CONCURSO_DEFECTO: list[str] = [
    "CQ TEST {MYCALL} {MYCALL} TEST",
    "{CALL} {RST} {NR}",
    "TU {MYCALL}",
    "{MYCALL}",
    "{CALL}",
    "NR?",
    "AGN",
    "QRZ? {MYCALL}",
]


def expandir_macro(texto: str, **variables: str) -> str:
    """Sustituye {CALL}, {MYCALL}, {RST}, {NR}, {NAME}... (mayúsculas o no).
    Variables sin valor se sustituyen por vacío. Lo que va entre corchetes es opcional: si
    alguna variable de dentro no tiene valor, el bloque entero se omite («[NAME {MYNAME}]»
    no envía nada si no hay nombre)."""
    valores = {k.upper(): str(v or "") for k, v in variables.items()}

    def valor(m: re.Match) -> str:
        return valores.get(m.group(1).upper(), "")

    def opcional(m: re.Match) -> str:
        dentro = m.group(1)
        if any(not valores.get(v.upper(), "").strip() for v in re.findall(r"\{([A-Za-z_]+)\}", dentro)):
            return ""
        return dentro

    texto = re.sub(r"\[([^\[\]]*)\]", opcional, str(texto or ""))
    texto = re.sub(r"\{([A-Za-z_]+)\}", valor, texto)
    return re.sub(r"[ \t]{2,}", " ", texto).strip()
