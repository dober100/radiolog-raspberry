"""
core/remoto/cw_servidor.py -- RadioLog
CW por la radio REMOTA: el keyer (core/cw_keyer.py, el mismo de siempre) se ejecuta AQUÍ, en el servidor (la Raspberry), y mueve una línea DTR o RTS de un puerto
serie del servidor. El cliente solo manda texto, velocidad y «parar» y recibe lo que va saliendo; los puntos y las rayas los cronometra el servidor con su
propio reloj, así que la latencia de la red no los altera. El morse es idéntico al local (mismas `tokenizar` y `plan_de_patron`).

SEGURIDAD (una radio que se queda con la clave pulsada es un problema de verdad):
  * El puerto de la clave lo decide SOLO el servidor (`--puerto-clave`, a mano o «auto» si se identifica sin dudas; ver core/remoto/autoconfig.py). Sin puerto
    identificado la manipulación queda DESACTIVADA. Nunca puede ser el puerto del CAT.
  * La línea (DTR/RTS) y la inversión físicas también son del servidor: el cliente dice lo que espera y, si no coincide, se rechaza (nunca se pulsa otra línea).
  * El puerto se abre UNA vez, al preparar el servidor, con las dos líneas en su estado de reposo ANTES de abrirlo (la clave en su nivel de «suelta» y la otra
    línea siempre baja) y se mantiene abierto: así no hay un pulso de apertura cada vez que alguien conecta. (Algunos controladores pueden dar un pulso al abrir:
    no se da por descartado; por eso solo ocurre al arrancar.)
  * La clave se suelta SIEMPRE: al parar, al cerrar, si el cliente desaparece o deja de dar pings, si falla algo, y por un vigilante propio si lleva pulsada más de
    `tecla_max_s` (ningún elemento de morse legítimo pasa de 3 puntos a 5 wpm = 0,72 s).
  * Una sola transmisión a la vez: no se envía CW con una transmisión digital o de voz en marcha, ni al revés (lo comprueba el servidor).

Mensajes (cliente -> servidor, «t»:«cw»):  abrir · enviar · wpm · ptt · parar · cerrar.    (servidor -> cliente):  cw_resp · cw_car · cw_fin · cw_error · cw_cerrado.
Sin Qt. Necesita pyserial (solo para abrir el puerto).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from core import cw_keyer as K
from core.puerto_radio import PuertoRadio, compartido

log = logging.getLogger("radiolog.remoto.cw")

TECLA_MAX_S = 2.0                   # la clave no puede estar pulsada más de esto
TEXTO_MAX = 512                     # caracteres por mensaje
COLA_MAX = 2000                     # elementos (letras y espacios) pendientes como mucho
LINEAS = ("dtr", "rts")
PESO_MIN, PESO_MAX = 2.0, 4.0


def _numero(valor, minimo: float, maximo: float, defecto: float) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor != valor:
        return defecto
    return min(maximo, max(minimo, float(valor)))


class ClaveVigilada:
    """Envuelve el backend de clave: apunta desde cuándo está pulsada (para el vigilante) y permite soltarla a la fuerza."""

    def __init__(self, backend, reloj=time.monotonic) -> None:
        self._b = backend
        self._reloj = reloj
        self.desde: Optional[float] = None

    def open(self) -> None:
        self._b.open()

    def close(self) -> None:
        try:
            self._b.close()
        finally:
            self.desde = None

    def set_key(self, pulsada: bool) -> None:
        self._b.set_key(pulsada)
        self.desde = self._reloj() if pulsada else None

    def read_paddles(self):
        return (False, False)

    def soltar(self) -> bool:
        """Suelta la clave pase lo que pase (aunque el keyer crea que ya está suelta). True si queda suelta."""
        try:
            self._b.set_key(False)
            self.desde = None
            return True
        except Exception:
            log.error("NO SE HA PODIDO SOLTAR LA CLAVE DE CW", exc_info=True)
            return False


class GestorCw:
    def __init__(self, puerto_clave: str = "", linea: str = "dtr", invertir: bool = False, *, avisar: Callable[[dict], None],
                 ptt: Callable[[bool], None], otro_tx: Callable[[], str], radio_lista: Callable[[], str], puerto_cat: Callable[[], str],
                 modelo: Callable[[], str], cliente_vivo: Callable[[], str], puerto_radio: Optional[PuertoRadio] = None,
                 resolver_auto: Optional[Callable] = None, tecla_max_s: float = TECLA_MAX_S, reloj=time.monotonic) -> None:
        self._cfg_puerto = str(puerto_clave or "").strip()
        self._linea = str(linea or "").strip().lower()
        self.invertir = bool(invertir)
        self._avisar, self._ptt, self._otro_tx, self._radio_lista = avisar, ptt, otro_tx, radio_lista
        self._puerto_cat, self._modelo, self._cliente_vivo = puerto_cat, modelo, cliente_vivo
        self._pr = puerto_radio if puerto_radio is not None else compartido()
        self._resolver_auto = resolver_auto
        self.tecla_max_s = float(tecla_max_s)
        self._reloj = reloj
        self._lock = threading.RLock()
        self._puerto = ""
        self._permanente = False                   # el puerto está abierto y en reposo (lo mantiene este gestor)
        self.motivo = ""
        self._keyer: Optional[K.CwKeyer] = None
        self._clave: Optional[ClaveVigilada] = None
        self._enviando = False
        self._ptt_propio = False
        self._hilo: Optional[threading.Thread] = None
        self._parar_hilo = threading.Event()
        self._error_config = ""
        if self._cfg_puerto and self._linea not in LINEAS:
            self._error_config = f"la línea de clave del servidor no es válida ({linea!r}): solo dtr o rts"

    # -- configuración y puerto ----------------------------------------------------------------------------------------------
    @property
    def linea(self) -> str:
        return self._linea if self._linea in LINEAS else "dtr"

    @property
    def configurado(self) -> bool:
        return bool(self._cfg_puerto)

    @property
    def disponible(self) -> bool:
        return self._permanente

    @property
    def activo(self) -> bool:
        """¿Hay una sesión de CW abierta (el keyer en marcha)?"""
        return self._keyer is not None

    @property
    def enviando(self) -> bool:
        """¿Está transmitiendo (hay texto en cola o enviándose)?"""
        return self._enviando

    @property
    def ocupado(self) -> bool:
        """Transmitiendo o con el PTT puesto por el CW: nada más puede transmitir."""
        return self._enviando or self._ptt_propio

    @property
    def ptt_propio(self) -> bool:
        return self._ptt_propio

    def _resolver(self) -> tuple[str, str]:
        if not self._cfg_puerto:
            return "", "el servidor no tiene puerto de clave (arráncalo con --puerto-clave, o «auto» si es una FTDX10)"
        if self._error_config:
            return "", self._error_config
        cat = str(self._puerto_cat() or "")
        if self._cfg_puerto.lower() == "auto":
            if not cat:
                return "", "aún no se conoce el puerto de la radio, y sin él no se puede identificar el de la clave"
            if self._resolver_auto is None:
                return "", "no hay forma de identificar el puerto de la clave: ponlo a mano (--puerto-clave /dev/ttyUSB1)"
            puerto, motivo = self._resolver_auto(cat, str(self._modelo() or ""))
            if not puerto:
                return "", (motivo or "no se puede identificar el puerto de la clave") + " — configúralo a mano con --puerto-clave"
        else:
            puerto = self._cfg_puerto
        if cat and puerto.upper() == cat.upper():
            return "", f"el puerto de la clave ({puerto}) es el mismo que el del CAT de la radio: tiene que ser otro"
        return puerto, ""

    def preparar(self) -> bool:
        """Abre (una sola vez) el puerto de la clave con las líneas en reposo. Se puede llamar cuantas veces haga falta: si ya está listo, no hace nada."""
        with self._lock:
            if self._permanente:
                return True
            puerto, motivo = self._resolver()
            if not puerto:
                self._poner_motivo(motivo)
                return False
            otra = "rts" if self.linea == "dtr" else "dtr"
            try:
                self._pr.adquirir(puerto, reposo={self.linea: self.invertir, otra: False})     # reposo ANTES de abrir; la otra línea, siempre baja
            except Exception as exc:
                self._poner_motivo(f"no se puede abrir el puerto de la clave {puerto}: {exc}")
                return False
            self._puerto, self._permanente = puerto, True
            try:
                self._pr.poner(self.linea, self.invertir)                                      # y, ya abierto, se comprueba/asegura el reposo
                self._pr.poner(otra, False)
            except Exception:
                log.error("No se pudo asegurar el reposo de las líneas de %s", puerto, exc_info=True)
            self.motivo = ""
            log.info("Manipulación CW lista: %s, línea %s%s (reposo %s)", puerto, self.linea.upper(), " invertida" if self.invertir else "", "alto" if self.invertir else "bajo")
            return True

    def _poner_motivo(self, motivo: str) -> None:
        if motivo != self.motivo:
            log.warning("Manipulación CW desactivada: %s", motivo)
        self.motivo = motivo

    def info(self) -> dict:
        """Lo que se le cuenta al cliente al conectar: si hay CW y, si no, por qué; y la línea física que espera el servidor."""
        ok = self.preparar()
        return {"cw": ok, "cw_linea": self.linea, "cw_invertir": self.invertir, "cw_motivo": "" if ok else self.motivo}

    # -- sesión ----------------------------------------------------------------------------------------------------------------
    def abrir(self, p: dict) -> tuple[bool, str]:
        """Abre el keyer con lo que pide el cliente, comprobándolo contra lo físico del servidor."""
        with self._lock:
            if not self.preparar():
                return False, "Manipulación CW desactivada en el servidor: " + self.motivo
            linea = str(p.get("linea", "") or "").strip().lower()
            if linea != self.linea:
                return False, (f"La línea de clave del servidor es {self.linea.upper()} y RadioLog pide {linea.upper() or '?'}: "
                               "cámbiala en uno de los dos para que coincidan")
            invertir = p.get("invertir")
            if not isinstance(invertir, bool) or invertir != self.invertir:
                return False, (f"La clave del servidor está {'invertida' if self.invertir else 'sin invertir'} y RadioLog pide lo contrario: "
                               "cámbialo en uno de los dos para que coincidan")
            r = self._radio_lista()
            if r:
                return False, r
            self.cerrar("nueva", avisar=False)
            wpm = int(_numero(p.get("wpm"), K.WPM_MIN, K.WPM_MAX, 20))
            peso = _numero(p.get("peso"), PESO_MIN, PESO_MAX, 3.0)
            antes = _numero(p.get("ptt_antes_ms"), 0.0, 2000.0, 30.0)
            despues = _numero(p.get("ptt_despues_ms"), 0.0, 2000.0, 80.0)
            cuelgue = _numero(p.get("ptt_cuelgue_ms"), 0.0, 5000.0, 400.0)
            clave = ClaveVigilada(K.ClaveSerie(self._puerto, linea=self.linea, invertir=self.invertir, puerto_radio=self._pr), self._reloj)
            keyer = K.CwKeyer(clave, wpm=wpm, ptt=self._ptt_cb if p.get("ptt") is True else None, sidetone=None, modo="iambic_a", palas=False, peso=peso,
                              ptt_antes_ms=antes, ptt_despues_ms=despues, ptt_cuelgue_ms=cuelgue)
            keyer.on_caracter, keyer.on_fin, keyer.on_error = self._on_caracter, self._on_fin, self._on_error
            if not keyer.iniciar():
                self._invalidar_puerto()                              # (el puerto pudo desaparecer: se vuelve a abrir en cuanto se pueda)
                return False, "No se puede iniciar la clave de CW: " + (keyer.error or "?")
            self._keyer, self._clave, self._enviando = keyer, clave, False
            self._parar_hilo.clear()
            self._hilo = threading.Thread(target=self._vigilar, daemon=True, name="remoto-cw-vigilante")
            self._hilo.start()
            log.info("CW abierto: %d wpm, PTT %s", wpm, "sí" if p.get("ptt") is True else "no")
            return True, ""

    def enviar(self, texto, abreviar: bool = False) -> tuple[bool, str, int]:
        with self._lock:
            keyer = self._keyer
            if keyer is None:
                return False, "La manipulación de CW no está abierta", 0
            if not isinstance(texto, str) or not texto.strip():
                return True, "", 0
            if not self._enviando:
                otro = self._otro_tx()
                if otro:
                    return False, otro, 0
                r = self._radio_lista()
                if r:
                    return False, r, 0
            if len(keyer._cola) > COLA_MAX:
                return False, "Demasiado texto pendiente de enviar", 0
            self._enviando = True
            n = keyer.enviar(texto[:TEXTO_MAX], abreviar=bool(abreviar))
            if n == 0 and not keyer.ocupado:
                self._enviando = False
            return True, "", n

    def poner_wpm(self, wpm) -> None:
        keyer = self._keyer
        if keyer is not None:
            keyer.set_wpm(_numero(wpm, K.WPM_MIN, K.WPM_MAX, keyer.wpm))

    def poner_ptt(self, activo) -> None:
        keyer = self._keyer
        if keyer is not None:
            keyer.fijar_ptt(self._ptt_cb if activo is True else None)

    def parar(self) -> None:
        """Corta el envío YA: sin texto pendiente y con la clave suelta. La sesión sigue abierta; el PTT (si lo hay) lo suelta el keyer en cuanto toca."""
        with self._lock:
            keyer, clave = self._keyer, self._clave
            if keyer is None:
                return
            keyer.parar()
            if clave is not None:
                clave.soltar()
            fue = self._enviando
            self._enviando = False
        if fue:
            self._avisar({"t": "cw_fin"})

    def cerrar(self, motivo: str = "cerrado", avisar: bool = True) -> None:
        """Cierra la sesión: clave suelta, PTT fuera, keyer parado. Se puede llamar siempre."""
        with self._lock:
            keyer, clave, hilo = self._keyer, self._clave, self._hilo
            self._keyer = self._clave = self._hilo = None
            self._parar_hilo.set()
        if keyer is None:
            return
        try:
            keyer.detener()
        except Exception:
            log.error("Fallo al detener el keyer de CW", exc_info=True)
        if clave is not None:
            clave.soltar()
        if self._ptt_propio:
            try:
                self._ptt(False)
            except Exception:
                log.error("No se pudo soltar el PTT del CW", exc_info=True)
            self._ptt_propio = False
        self._enviando = False
        if hilo is not None and hilo is not threading.current_thread():
            hilo.join(timeout=1.0)
        if avisar and motivo not in ("cliente", "nueva"):
            self._avisar({"t": "cw_cerrado", "motivo": motivo})

    def cerrar_todo(self) -> None:
        """Al parar el servidor: la sesión y, por último, el puerto (las líneas bajan solas al cerrarlo)."""
        self.cerrar("servidor")
        with self._lock:
            if self._permanente:
                self._permanente = False
                try:
                    self._pr.liberar()
                except Exception:
                    log.error("No se pudo cerrar el puerto de la clave", exc_info=True)

    # -- del keyer ---------------------------------------------------------------------------------------------------------------
    def _ptt_cb(self, on: bool) -> None:
        if on:
            self._ptt(True)
            self._ptt_propio = True
        else:
            try:
                self._ptt(False)
            finally:
                self._ptt_propio = False

    def _on_caracter(self, etiqueta: str) -> None:
        self._avisar({"t": "cw_car", "c": etiqueta})

    def _on_fin(self) -> None:
        with self._lock:
            keyer = self._keyer
            if keyer is None or keyer.ocupado:                    # (llegó más texto justo ahora: sigue transmitiendo)
                return
            fue = self._enviando
            self._enviando = False
        if fue:
            self._avisar({"t": "cw_fin"})

    def _on_error(self, mensaje: str) -> None:
        log.error("Error en el keyer de CW: %s", mensaje)
        self._avisar({"t": "cw_error", "msg": f"Fallo en la clave de CW: {mensaje}"})
        threading.Thread(target=self._cerrar_por_error, daemon=True).start()

    def _cerrar_por_error(self) -> None:
        self.cerrar("error")
        self._invalidar_puerto()

    def _invalidar_puerto(self) -> None:
        """Tras un fallo con el puerto (la radio se apagó y el USB desapareció, p. ej.) el manejador que se tenía abierto ya no vale: se suelta y `preparar()` lo vuelve a
        abrir en cuanto el puerto esté otra vez (el vigilante del servidor lo intenta cada pocos segundos)."""
        with self._lock:
            if not self._permanente:
                return
            self._permanente = False
            try:
                self._pr.liberar()
            except Exception:
                log.error("No se pudo soltar el puerto de la clave", exc_info=True)
        self._poner_motivo("el puerto de la clave ha fallado: se reabrirá cuando vuelva")

    def _vigilar(self) -> None:
        """Un vigilante independiente del keyer: la clave no puede quedarse pulsada, y si el cliente desaparece se cierra todo."""
        while not self._parar_hilo.wait(0.02):
            clave, keyer = self._clave, self._keyer
            if clave is None or keyer is None:
                return
            motivo = self._cliente_vivo()
            if motivo:
                log.warning("CW cortado: %s", motivo)
                self.cerrar("cliente" if "desconect" in motivo else "sin_ping", avisar=False)
                return
            desde = clave.desde
            if desde is not None and self._reloj() - desde > self.tecla_max_s:
                log.error("La clave de CW llevaba más de %.1f s pulsada: se suelta", self.tecla_max_s)
                self._avisar({"t": "cw_error", "msg": f"La clave llevaba más de {self.tecla_max_s:g} s pulsada: se ha soltado"})
                self.parar()
            elif not keyer.activo:
                self._avisar({"t": "cw_error", "msg": "El keyer de CW se ha parado"})
                threading.Thread(target=self._cerrar_por_error, daemon=True).start()
                return
