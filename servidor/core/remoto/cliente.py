"""
core/remoto/cliente.py -- RadioLog
Backend CAT «Remoto»: habla con un servidor de radio (core/remoto/servidor.py) en lugar de abrir un puerto serie. Para el resto de RadioLog es una
radio más: Control de equipo, las teclas del panel principal y el libro de guardia lo usan sin saber que la radio está en otro sitio.

El servidor manda el estado de la radio varias veces por segundo y este backend lo guarda en `state`; las órdenes salen por la red y el estado
local se adelanta para que la pantalla responda al instante (el servidor lo confirma justo después).
"""
from __future__ import annotations

import base64
import logging
import socket
import ssl
import threading
import time

from core.cat_engine import CAP_SIMULATOR, CatBackendBase
from core.remoto import certificado as C
from core.remoto import protocolo as P
from core.remoto.conexion import Conexion

log = logging.getLogger("radiolog.remoto.cliente")


class CatBackendRemoto(CatBackendBase):
    name = "Remoto"

    def __init__(self, host: str, port: int = P.PUERTO_DEFECTO, clave: str = "", timeout: float = 5.0, cifrado: bool = True):
        super().__init__()
        self.cifrado = bool(cifrado)
        self.host = str(host).strip()
        self.port = int(port)
        self.clave = clave
        self.timeout = float(timeout)
        self._sock: socket.socket | None = None
        self._conn: Conexion | None = None
        self._lock_envio = threading.Lock()
        self._parar = threading.Event()
        self._hilos: list[threading.Thread] = []
        self._caps: dict = {}
        self._barrido_res = None
        self._audio_info: dict = {}
        self._id = 0
        self._ultimo_rx = 0.0
        self._primer_estado = threading.Event()
        self.ultimo_aviso = ""
        self.on_audio = None               # función(bytes PCM16 8 kHz mono) que recibe el audio de recepción
        self.audio_recibidas = 0
        self.audio_perdidas = 0
        self.latencia_ms = None            # ida y vuelta al servidor (media suavizada); None hasta que llega la primera respuesta
        self.ultimo_audio = 0.0            # cuándo llegó la última trama de audio
        self._audio_pedido = False
        self._audio_n = 0
        self._tx_n = 0
        self.state.backend_name = f"Remoto ({self.host}:{self.port})"

    # ── conexión ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def get_capabilities(self) -> dict:
        return dict(self._caps) if self._caps else CAP_SIMULATOR

    def connect(self) -> bool:
        self.disconnect()
        self._parar.clear()
        self._primer_estado.clear()
        self.latencia_ms = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            self.state.last_error = f"No se puede llegar a {self.host}:{self.port} ({e})"
            return False
        sock.settimeout(self.timeout)
        enlace = b""
        if self.cifrado:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE                 # el certificado es del propio servidor: se comprueba con la clave compartida (enlace)
                sock = ctx.wrap_socket(sock)
                enlace = C.huella_de_der(sock.getpeercert(binary_form=True))
            except (ssl.SSLError, OSError) as e:
                self.state.last_error = ("No se ha podido establecer la conexión cifrada: ¿el servidor está arrancado sin cifrado? (" + str(e) + ")")
                try:
                    sock.close()
                except OSError:
                    pass
                return False
        try:
            lector = P.LectorLineas()
            hola = self._leer_uno(sock, lector)
            if hola.get("t") != "hola":
                raise ValueError(hola.get("msg", "El servidor no contesta como se esperaba"))
            if int(hola.get("v", 0)) != P.VERSION_PROTOCOLO:
                raise ValueError("Versiones distintas del servidor y de RadioLog")
            reto_cliente = P.reto_nuevo()
            sock.sendall(P.codificar({"t": "auth", "resp": P.respuesta(self.clave, str(hola["reto"]), b"cliente", enlace), "reto": reto_cliente}))
            ok = self._leer_uno(sock, lector)
            if ok.get("t") == "error":
                raise ValueError(ok.get("msg", "Conexión rechazada"))
            if ok.get("t") != "ok" or not P.comprobar(self.clave, reto_cliente, b"servidor", ok.get("resp", ""), enlace):
                raise ValueError("El servidor no ha demostrado tener la clave: no te conectes a él")
            self._caps = P.normalizar_caps(ok.get("caps") or {})
            self._audio_info = dict(ok.get("audio") or {})
        except (OSError, ValueError, KeyError) as e:
            self.state.last_error = str(e)
            if isinstance(e, socket.timeout) and not self.cifrado:
                self.state.last_error = "El servidor no contesta (si usa cifrado, activa «Cifrado» en la configuración de la radio remota)"
            try:
                sock.close()
            except OSError:
                pass
            return False

        self._sock = sock
        self._conn = Conexion(sock)                 # a partir de aquí se lee en un hilo y se escribe desde varios
        self._ultimo_rx = time.monotonic()
        for objetivo, nombre in ((lambda: self._leer(lector), "remoto-lector"), (self._latido, "remoto-latido")):
            h = threading.Thread(target=objetivo, daemon=True, name=nombre)
            h.start()
            self._hilos.append(h)
        if not self._primer_estado.wait(self.timeout):
            self.state.last_error = "El servidor no manda el estado de la radio"
            self.disconnect()
            return False
        if not self.state.connected:
            self.state.last_error = "Conectado al servidor, pero la radio no contesta (¿está encendida y con el cable?)"
            self.disconnect()
            return False
        self.state.last_error = ""
        self._sondeo_ok()
        return True

    @staticmethod
    def _leer_uno(sock, lector: P.LectorLineas) -> dict:
        while True:
            datos = sock.recv(4096)
            if not datos:
                raise OSError("el servidor ha cerrado la conexión")
            for linea in lector.alimentar(datos):
                return P.decodificar(linea)

    def disconnect(self) -> None:
        self._parar.set()
        sock, self._sock = self._sock, None
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.cerrar()
        elif sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        for h in self._hilos:
            if h is not threading.current_thread():
                h.join(timeout=2.0)
        self._hilos = []
        self.state.connected = False

    # ── hilos ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _leer(self, lector: P.LectorLineas) -> None:
        conn = self._conn
        motivo = ""
        while not self._parar.is_set() and conn is not None:
            try:
                datos = conn.leer(1.0)
            except OSError as e:
                motivo = f"{type(e).__name__}: {e}"
                break
            if datos is None:
                continue
            if not datos:
                motivo = "el servidor cerró la conexión"
                break
            try:
                for linea in lector.alimentar(datos):
                    self._recibido(P.decodificar(linea))
            except (ValueError, UnicodeDecodeError):
                motivo = "mensaje no válido del servidor"
                log.warning("Mensaje no válido del servidor")
                break
        if not self._parar.is_set():
            self.state.connected = False
            self.state.last_error = "Se ha perdido la conexión con el servidor (" + motivo + ")"
            log.warning("Conexión con la radio remota perdida: %s", motivo)
            conn = self._conn
            if conn is not None:
                conn.cerrar()                      # se cierra YA: así el servidor libera la radio y se puede volver a conectar enseguida

    def _recibido(self, m: dict) -> None:
        self._ultimo_rx = time.monotonic()
        tipo = m.get("t")
        if tipo == "estado":
            P.aplicar_estado(self.state, m.get("s", {}))
            self.state.connected = bool(m.get("radio")) and self._sock is not None
            self.state.backend_name = f"Remoto ({self.host}:{self.port})"
            self._primer_estado.set()
        elif tipo == "resp" and not m.get("ok"):
            self.state.last_error = str(m.get("msg", "")) or self.state.last_error
        elif tipo == "pong":
            ts = m.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                rtt = max(0.0, min(60000.0, (time.monotonic() - float(ts)) * 1000.0))
                self.latencia_ms = rtt if self.latencia_ms is None else 0.7 * self.latencia_ms + 0.3 * rtt
        elif tipo == "barrido":
            self._barrido_res = {k: v for k, v in m.items() if k != "t"}
        elif tipo == "audio":
            self._audio(m)
        elif tipo == "aviso":
            self.ultimo_aviso = str(m.get("msg", ""))
            log.warning("Aviso del servidor: %s", self.ultimo_aviso)
        elif tipo == "error":
            self.state.last_error = str(m.get("msg", ""))

    def _audio(self, m: dict) -> None:
        n = int(m.get("n", 0))
        if self._audio_n and n > self._audio_n + 1:
            self.audio_perdidas += n - self._audio_n - 1
        self._audio_n = n
        self.audio_recibidas += 1
        self.ultimo_audio = time.monotonic()
        cb = self.on_audio
        if cb is not None:
            try:
                cb(base64.b64decode(m.get("d", "")))
            except Exception:
                log.exception("Error al entregar el audio")

    def enlace_vivo(self) -> bool:
        """¿Hay conexión con el servidor y ha dado señales hace poco?"""
        return self._sock is not None and time.monotonic() - self._ultimo_rx < 3.0

    def estado_audio(self) -> str:
        """«no_hay» (el servidor no tiene audio), «apagado» (no se ha pedido), «ok» (llegan tramas) o «sin_datos» (se ha pedido y no llega nada)."""
        if not self._audio_info.get("rx"):
            return "no_hay"
        if not self._audio_pedido:
            return "apagado"
        return "ok" if time.monotonic() - self.ultimo_audio < 1.5 else "sin_datos"

    def audio_disponible(self) -> bool:
        return bool(self.state.connected and self._audio_info.get("rx"))

    def audio_tx_disponible(self) -> bool:
        return bool(self.state.connected and self._audio_info.get("tx"))

    def audio_tx(self, on: bool) -> bool:
        """Pide al servidor que abra (o cierre) la salida de audio hacia la radio."""
        return self._orden("audio_tx", bool(on))

    def enviar_audio_tx(self, pcm16: bytes) -> bool:
        """Una trama de micrófono (PCM16, 8 kHz, mono). Seguro llamarla desde el hilo de audio."""
        self._tx_n += 1
        return self._enviar({"t": "audio_tx", "n": self._tx_n, "d": base64.b64encode(pcm16).decode("ascii")})

    def audio_rx(self, on: bool) -> bool:
        """Pide al servidor que empiece (o pare) de mandar el audio de recepción."""
        if not on:
            self._audio_n = 0
        self._audio_pedido = bool(on)
        return self._orden("audio_rx", bool(on))

    def _latido(self) -> None:
        while not self._parar.wait(1.0):
            if not self._enviar({"t": "ping", "ts": time.monotonic()}):
                break

    # ── envío ────────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _enviar(self, mensaje: dict) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.escribir(P.codificar(mensaje))
            return True
        except OSError:
            return False

    def _orden(self, op: str, *args) -> bool:
        self._id += 1
        return self._enviar({"t": "cmd", "id": self._id, "op": op, "a": list(args)})

    def poll(self) -> bool:
        if not self.state.connected:
            return False
        if time.monotonic() - self._ultimo_rx > 6.0:
            if self._sondeo_perdido("sin noticias del servidor"):
                self.state.connected = False
                self.state.last_error = "El servidor ha dejado de contestar"
                return False
            return True
        self._sondeo_ok()
        return True

    def refresh_rig_params(self) -> None:
        self._orden("refrescar")

    # ── barrido de banda (lo hace el servidor, junto a la radio; aquí solo se pide y se recoge) ──────────────────────────────

    def barrido_iniciar(self, centro_mhz: float, span_khz: float, paso_khz: float) -> bool:
        self._barrido_res = None
        return self._orden("barrido", float(centro_mhz), float(span_khz), float(paso_khz))

    def barrido_cancelar(self) -> None:
        self._orden("barrido_cancelar")

    def barrido_resultado(self):
        r, self._barrido_res = self._barrido_res, None
        return r

    _leer_estado_rig = refresh_rig_params

    def leer_filtros_rx(self) -> None:
        self._orden("leer_filtros")

    def leer_tuner(self) -> None:
        self._orden("leer_tuner")

    # ── órdenes (el estado local se adelanta; el servidor lo confirma) ────────────────────────────────────────────────────

    def set_frequency(self, f) -> bool:
        self.state.frequency_mhz = float(f)
        return self._orden("set_frequency", float(f))

    def set_vfob_frequency(self, f) -> bool:
        self.state.vfob_freq = float(f)
        return self._orden("set_vfob_frequency", float(f))

    def set_mode(self, mode) -> bool:
        self.state.mode = str(mode)
        return self._orden("set_mode", str(mode))

    def set_active_vfo(self, vfo: int) -> None:
        super().set_active_vfo(vfo)
        self._orden("set_active_vfo", int(vfo) & 1)

    def set_split(self, on: bool) -> None:
        super().set_split(on)
        self._orden("set_split", bool(on))

    def vfo_swap(self) -> None:
        self._orden("vfo_swap")

    def vfo_a_to_b(self) -> None:
        self._orden("vfo_a_to_b")

    def vfo_b_to_a(self) -> None:
        self._orden("vfo_b_to_a")

    def set_af_gain(self, v) -> None:
        super().set_af_gain(v); self._orden("set_af_gain", self.state.af_gain)

    def set_rf_gain(self, v) -> None:
        super().set_rf_gain(v); self._orden("set_rf_gain", self.state.rf_gain)

    def set_squelch(self, v) -> None:
        super().set_squelch(v); self._orden("set_squelch", self.state.squelch)

    def set_mic_gain(self, v) -> None:
        super().set_mic_gain(v); self._orden("set_mic_gain", self.state.mic_gain)

    def set_power(self, v) -> None:
        super().set_power(v); self._orden("set_power", self.state.power_w)

    def set_nb(self, on) -> None:
        super().set_nb(on); self._orden("set_nb", bool(on))

    def set_nr(self, on) -> None:
        super().set_nr(on); self._orden("set_nr", bool(on))

    def set_notch(self, on) -> None:
        super().set_notch(on); self._orden("set_notch", bool(on))

    def set_proc(self, on) -> None:
        super().set_proc(on); self._orden("set_proc", bool(on))

    def set_vox(self, on) -> None:
        super().set_vox(on); self._orden("set_vox", bool(on))

    def set_att(self, v) -> None:
        super().set_att(v); self._orden("set_att", self.state.att)

    def set_ipa(self, v) -> None:
        super().set_ipa(v); self._orden("set_ipa", self.state.ipa)

    def set_agc(self, v) -> None:
        super().set_agc(v); self._orden("set_agc", self.state.agc)

    def set_ptt(self, on) -> None:
        super().set_ptt(on); self._orden("set_ptt", bool(on))

    def tune(self) -> None:
        self.state.tuning = True
        self._orden("tune")
