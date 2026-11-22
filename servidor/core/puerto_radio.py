"""
core/puerto_radio.py -- RadioLog
El puerto serie de la SALIDA de la radio (en la FTDX10, el «Standard COM Port»), compartido por toda la suite.

Un puerto serie solo lo puede abrir una vez el mismo programa, y la clave del CW (una línea, p. ej. DTR) y el PTT
(otra línea, p. ej. RTS) viven en ese mismo puerto. Aquí hay UN manejador compartido con cuenta de usuarios:
  * `adquirir(puerto)` abre el puerto la primera vez (con las líneas SUELTAS desde el primer instante, para que no suene
    el relé ni se pulse nada al abrir) y, las siguientes veces, solo suma un usuario;
  * `liberar()` resta un usuario y, al irse el último, baja las dos líneas y cierra el puerto;
  * `poner(linea, valor)` mueve una línea («rts» o «dtr») sin tocar la otra.
Así el CW Keyer y el PTT de RTTY pueden estar «conectados» a la vez sin pelearse por el puerto, y ningún Studio tiene que
desconectarse para que otro transmita.

Los errores de apertura se propagan tal cual (PermissionError si el puerto lo tiene otro programa, etc.): quien llama
decide cómo contarlos. Lógica pura, con `serial` inyectable (las pruebas usan uno falso) y seguro entre hilos.
"""
from __future__ import annotations

import atexit
import logging
import threading

logger = logging.getLogger(__name__)

LINEAS = ("rts", "dtr")


class PuertoRadio:
    def __init__(self, serie=None) -> None:
        self._lock = threading.RLock()
        self._serie = serie              # módulo tipo pyserial; None = el de verdad (se importa al abrir)
        self._ser = None
        self._puerto = ""
        self._usuarios = 0

    # -- estado ---------------------------------------------------------------------------
    @property
    def abierto(self) -> bool:
        return self._ser is not None

    @property
    def puerto(self) -> str:
        return self._puerto

    @property
    def usuarios(self) -> int:
        return self._usuarios

    @property
    def ser(self):
        """El objeto serie abierto (para leer CTS/DSR de las palas) o None."""
        return self._ser

    # -- uso ------------------------------------------------------------------------------
    def adquirir(self, puerto: str, reposo: dict | None = None) -> None:
        """Pide el puerto. Lo abre si no estaba abierto; si ya lo estaba (otro módulo), solo suma un usuario.
        `reposo` = nivel inicial de las líneas al abrir (por defecto las dos sueltas); solo vale la primera vez.
        Lanza la excepción de pyserial si no se puede abrir, y OSError si ya está abierto con OTRO puerto."""
        puerto = (puerto or "").strip()
        if not puerto:
            raise OSError("no hay puerto elegido")
        with self._lock:
            if self._ser is not None:
                if puerto.upper() != self._puerto.upper():
                    raise OSError(f"el puerto de la radio ya está abierto en {self._puerto} (no se puede usar también {puerto})")
                self._usuarios += 1
                return
            mod = self._serie
            if mod is None:
                import serial as mod                 # noqa: PLC0415 (al abrir, no al importar el módulo)
            reposo = reposo or {}
            if "://" in puerto:                      # p. ej. «loop://» (pruebas sin hardware)
                ser = mod.serial_for_url(puerto, do_not_open=True)
            else:
                ser = mod.Serial()
                ser.port = puerto
            # Las dos líneas en su estado de reposo ANTES de abrir: así el puerto no pulsa la clave ni el PTT al abrirse
            # (el estado inicial se aplica durante la apertura).
            ser.dtr = bool(reposo.get("dtr", False))
            ser.rts = bool(reposo.get("rts", False))
            ser.dsrdtr = False
            ser.rtscts = False
            ser.open()
            self._ser, self._puerto, self._usuarios = ser, puerto, 1

    def liberar(self) -> None:
        """Un usuario menos; al irse el último se sueltan las líneas y se cierra el puerto. No lanza nunca."""
        with self._lock:
            if self._usuarios > 0:
                self._usuarios -= 1
            if self._usuarios == 0:
                self._cerrar()

    def poner(self, linea: str, valor: bool) -> None:
        """Mueve una línea («rts» o «dtr»). Si el puerto no está abierto no hace nada; si el puerto falla, lo propaga."""
        if linea not in LINEAS:
            raise ValueError(f"línea desconocida: {linea}")
        with self._lock:
            if self._ser is not None:
                setattr(self._ser, linea, bool(valor))

    def valor(self, linea: str) -> bool:
        with self._lock:
            return bool(getattr(self._ser, linea, False)) if self._ser is not None else False

    def cerrar_todo(self) -> None:
        """Cierra el puerto pase lo que pase, aunque haya usuarios (al salir del programa)."""
        with self._lock:
            self._usuarios = 0
            self._cerrar()

    def _cerrar(self) -> None:
        ser, self._ser, self._puerto = self._ser, None, ""
        if ser is None:
            return
        for linea in LINEAS:
            try:
                setattr(ser, linea, False)           # las dos líneas sueltas antes de cerrar
            except Exception:
                logger.exception("Puerto de la radio: fallo al soltar %s", linea)
        try:
            ser.close()
        except Exception:
            logger.exception("Puerto de la radio: fallo al cerrar")


_compartido = PuertoRadio()


def compartido() -> PuertoRadio:
    """El manejador único de la aplicación."""
    return _compartido


atexit.register(lambda: _compartido.cerrar_todo())
