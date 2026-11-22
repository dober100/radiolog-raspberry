"""
core/remoto/servidor.py -- RadioLog
Servidor del mando remoto: está junto a la radio (un PC o una Raspberry Pi), tiene el CAT y atiende a UN cliente RadioLog a la vez.

Seguridad:
  * Cada lado demuestra que tiene la clave compartida con un HMAC del reto del otro (la clave nunca viaja).
  * El cliente solo puede mandar las órdenes de protocolo.ORDENES; con argumentos de tipo comprobado.
  * PTT: el servidor lo suelta solo si el cliente desaparece, si deja de dar señales de vida (ping) mientras se transmite, o si se supera el
    tiempo máximo de transmisión. Esto no depende del cliente.
  * Intentos de clave fallidos: pausa antes de contestar y límite por dirección.
Nada de Qt: funciona igual en Windows y en Linux (Raspberry Pi).
"""
from __future__ import annotations

import base64
import logging
import ipaddress
import json
import queue
import socket
import ssl
import threading
import time
from pathlib import Path

import numpy as np

from core.remoto import audio as A
from core.remoto import certificado as C
from core.remoto.conexion import Conexion
from core.remoto import protocolo as P
from core.remoto.reproductor import ReproductorAudio
from core.remoto.cw_servidor import GestorCw
from core.remoto.tx_digital import SalidaProgramada

log = logging.getLogger("radiolog.remoto.servidor")


class _SesionTxd:
    """Una transmisión digital en curso en el servidor."""

    def __init__(self, salida, antes: float, despues: float, max_s: float, dueno: str, t_apertura: float) -> None:
        self.salida = salida
        self.antes, self.despues, self.max_s, self.dueno = antes, despues, max_s, dueno
        self.t_apertura = t_apertura
        self.t0 = None                 # hora (reloj del servidor) a la que suena la primera muestra
        self.t_ptt = 0.0               # hora a la que hay que poner el PTT
        self.t_ptt_real = 0.0
        self.ptt_puesto = False
        self.fin = False               # el cliente ya no manda más audio
        self.abortar = None            # (motivo, texto) si hay que cortar
        self.retraso = 0.0
        self.tramas = 0
        self.hilo = None


class ServidorRadio:
    def __init__(self, motor, clave: str, host: str = "0.0.0.0", puerto: int = P.PUERTO_DEFECTO,
                 max_tx_s: float = 180.0, tx_sin_ping_s: float = 3.0, silencio_max_s: float = 10.0,
                 ciclo_s: float = 0.25, reconectar_radio_s: float = 3.0, audio_entrada: str | None = None, captura_factory=None,
                 audio_salida: str | None = None, salida_factory=None, tx_pico: float = 0.8, tx_ganancia: float = 1.0,
                 cifrado: bool = True, carpeta_cert=None, redes_permitidas=None, emparejar_fichero=None, buscar_radio=None, estado_radio_fichero=None,
                 salida_digital_factory=None, reloj=time.monotonic, puerto_clave: str = "", linea_clave: str = "dtr", invertir_clave: bool = False,
                 puerto_radio_clave=None, cw_tecla_max_s: float = 2.0):
        if not clave or len(clave) < 8:
            raise ValueError("La clave es obligatoria (mínimo 8 caracteres)")
        self.motor = motor                      # un CatEngine
        self.clave = clave
        self.host, self.puerto = host, int(puerto)
        self.max_tx_s = float(max_tx_s)
        self.tx_sin_ping_s = float(tx_sin_ping_s)
        self.silencio_max_s = float(silencio_max_s)
        self.ciclo_s = float(ciclo_s)
        self.reconectar_radio_s = float(reconectar_radio_s)

        self._parar = threading.Event()
        self._sock_escucha: socket.socket | None = None
        self._hilos: list[threading.Thread] = []
        self._lock_cliente = threading.Lock()
        self._cliente: Conexion | None = None
        self._lock_envio = threading.Lock()
        self._ultimo_ping = 0.0
        self._ptt_on = False
        self._ptt_desde = 0.0
        self._fallos_auth: dict[str, list[float]] = {}
        self._ultimo_estado_json = ""
        self._ultimo_envio_estado = 0.0
        self._hilo_barrido: threading.Thread | None = None
        self._barrido_cancelar_ev = threading.Event()
        # audio de recepción: nombre (o trozo) de la tarjeta de sonido de la radio; `captura_factory(entregar)` solo se usa en las pruebas
        self.audio_entrada = audio_entrada
        self._captura_factory = captura_factory
        self._captura = None
        self._audio_on = False
        self._audio_seq = 0
        self._audio_cola: queue.Queue = queue.Queue(maxsize=100)
        # audio de transmisión: tarjeta de sonido (de salida) por la que se manda la voz a la radio; `salida_factory()` solo se usa en las pruebas
        self.audio_salida = audio_salida
        self._salida_factory = salida_factory
        self._salida = None
        self._tx_audio_on = False
        self.tx_pico = max(0.05, min(1.0, float(tx_pico)))         # tope de amplitud de lo que se manda a la radio (evita saturar la modulación)
        self.tx_ganancia = max(0.0, float(tx_ganancia))
        self.tx_tramas = 0
        self.tx_descartadas = 0
        # transmisión de modos digitales (FT8, RTTY…): el audio llega por adelantado y suena en una línea de tiempo absoluta (core/remoto/tx_digital.py);
        # el PTT lo manda el servidor (ver _txd_*). `salida_digital_factory()` solo se usa en las pruebas; `reloj` es el reloj monótono del servidor.
        self._salida_digital_factory = salida_digital_factory
        self._reloj = reloj
        self._txd: _SesionTxd | None = None
        self._lock_txd = threading.RLock()
        # CW por la radio remota: el keyer se ejecuta aquí y mueve una línea DTR/RTS del puerto de clave (core/remoto/cw_servidor.py). Sin `puerto_clave` queda desactivado.
        from core.remoto import autoconfig as _AC
        self._cw = GestorCw(puerto_clave, linea_clave, invertir_clave, avisar=self._txd_avisar, ptt=self._cw_ptt, otro_tx=self._cw_otro_tx,
                            radio_lista=self._cw_radio_lista, puerto_cat=lambda: str(getattr(self.motor.backend, "port", "") or ""),
                            modelo=lambda: str(getattr(self.motor.backend, "model", "") or ""), cliente_vivo=self._cw_cliente_vivo,
                            puerto_radio=puerto_radio_clave, resolver_auto=_AC.puerto_clave_auto, tecla_max_s=cw_tecla_max_s, reloj=time.monotonic)
        # cifrado TLS con certificado propio (se crea la primera vez) y, opcionalmente, solo desde ciertas redes (p. ej. la de Tailscale)
        self.cifrado = bool(cifrado)
        self._ctx_tls: ssl.SSLContext | None = None
        self._huella = b""
        if self.cifrado:
            if carpeta_cert is None:
                raise ValueError("Con cifrado hace falta la carpeta del certificado (carpeta_cert)")
            cert, clave_priv = C.asegurar(carpeta_cert)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(cert), str(clave_priv))
            self._ctx_tls = ctx
            self._huella = C.huella(cert)
        self._redes = [ipaddress.ip_network(r.strip(), strict=False) for r in (redes_permitidas or []) if str(r).strip()]
        # emparejamiento: durante unos minutos, a quien lo pida se le entrega la clave UNA vez (para no tener que teclearla); se abre desde la página de la
        # Raspberry (que escribe este fichero con la hora límite) o con abrir_emparejamiento()
        self._emparejar_fichero = Path(emparejar_fichero) if emparejar_fichero else None
        self._emparejar_hasta = 0.0
        # radio «a buscar»: con `buscar_radio` (una función que devuelve {puerto, baudios, modelo, id} o None) el servidor arranca aunque la radio esté apagada o
        # desenchufada y, mientras no está conectada, la busca él solo cada pocos segundos (en la Raspberry se enciende después que ella, o se apaga y se enciende).
        # `estado_radio_fichero`: aquí deja escrito cómo está la radio (la página de la Raspberry lo enseña).
        self._buscar_radio = buscar_radio
        self._estado_radio_fichero = Path(estado_radio_fichero) if estado_radio_fichero else None
        self._ultimo_estado_radio = ""

    # ── emparejamiento ───────────────────────────────────────────────────────────────────────────────────────────────────────

    def abrir_emparejamiento(self, segundos: float = 120.0) -> None:
        self._emparejar_hasta = time.monotonic() + float(segundos)
        log.warning("Emparejamiento abierto durante %d s: el primer RadioLog que lo pida recibirá la clave", int(segundos))

    def emparejamiento_abierto(self) -> bool:
        if time.monotonic() < self._emparejar_hasta:
            return True
        if self._emparejar_fichero is not None:
            try:
                return time.time() < float(json.loads(self._emparejar_fichero.read_text(encoding="utf-8")).get("hasta", 0))
            except (OSError, ValueError, AttributeError, TypeError):
                return False
        return False

    def _cerrar_emparejamiento(self) -> None:
        self._emparejar_hasta = 0.0
        if self._emparejar_fichero is not None:
            try:
                self._emparejar_fichero.unlink()
            except OSError:
                pass

    def _anotar_conexion(self, ip: str) -> None:
        """Deja escrito (junto a los demás ficheros de estado) que un RadioLog se acaba de conectar con la clave y, si llegó por la VPN de Tailscale (100.64.0.0/10), que la VPN
        queda PROBADA de punta a punta: la página de la Raspberry lo enseña en su lista «lista para llevar»."""
        if self._emparejar_fichero is None:
            return
        try:
            por_vpn = ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
            carpeta = self._emparejar_fichero.parent
            nota = json.dumps({"ip": ip, "cuando": time.time(), "vpn": por_vpn})
            (carpeta / "conexion.json").write_text(nota, encoding="utf-8")
            if por_vpn:
                (carpeta / "vpn_probada.json").write_text(nota, encoding="utf-8")
        except (OSError, ValueError):
            pass

    def _anotar_emparejado(self, ip: str) -> None:
        """Deja escrito con quién se acaba de emparejar (junto al fichero de emparejamiento), para que la página de la Raspberry lo enseñe."""
        if self._emparejar_fichero is None:
            return
        try:
            (self._emparejar_fichero.parent / "emparejado.json").write_text(json.dumps({"ip": ip, "cuando": time.time()}), encoding="utf-8")
        except OSError:
            pass

    def _emparejar(self, sock, ip: str) -> None:
        """Un RadioLog pide la clave: solo se le da si el emparejamiento está abierto Y la conexión va cifrada, y se cierra al darla (una sola vez)."""
        if not self.cifrado:
            self._enviar(sock, {"t": "error", "msg": "El emparejamiento necesita la conexión cifrada"})
        elif not self.emparejamiento_abierto():
            self._enviar(sock, {"t": "error", "msg": "El servidor no está esperando un emparejamiento: púlselo en su página"})
        else:
            self._cerrar_emparejamiento()
            self._anotar_emparejado(ip)
            log.warning("Emparejamiento: clave entregada a %s", ip)
            self._enviar(sock, {"t": "clave", "clave": self.clave})

    # ── ciclo de vida ────────────────────────────────────────────────────────────────────────────────────────────────────────

    def iniciar(self) -> int:
        """Abre el puerto y arranca los hilos. Devuelve el puerto real (útil con puerto=0 en las pruebas)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.puerto))
        s.listen(2)
        s.settimeout(0.5)
        self._sock_escucha = s
        self.puerto = s.getsockname()[1]
        for objetivo, nombre in ((self._aceptar, "remoto-aceptar"), (self._bucle_radio, "remoto-radio"), (self._vigilante, "remoto-vigilante"),
                                 (self._bucle_audio, "remoto-audio")):
            h = threading.Thread(target=objetivo, daemon=True, name=nombre)
            h.start()
            self._hilos.append(h)
        log.info("Servidor de radio escuchando en %s:%d", self.host, self.puerto)
        if self._cw.configurado:
            self._cw.preparar()                                    # abre el puerto de la clave (en reposo) ya, no al primer envío
        return self.puerto

    def detener(self) -> None:
        self._parar.set()
        self._barrido_cancelar_ev.set()
        h = self._hilo_barrido
        if h is not None:
            h.join(timeout=3.0)
        self._txd_abortar("servidor", "servidor detenido")
        self._cw.cerrar_todo()
        self._soltar_ptt("servidor detenido")
        self._audio_rx(False)
        self._audio_tx(False)
        with self._lock_cliente:
            if self._cliente is not None:
                self._cerrar(self._cliente)
                self._cliente = None
        if self._sock_escucha is not None:
            try:
                self._sock_escucha.close()
            except OSError:
                pass
        for h in self._hilos:
            h.join(timeout=2.0)

    @staticmethod
    def _cerrar(sock) -> None:
        if isinstance(sock, Conexion):
            sock.cerrar()
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    # ── conexiones ───────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _aceptar(self) -> None:
        while not self._parar.is_set():
            try:
                sock, dir_ = self._sock_escucha.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._ip_permitida(dir_[0]):
                log.warning("Conexión rechazada de %s (fuera de las redes permitidas)", dir_[0])
                self._cerrar(sock)
                continue
            threading.Thread(target=self._atender, args=(sock, dir_[0]), daemon=True, name="remoto-cliente").start()

    def _enviar(self, sock, mensaje: dict) -> bool:
        try:
            if isinstance(sock, Conexion):
                sock.escribir(P.codificar(mensaje))
            else:
                with self._lock_envio:
                    sock.sendall(P.codificar(mensaje))
            return True
        except OSError:
            return False

    def _demasiados_fallos(self, ip: str) -> bool:
        ahora = time.monotonic()
        lista = [t for t in self._fallos_auth.get(ip, []) if ahora - t < 60.0]
        self._fallos_auth[ip] = lista
        return len(lista) >= 5

    def _ip_permitida(self, ip: str) -> bool:
        if not self._redes:
            return True
        try:
            direccion = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(direccion in red for red in self._redes)

    def _atender(self, sock: socket.socket, ip: str) -> None:
        sock.settimeout(5.0)
        conn = None
        try:
            if self._ctx_tls is not None:
                try:
                    sock = self._ctx_tls.wrap_socket(sock, server_side=True)
                except (ssl.SSLError, OSError) as e:
                    log.info("Conexión de %s sin cifrado válido: %s", ip, e)
                    return
            if self._demasiados_fallos(ip):
                self._enviar(sock, {"t": "error", "msg": "Demasiados intentos con clave incorrecta: espera un minuto"})
                return
            reto = P.reto_nuevo()
            self._enviar(sock, {"t": "hola", "v": P.VERSION_PROTOCOLO, "reto": reto})
            lector = P.LectorLineas()
            auth = None
            while auth is None:
                datos = sock.recv(4096)
                if not datos:
                    return
                for linea in lector.alimentar(datos):
                    auth = P.decodificar(linea)
                    break
            if auth.get("t") == "emparejar":
                self._emparejar(sock, ip)
                return
            if auth.get("t") != "auth" or not P.comprobar(self.clave, reto, b"cliente", auth.get("resp", ""), self._huella):
                self._fallos_auth.setdefault(ip, []).append(time.monotonic())
                log.warning("Clave incorrecta desde %s", ip)
                time.sleep(0.5)
                self._enviar(sock, {"t": "error", "msg": "Clave incorrecta"})
                return
            with self._lock_cliente:
                viejo = self._cliente
                if viejo is not None:
                    if time.monotonic() - self._ultimo_ping <= 3.0:               # el cliente de ahora sigue vivo: no se le quita la radio
                        self._enviar(sock, {"t": "error", "msg": "La radio ya está siendo usada por otro cliente"})
                        return
                    log.warning("El cliente anterior no da señales de vida: se le sustituye por el nuevo")
                    self._cliente = None
                    self._cerrar(viejo)
                    self._ptt_on and self._soltar_ptt_sin_aviso("se cambió de cliente")
                conn = Conexion(sock)               # a partir de aquí se lee en este hilo y se escribe desde varios
                self._cliente = conn
            try:
                caps = self.motor.get_capabilities()
            except Exception:
                caps = {}
            self._enviar(conn, {"t": "ok", "resp": P.respuesta(self.clave, str(auth.get("reto", "00")), b"servidor", self._huella),
                                "caps": caps, "modelo": caps.get("name", "Radio"), "v": P.VERSION_PROTOCOLO,
                                "audio": {"rx": self._audio_disponible(), "tx": self._tx_disponible(), "txd": self._txd_disponible(), "fs": A.FS_RED, "codec": "pcm16",
                                          "trama_ms": A.TRAMA_MS, **self._cw.info()},
                                "cifrado": self.cifrado})
            log.info("Cliente conectado desde %s", ip)
            self._anotar_conexion(ip)
            self._ultimo_ping = time.monotonic()
            self._ultimo_estado_json = ""                # que el cliente reciba el estado completo enseguida
            self._bucle_cliente(conn, lector)
        except (OSError, ValueError, UnicodeDecodeError) as e:
            log.info("Conexión de %s terminada: %s", ip, e)
        finally:
            with self._lock_cliente:
                era_el_cliente = conn is not None and self._cliente is conn
                if era_el_cliente:
                    self._cliente = None
            self._cerrar(conn if conn is not None else sock)
            if era_el_cliente:
                self._barrido_cancelar_ev.set()
                self._txd_abortar("cliente", "el cliente se ha desconectado")
                self._cw.cerrar("cliente")
                self._soltar_ptt("el cliente se ha desconectado")
                self._audio_rx(False)
                self._audio_tx(False)
                log.info("Cliente %s fuera", ip)

    def _bucle_cliente(self, conn: Conexion, lector: P.LectorLineas) -> None:
        ultimo_mensaje = time.monotonic()
        while not self._parar.is_set():
            datos = conn.leer(1.0)
            if datos is None:                        # nada en 1 s
                if time.monotonic() - ultimo_mensaje > self.silencio_max_s:
                    log.info("Cliente sin señales de vida: se corta")
                    return
                continue
            if not datos:
                return
            ultimo_mensaje = time.monotonic()
            for linea in lector.alimentar(datos):
                self._mensaje(conn, P.decodificar(linea))

    # ── mensajes y órdenes ───────────────────────────────────────────────────────────────────────────────────────────────────

    def _mensaje(self, sock, m: dict) -> None:
        tipo = m.get("t")
        if tipo == "ping":
            self._ultimo_ping = time.monotonic()
            self._enviar(sock, {"t": "pong", "ts": m.get("ts"), "st": self._reloj()})      # st: la hora del servidor (para sincronizar los relojes)
        elif tipo == "audio_tx":
            self._trama_tx(m)
        elif tipo == "txd":
            self._txd_mensaje(sock, m)
        elif tipo == "cw":
            self._cw_mensaje(sock, m)
        elif tipo == "cmd":
            ok, msg = self._ejecutar(str(m.get("op", "")), m.get("a", []))
            self._enviar(sock, {"t": "resp", "id": m.get("id"), "ok": ok, "msg": msg})
        else:
            self._enviar(sock, {"t": "error", "msg": "Mensaje desconocido"})

    @staticmethod
    def _convertir(valor, tipo):
        """Comprueba el tipo de un argumento (sin aceptar «true» como número ni cosas raras)."""
        if tipo is bool:
            if isinstance(valor, bool):
                return valor
            raise ValueError("se esperaba verdadero/falso")
        if tipo is int:
            if isinstance(valor, bool):
                raise ValueError("se esperaba un número entero")
            if isinstance(valor, int):
                return valor
            if isinstance(valor, float) and valor.is_integer():
                return int(valor)
            raise ValueError("se esperaba un número entero")
        if tipo is float:
            if isinstance(valor, bool) or not isinstance(valor, (int, float)):
                raise ValueError("se esperaba un número")
            return float(valor)
        if tipo is str:
            if isinstance(valor, str) and len(valor) <= 32:
                return valor
            raise ValueError("se esperaba un texto corto")
        raise ValueError("tipo no admitido")

    def _ejecutar(self, op: str, args) -> tuple[bool, str]:
        regla = P.ORDENES.get(op)
        if regla is None:
            log.warning("Orden no permitida: %r", op[:40])
            return False, "Orden no permitida"
        metodo, tipos = regla
        if not isinstance(args, list) or len(args) != len(tipos):
            return False, "Argumentos incorrectos"
        try:
            valores = [self._convertir(a, t) for a, t in zip(args, tipos)]
        except ValueError as e:
            return False, str(e)
        try:
            if op == "set_ptt":
                return self._ptt(bool(valores[0]))
            if op == "barrido":
                return self._barrido(float(valores[0]), float(valores[1]), float(valores[2]))
            if op == "barrido_cancelar":
                self._barrido_cancelar_ev.set()
                return True, ''
            if op == "audio_rx":
                return self._audio_rx(bool(valores[0]))
            if op == "audio_tx":
                return self._audio_tx(bool(valores[0]))
            resultado = getattr(self.motor, metodo)(*valores)
            self._ultimo_estado_json = ""            # que el cliente vea el cambio sin esperar
            return (resultado is not False), ""
        except Exception as e:
            log.warning("La orden %s falló: %s", op, e, exc_info=True)
            return False, "La radio no ha podido hacerlo"

    # ── PTT con seguro ───────────────────────────────────────────────────────────────────────────────────────────────────────

    # ── barrido de banda (panorama / cascada de RF) ──────────────────────────────────────────────────────────────────────────

    def _rechazar_barrido(self, motivo: str) -> tuple[bool, str]:
        """Todo barrido pedido acaba en UN mensaje «barrido» para el cliente, también el que se rechaza."""
        with self._lock_cliente:
            sock = self._cliente
        if sock is not None:
            self._enviar(sock, {"t": "barrido", "ok": False, "msg": motivo, "completo": False, "centro_hz": 0, "paso_hz": 0, "f0_hz": 0, "db": []})
        return False, motivo

    def _barrido(self, centro_mhz: float, span_khz: float, paso_khz: float) -> tuple[bool, str]:
        from core import barrido_rf
        error = barrido_rf.comprobar_parametros(centro_mhz, span_khz, paso_khz)
        if error:
            return self._rechazar_barrido(error)
        if not self.motor.is_connected():
            return self._rechazar_barrido("La radio no está conectada")
        if self._ptt_on or self.motor.get_ptt():
            return self._rechazar_barrido("No se barre transmitiendo")
        if self.motor.barriendo or (self._hilo_barrido is not None and self._hilo_barrido.is_alive()):
            return self._rechazar_barrido("Ya hay un barrido en marcha")
        self._barrido_cancelar_ev = threading.Event()
        self._hilo_barrido = threading.Thread(target=self._hacer_barrido, args=(centro_mhz, span_khz, paso_khz, self._barrido_cancelar_ev),
                                              daemon=True, name="remoto-barrido")
        self._hilo_barrido.start()
        return True, ""

    def _hacer_barrido(self, centro_mhz, span_khz, paso_khz, cancelar) -> None:
        from core import barrido_rf
        res = barrido_rf.barrer(self.motor, centro_mhz, span_khz, paso_khz, cancelar=cancelar)
        with self._lock_cliente:
            sock = self._cliente
        if sock is not None:
            self._enviar(sock, dict(res, t="barrido"))
        self._ultimo_estado_json = ""

    def _ptt(self, on: bool, txd: bool = False) -> tuple[bool, str]:
        if on:
            if self._txd is not None and not txd:
                return False, "Hay una transmisión digital en curso"
            if self._cw.ocupado and not txd:
                return False, "Hay una transmisión de CW en curso"
            if not self.motor.is_connected():
                return False, "La radio no está conectada"
            if self.motor.barriendo:
                return False, "Se está barriendo la banda: espera a que termine"
            self._ptt_desde = time.monotonic()
            self._ptt_on = True
            self.motor.set_ptt(True)
            log.info("PTT activado")
        else:
            self._soltar_ptt("pedido por el cliente")
        self._ultimo_estado_json = ""
        return True, ""

    def _soltar_ptt_sin_aviso(self, motivo: str) -> None:
        """Como _soltar_ptt, para llamarla con el candado del cliente tomado (no avisa al cliente)."""
        if not self._ptt_on:
            return
        self._ptt_on = False
        try:
            self.motor.set_ptt(False)
        except Exception:
            log.error("No se pudo soltar el PTT", exc_info=True)
        log.warning("PTT soltado: %s", motivo)
        self._ultimo_estado_json = ""

    def _soltar_ptt(self, motivo: str) -> None:
        if not self._ptt_on:
            return
        self._ptt_on = False
        try:
            self.motor.set_ptt(False)
        except Exception:
            log.error("No se pudo soltar el PTT", exc_info=True)
        log.warning("PTT soltado: %s", motivo)
        with self._lock_cliente:
            sock = self._cliente
        if sock is not None:
            self._enviar(sock, {"t": "aviso", "msg": "PTT soltado: " + motivo})
        self._ultimo_estado_json = ""

    def _vigilante(self) -> None:
        desde = None                                              # desde cuándo se está dando la razón para soltar el PTT
        vueltas = 0
        while not self._parar.wait(0.1):
            vueltas += 1
            if vueltas % 50 == 0 and self._cw.configurado and not self._cw.disponible:
                self._cw.preparar()                               # el puerto de la clave aún no estaba (radio apagada, sin enchufar…): se reintenta
            if not self._ptt_on:
                desde = None
                continue
            ahora = time.monotonic()
            with self._lock_cliente:
                hay_cliente = self._cliente is not None
            motivo = None
            if not hay_cliente:
                motivo = "no hay cliente"
            elif ahora - self._ultimo_ping > self.tx_sin_ping_s:
                motivo = "el cliente no da señales de vida"
            elif ahora - self._ptt_desde > self.max_tx_s:
                motivo = "tiempo máximo de transmisión (%d s)" % self.max_tx_s
            if motivo is None:
                desde = None
                continue
            if self._txd is not None or self._cw.activo:             # una transmisión digital o de CW: ella baja el audio / suelta la clave y el PTT; este seguro solo
                desde = desde or ahora                               # entra si en 1,5 s no lo ha hecho
                if ahora - desde < 1.5:
                    continue
            self._soltar_ptt(motivo)
            desde = None

    # ── audio de recepción ───────────────────────────────────────────────────────────────────────────────────────────────────

    def _audio_disponible(self) -> bool:
        return self._captura_factory is not None or bool(self.audio_entrada)

    def _audio_rx(self, on: bool) -> tuple[bool, str]:
        if not on:
            cap, self._captura = self._captura, None
            self._audio_on = False
            if cap is not None:
                cap.cerrar()
                log.info("Audio de recepción parado")
            return True, ""
        if not self._audio_disponible():
            return False, "Este servidor no tiene audio configurado (falta --audio-entrada)"
        if self._audio_on:
            return True, ""
        factoria = self._captura_factory or (lambda entregar: A.CapturaRX(entregar, self.audio_entrada))
        cap = factoria(self._frame_audio)
        if not cap.abrir():
            return False, getattr(cap, "error", "") or "No se puede abrir la tarjeta de sonido"
        while not self._audio_cola.empty():
            try:
                self._audio_cola.get_nowait()
            except queue.Empty:
                break
        self._captura = cap
        self._audio_on = True
        log.info("Audio de recepción en marcha")
        return True, ""

    def _frame_audio(self, trama: bytes) -> None:
        """Llamado por la captura con cada trama de 40 ms. Si el envío va atrasado se tira lo más antiguo: mejor un salto que un retraso creciente."""
        try:
            self._audio_cola.put_nowait(trama)
        except queue.Full:
            try:
                self._audio_cola.get_nowait()
                self._audio_cola.put_nowait(trama)
            except (queue.Empty, queue.Full):
                pass

    def _bucle_audio(self) -> None:
        while not self._parar.is_set():
            try:
                trama = self._audio_cola.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._audio_on:
                continue
            while self._audio_cola.qsize() > 12:                        # el cliente va lento: se saltan tramas viejas
                try:
                    trama = self._audio_cola.get_nowait()
                except queue.Empty:
                    break
            with self._lock_cliente:
                sock = self._cliente
            if sock is None:
                continue
            self._audio_seq += 1
            self._enviar(sock, {"t": "audio", "n": self._audio_seq, "d": base64.b64encode(trama).decode("ascii")})

    # ── audio de transmisión ─────────────────────────────────────────────────────────────────────────────────────────────────

    def _tx_disponible(self) -> bool:
        return self._salida_factory is not None or bool(self.audio_salida)

    def _audio_tx(self, on: bool) -> tuple[bool, str]:
        """Abre o cierra la salida de audio hacia la radio. Abrirla no transmite nada: el audio solo se reproduce mientras el PTT está puesto."""
        if not on:
            self._tx_audio_on = False
            salida, self._salida = self._salida, None
            if salida is not None:
                salida.cerrar()
                log.info("Salida de audio de transmisión cerrada")
            return True, ""
        if not self._tx_disponible():
            return False, "Este servidor no tiene salida de audio para transmitir (falta --audio-salida)"
        if self._txd is not None:
            return False, "Hay una transmisión digital en curso"
        if self._cw.ocupado:
            return False, "Hay una transmisión de CW en curso"
        if self._tx_audio_on:
            return True, ""
        salida = self._salida_factory() if self._salida_factory else ReproductorAudio(dispositivo=self.audio_salida, retardo_ms=80, max_ms=300)
        if not salida.abrir():
            return False, getattr(salida, "error", "") or "No se puede abrir la salida de audio hacia la radio"
        self._salida = salida
        self._tx_audio_on = True
        log.info("Salida de audio de transmisión abierta")
        return True, ""

    def _trama_tx(self, m: dict) -> None:
        """Una trama de micrófono del cliente. Solo se reproduce con el PTT puesto: sin él se tira (con VOX encendido, audio suelto en la radio la haría transmitir)."""
        salida = self._salida
        if not (self._tx_audio_on and self._ptt_on and salida is not None):
            self.tx_descartadas += 1
            return
        try:
            datos = base64.b64decode(m.get("d", ""), validate=True)
        except (ValueError, TypeError):
            return
        if not datos or len(datos) > 2 * 2 * A.MUESTRAS_TRAMA or len(datos) % 2:      # como mucho dos tramas; nada raro
            return
        x = A.de_pcm16(datos) * self.tx_ganancia
        salida.alimentar(A.a_pcm16(np.clip(x, -self.tx_pico, self.tx_pico)))
        self.tx_tramas += 1

    # ── transmisión de modos digitales ───────────────────────────────────────────────────────────────────────────────────────
    #
    #   cliente -> servidor  {"t":"txd","op":"abrir","id":n,"antes_ms":150,"despues_ms":100,"max_s":120,"dueno":"ft8"}   (contesta «txd_resp»)
    #                        {"t":"txd","op":"inicio","t0":hora_del_servidor}      a qué hora suena la primera muestra
    #                        {"t":"txd","op":"datos","i":muestra,"d":base64}       audio PCM16 8 kHz; `i` = nº de su primera muestra
    #                        {"t":"txd","op":"fin"}                                no hay más audio: acaba, espera el «después» y suelta el PTT
    #                        {"t":"txd","op":"parar"}                              corta ya (bajada de 30 ms) y suelta el PTT
    #   servidor -> cliente  {"t":"txd_estado",...} cada 100 ms y {"t":"txd_fin","motivo":...,"msg":...} al acabar por lo que sea
    #
    # El PTT lo pone y lo suelta el SERVIDOR: se pone `antes_ms` antes de la primera muestra y se suelta `despues_ms` después de que SUENE la última
    # (contando la latencia de la tarjeta). Si el cliente se va, deja de dar señales de vida, falla el PTT o se pasa el tiempo máximo, el audio se corta con una
    # bajada suave y el PTT se suelta. Una transmisión digital y una de voz (o dos digitales) nunca a la vez.

    def _txd_disponible(self) -> bool:
        return self._salida_digital_factory is not None or bool(self.audio_salida)

    def _txd_responder(self, sock, ident, ok: bool, msg: str = "") -> None:
        self._enviar(sock, {"t": "txd_resp", "id": ident, "ok": ok, "msg": msg, "st": self._reloj()})

    def _txd_mensaje(self, sock, m: dict) -> None:
        op = m.get("op")
        try:
            if op == "abrir":
                self._txd_abrir(sock, m)
            elif op == "inicio":
                self._txd_inicio(m)
            elif op == "datos":
                self._txd_datos(m)
            elif op == "fin":
                self._txd_fin()
            elif op == "parar":
                self._txd_abortar("parado", "parado por el cliente")
            else:
                self._enviar(sock, {"t": "error", "msg": "Mensaje de transmisión desconocido"})
        except Exception:
            log.error("Fallo en un mensaje de transmisión digital (%r)", op, exc_info=True)
            self._txd_abortar("error", "fallo en el servidor")

    @staticmethod
    def _numero(valor, minimo: float, maximo: float, defecto: float) -> float:
        if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor != valor:
            return defecto
        return min(maximo, max(minimo, float(valor)))

    def _txd_abrir(self, sock, m: dict) -> None:
        ident = m.get("id")
        if not self._txd_disponible():
            return self._txd_responder(sock, ident, False, "Este servidor no tiene salida de audio para transmitir (falta --audio-salida)")
        antes = self._numero(m.get("antes_ms"), 0.0, 1000.0, 150.0) / 1000.0
        despues = self._numero(m.get("despues_ms"), 0.0, 1000.0, 100.0) / 1000.0
        max_s = self._numero(m.get("max_s"), 1.0, self.max_tx_s, self.max_tx_s)
        dueno = str(m.get("dueno", "") or "")[:16]
        with self._lock_txd:
            if self._txd is not None:
                return self._txd_responder(sock, ident, False, "Ya hay una transmisión digital en curso")
            if self._cw.ocupado:
                return self._txd_responder(sock, ident, False, "Hay una transmisión de CW en curso")
            if self._ptt_on or self._tx_audio_on:
                return self._txd_responder(sock, ident, False, "La radio ya está transmitiendo (voz)")
            if not self.motor.is_connected():
                return self._txd_responder(sock, ident, False, "La radio no está conectada")
            if self.motor.barriendo:
                return self._txd_responder(sock, ident, False, "Se está barriendo la banda: espera a que termine")
            salida = self._salida_digital_factory() if self._salida_digital_factory else SalidaProgramada(self.audio_salida, reloj=self._reloj)
            if not salida.abrir():
                return self._txd_responder(sock, ident, False, getattr(salida, "error", "") or "No se puede abrir la salida de audio hacia la radio")
            ses = _SesionTxd(salida, antes, despues, max_s, dueno, self._reloj())
            self._txd = ses
            ses.hilo = threading.Thread(target=self._txd_bucle, args=(ses,), daemon=True, name="remoto-txd")
            ses.hilo.start()
        log.info("Transmisión digital abierta (%s): antes %.0f ms, después %.0f ms, máximo %.0f s", dueno or "?", antes * 1000, despues * 1000, max_s)
        self._txd_responder(sock, ident, True)

    def _txd_inicio(self, m: dict) -> None:
        ses = self._txd
        if ses is None or ses.t0 is not None:
            return
        ahora = self._reloj()
        t0 = self._numero(m.get("t0"), ahora - 60.0, ahora + 30.0, ahora + ses.antes + 0.05)
        minimo = ahora + ses.antes + 0.05                       # el PTT tiene que poder ponerse `antes` ANTES de la primera muestra
        t0e = max(t0, minimo)
        ses.retraso = t0e - t0
        if ses.retraso > 0.02:
            log.warning("La transmisión digital llegó tarde: empieza %.0f ms después de lo pedido", ses.retraso * 1000)
        ses.salida.anclar(t0e)
        ses.t_ptt = t0e - ses.antes - 0.04
        ses.t0 = t0e

    def _txd_datos(self, m: dict) -> None:
        ses = self._txd
        if ses is None or ses.fin:
            return
        try:
            datos = base64.b64decode(m.get("d", ""), validate=True)
        except (ValueError, TypeError):
            return
        indice = m.get("i")
        if isinstance(indice, bool) or not isinstance(indice, int) or indice < 0 or not datos or len(datos) % 2 or len(datos) > 64 * 1024:
            return
        if indice + len(datos) // 2 > int((ses.max_s + 5.0) * A.FS_RED):
            return self._txd_abortar("error", "demasiado audio para el tiempo máximo")
        x = np.clip(A.de_pcm16(datos) * self.tx_ganancia, -self.tx_pico, self.tx_pico)
        ses.salida.poner(indice, x)
        ses.tramas += 1

    def _txd_fin(self) -> None:
        ses = self._txd
        if ses is None or ses.fin:
            return
        ses.fin = True
        ses.salida.finalizar()

    def _txd_abortar(self, motivo: str, msg: str = "") -> None:
        """Corta la transmisión digital que haya (con su bajada suave) y suelta el PTT. Se puede llamar siempre; si no hay nada, no hace nada."""
        ses = self._txd
        if ses is None:
            return
        if ses.abortar is None:
            ses.abortar = (motivo, msg)
        h = ses.hilo
        if h is not None and h is not threading.current_thread():
            h.join(timeout=2.0)

    def _soltar_ptt_reintentando(self, motivo: str) -> bool:
        """Suelta el PTT si está puesto. True si queda suelto. Si la radio no contesta, se insiste (≈ 1 s) y, si aun así no, NO se da por soltado: `_ptt_on` sigue
        puesto para que el vigilante lo siga intentando (cada 0,1 s si no hay cliente)."""
        if not self._ptt_on:
            return True
        for intento in range(10):
            try:
                self.motor.set_ptt(False)
                self._ptt_on = False
                self._ultimo_estado_json = ""
                log.info("PTT soltado (%s)", motivo)
                return True
            except Exception:
                log.error("No se pudo soltar el PTT (intento %d)", intento + 1, exc_info=(intento == 0))
                time.sleep(0.1)
        log.error("NO SE HA PODIDO SOLTAR EL PTT: se sigue intentando")
        return False

    def _txd_soltar_ptt(self, ses, motivo: str) -> bool:
        """Suelta el PTT que puso esta transmisión digital (ver `_soltar_ptt_reintentando`)."""
        if not (ses.ptt_puesto and self._ptt_on):
            return True
        return self._soltar_ptt_reintentando("transmisión digital: " + motivo)

    def _txd_decidir(self, ses) -> tuple[str, str] | None:
        """¿Hay que cortar ya la transmisión? (motivo, texto) o None."""
        if ses.abortar is not None:
            return ses.abortar
        if self._parar.is_set():
            return "servidor", "servidor detenido"
        with self._lock_cliente:
            hay_cliente = self._cliente is not None
        if not hay_cliente:
            return "cliente", "el cliente se ha desconectado"
        if time.monotonic() - self._ultimo_ping > self.tx_sin_ping_s:
            return "sin_ping", "el cliente no da señales de vida"
        if ses.ptt_puesto and not self._ptt_on:
            return "ptt", "el PTT se soltó por otro motivo"
        if getattr(ses.salida, "atascada", False):
            return "audio", "la tarjeta de sonido de la radio ha dejado de sacar audio"
        if ses.t0 is None and not ses.fin and self._reloj() - ses.t_apertura > 15.0:
            return "inactiva", "la transmisión se abrió y no ha llegado nada"
        return None

    def _txd_bucle(self, ses) -> None:
        motivo, msg = "acabado", ""
        try:
            ultimo_estado = 0.0
            while True:
                ahora = self._reloj()
                corte = self._txd_decidir(ses)
                if corte is not None:
                    motivo, msg = corte
                    break
                if ses.t0 is not None and not ses.ptt_puesto and ahora >= ses.t_ptt:
                    ok, texto = self._ptt(True, txd=True)               # el PTT, antes de la primera muestra
                    if not ok:
                        motivo, msg = "ptt", texto
                        break
                    ses.ptt_puesto = True
                    ses.t_ptt_real = ahora
                if ses.ptt_puesto and ahora - ses.t_ptt_real > ses.max_s:
                    motivo, msg = "tiempo", "tiempo máximo de transmisión (%d s)" % int(ses.max_s)
                    break
                if ses.fin and ses.t0 is None:                           # se cerró sin haber transmitido nada
                    break
                if ses.salida.terminada:
                    break
                if ahora - ultimo_estado >= 0.1:
                    ultimo_estado = ahora
                    self._txd_avisar({"t": "txd_estado", "pos": ses.salida.posicion, "recibido": ses.salida.recibido, "ptt": self._ptt_on,
                                      "huecos": int(getattr(ses.salida, "huecos", 0)), "retraso_ms": int(1000 * (ses.retraso + getattr(ses.salida, "retraso_s", 0.0))),
                                      "arrancada": bool(getattr(ses.salida, "arrancada", False))})
                time.sleep(0.01)
        except Exception as exc:
            log.error("Fallo en la transmisión digital", exc_info=True)
            motivo, msg = "error", str(exc) or "fallo en el servidor"
        finally:
            self._txd_terminar(ses, motivo, msg)

    def _txd_terminar(self, ses, motivo: str, msg: str) -> None:
        """Último paso, siempre: si hay que cortar, bajada suave; después SIEMPRE se suelta el PTT (en una transmisión normal, solo cuando la última muestra
        ya ha sonado de verdad y ha pasado el «después»), se cierra el audio y se avisa al cliente."""
        try:
            if motivo != "acabado" and not ses.salida.terminada:
                ses.salida.parar_suave()
                limite = time.monotonic() + 0.5
                while not ses.salida.terminada and time.monotonic() < limite:
                    time.sleep(0.005)
            fin_audible = getattr(ses.salida, "t_fin_audible", None)
            if motivo == "acabado" and fin_audible is not None:
                espera = fin_audible + ses.despues - self._reloj()
                if espera > 0:
                    time.sleep(min(espera, 2.0))
            elif fin_audible is not None:
                espera = fin_audible + 0.02 - self._reloj()                # la bajada suave tiene que acabar de sonar
                if espera > 0:
                    time.sleep(min(espera, 0.2))
            if not self._txd_soltar_ptt(ses, motivo):
                motivo, msg = "error", "No se ha podido soltar el PTT: la radio no contesta"
        finally:
            try:
                ses.salida.cerrar()
            except Exception:
                log.error("No se pudo cerrar la salida de transmisión digital", exc_info=True)
            with self._lock_txd:
                if self._txd is ses:
                    self._txd = None
            if motivo not in ("acabado", "parado"):
                log.warning("Transmisión digital cortada: %s", msg or motivo)
            self._txd_avisar({"t": "txd_fin", "motivo": motivo, "msg": msg, "huecos": int(getattr(ses.salida, "huecos", 0)),
                              "retraso_ms": int(1000 * (ses.retraso + getattr(ses.salida, "retraso_s", 0.0)))})

    def _txd_avisar(self, mensaje: dict) -> None:
        with self._lock_cliente:
            sock = self._cliente
        if sock is not None:
            self._enviar(sock, mensaje)

    # ── CW ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _cw_ptt(self, on: bool) -> None:
        """El PTT del keyer de CW (por CAT, como el de la voz): se pone con el seguro de siempre y se suelta con reintentos. Si no se puede poner, el keyer lo anota y sigue."""
        if on:
            ok, texto = self._ptt(True, txd=True)
            if not ok:
                raise RuntimeError(texto)
        else:
            self._soltar_ptt_reintentando("CW")

    def _cw_otro_tx(self) -> str:
        """Por qué NO puede empezar a transmitir CW ahora («» = puede): una transmisión digital o de voz en marcha."""
        if self._txd is not None:
            return "Hay una transmisión digital en curso"
        if self._tx_audio_on or (self._ptt_on and not self._cw.ptt_propio):
            return "La radio ya está transmitiendo (voz)"
        return ""

    def _cw_radio_lista(self) -> str:
        if not self.motor.is_connected():
            return "La radio no está conectada"
        if self.motor.barriendo:
            return "Se está barriendo la banda: espera a que termine"
        return ""

    def _cw_cliente_vivo(self) -> str:
        with self._lock_cliente:
            hay = self._cliente is not None
        if not hay:
            return "el cliente se ha desconectado"
        if time.monotonic() - self._ultimo_ping > self.tx_sin_ping_s:
            return "el cliente no da señales de vida"
        return ""

    def _cw_mensaje(self, sock, m: dict) -> None:
        op = m.get("op")
        try:
            if op == "abrir":
                ok, texto = self._cw.abrir(m)
                self._enviar(sock, {"t": "cw_resp", "id": m.get("id"), "ok": ok, "msg": texto})
            elif op == "enviar":
                ok, texto, _n = self._cw.enviar(m.get("texto"), m.get("abreviar") is True)
                if not ok:
                    self._enviar(sock, {"t": "cw_error", "msg": texto, "rechazado": True})
            elif op == "wpm":
                self._cw.poner_wpm(m.get("wpm"))
            elif op == "ptt":
                self._cw.poner_ptt(m.get("on"))
            elif op == "parar":
                self._cw.parar()
            elif op == "cerrar":
                self._cw.cerrar("cerrado", avisar=False)
            else:
                self._enviar(sock, {"t": "error", "msg": "Mensaje de CW desconocido"})
        except Exception:
            log.error("Fallo en un mensaje de CW (%r)", op, exc_info=True)
            self._cw.cerrar("error")

    # ── la radio ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _bucle_radio(self) -> None:
        n = 0
        ultimo_intento = 0.0
        while not self._parar.wait(self.ciclo_s):
            try:
                if not self.motor.is_connected():
                    if time.monotonic() - ultimo_intento >= self.reconectar_radio_s:
                        ultimo_intento = time.monotonic()
                        if self._buscar_radio is None or self._aplicar_busqueda_radio():
                            log.info("Intentando conectar con la radio…")
                            self.motor.connect()
                else:
                    n += 1
                    try:
                        if self.motor.get_capabilities().get("smeter"):
                            self.motor.get_smeter()
                    except Exception:
                        pass
                    if n % 4 == 0:
                        self.motor.poll()
                    if n % 20 == 0 and self.motor.get_capabilities().get("filtros_rx"):
                        self.motor.leer_filtros_rx()
            except Exception:
                log.warning("Error en el ciclo de la radio", exc_info=True)
            self._anotar_estado_radio()
            self._difundir_estado()

    def _aplicar_busqueda_radio(self) -> bool:
        """Busca la radio y deja el motor apuntando a ella. False si no hay ninguna que se pueda manejar (no se intenta conectar)."""
        try:
            r = self._buscar_radio()
        except Exception:
            log.warning("Error buscando la radio", exc_info=True)
            r = None
        self._radio_hallada = r
        if not r or not r.get("modelo"):
            if r:
                log.warning("Hay una radio Yaesu (código %s) que este servidor todavía no maneja", r.get("id"))
            return False
        b = self.motor.backend
        if (getattr(b, "port", None), getattr(b, "baudrate", None), getattr(b, "model", None)) != (r["puerto"], r["baudios"], r["modelo"]):
            log.info("Radio encontrada: %s en %s a %s baudios", r["modelo"], r["puerto"], r["baudios"])
            b.port, b.baudrate, b.model = r["puerto"], int(r["baudios"]), r["modelo"]
            try:
                b.state.backend_name = f"Yaesu Direct ({r['puerto']})"
            except Exception:
                pass
        return True

    def _anotar_estado_radio(self) -> None:
        """Escribe (solo si cambia) cómo está la radio: conectada o no, dónde, y si es una Yaesu que aún no se maneja."""
        if self._estado_radio_fichero is None:
            return
        try:
            b = self.motor.backend
            hallada = getattr(self, "_radio_hallada", None) or {}
            est = {"conectada": bool(self.motor.is_connected()), "puerto": str(getattr(b, "port", "") or ""), "baudios": int(getattr(b, "baudrate", 0) or 0),
                   "modelo": str(getattr(b, "model", "") or ""), "id_sin_soporte": str(hallada.get("id", "")) if hallada and not hallada.get("modelo") else ""}
            firma = json.dumps(est, sort_keys=True)
            if firma == self._ultimo_estado_radio and time.monotonic() - getattr(self, "_ultima_nota_radio", 0.0) < 5.0:
                return
            self._ultimo_estado_radio = firma
            self._ultima_nota_radio = time.monotonic()
            tmp = self._estado_radio_fichero.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(est, actualizado=time.time())), encoding="utf-8")
            tmp.replace(self._estado_radio_fichero)
        except Exception:
            pass

    def _difundir_estado(self) -> None:
        with self._lock_cliente:
            sock = self._cliente
        if sock is None:
            return
        try:
            estado = P.estado_a_dict(self.motor.backend.state)
            radio = bool(self.motor.is_connected())
        except Exception:
            return
        cuerpo = {"t": "estado", "s": estado, "radio": radio}
        import json
        firma = json.dumps(cuerpo, sort_keys=True)
        ahora = time.monotonic()
        if firma != self._ultimo_estado_json or ahora - self._ultimo_envio_estado > 2.0:
            self._ultimo_estado_json = firma
            self._ultimo_envio_estado = ahora
            self._enviar(sock, cuerpo)
