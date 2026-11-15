"""
core/remoto/conexion.py -- RadioLog
Conexión de red (en claro o TLS) que se puede LEER desde un hilo y ESCRIBIR desde varios a la vez.

Un SSLSocket no admite leer y escribir a la vez desde hilos distintos: con TLS la conexión acababa cayendo al cabo de unos segundos con
«BlockingIOError: [WinError 10035]». Aquí el socket va en modo no bloqueante y cada operación sobre él se hace con un candado; la espera (select)
se hace fuera del candado. Cada mensaje se escribe entero antes de que pase otro (otro candado), para que no se mezclen.
"""
from __future__ import annotations

import select
import socket
import ssl
import threading
import time


class Conexion:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        sock.setblocking(False)
        self._ssl = threading.RLock()            # una sola operación a la vez sobre el socket
        self._escritura = threading.Lock()       # un mensaje entero a la vez
        self.cerrada = False

    # ── lectura ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def _pendiente(self) -> bool:
        """Datos ya descifrados esperando dentro de la capa TLS (select no los vería)."""
        if isinstance(self.sock, ssl.SSLSocket):
            with self._ssl:
                try:
                    return self.sock.pending() > 0
                except (ssl.SSLError, OSError, ValueError):
                    return False
        return False

    def leer(self, espera: float = 1.0, maximo: int = 65536):
        """Lee lo que haya. Devuelve bytes; None si no llegó nada en `espera` segundos; b"" si el otro lado cerró. Lanza OSError si falla."""
        if self.cerrada:
            return b""
        if not self._pendiente():
            try:
                listos, _, _ = select.select([self.sock], [], [], espera)
            except (OSError, ValueError):
                return b"" if self.cerrada else self._error_cierre()
            if not listos:
                return None
        with self._ssl:
            if self.cerrada:
                return b""
            try:
                return self.sock.recv(maximo)
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                return None
            except ssl.SSLZeroReturnError:
                return b""

    @staticmethod
    def _error_cierre():
        raise OSError("el socket se ha cerrado")

    # ── escritura ────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def escribir(self, datos: bytes, plazo: float = 5.0) -> None:
        """Manda el mensaje entero. Lanza OSError si no se puede en `plazo` segundos o si la conexión está cerrada."""
        if self.cerrada:
            raise OSError("conexión cerrada")
        limite = time.monotonic() + plazo
        vista = memoryview(datos)
        with self._escritura:
            enviado = 0
            while enviado < len(vista):
                if self.cerrada:
                    raise OSError("conexión cerrada")
                n = 0
                with self._ssl:
                    try:
                        n = self.sock.send(vista[enviado:])
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                        n = 0
                enviado += n
                if n == 0:
                    if time.monotonic() > limite:
                        raise OSError("tiempo agotado al enviar")
                    try:
                        select.select([], [self.sock], [], 0.05)
                    except (OSError, ValueError):
                        raise OSError("conexión cerrada")

    # ── cierre ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

    def cerrar(self) -> None:
        if self.cerrada:
            return
        self.cerrada = True
        with self._ssl:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
