"""
core/remoto/reproductor.py -- RadioLog
Reproduce en el altavoz del cliente el audio de recepción que llega del servidor (PCM16, 8000 Hz, mono).

Lleva un pequeño colchón de audio (`retardo_ms`): hasta que no hay esa cantidad no empieza a sonar, y si la red se retrasa y el colchón se vacía,
vuelve a llenarlo en vez de entrecortar. Si por el contrario se acumula demasiado (la red manda a ráfagas), tira lo más antiguo para que el audio
no se vaya quedando atrás. Qt no interviene.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Optional

import numpy as np

from core.remoto import audio as A

log = logging.getLogger("radiolog.remoto.reproductor")


class ReproductorAudio:
    def __init__(self, dispositivo: Optional[str] = None, retardo_ms: int = 200, max_ms: int = 600, sd=None):
        self.dispositivo = dispositivo
        self.retardo_ms = int(retardo_ms)
        self.max_ms = int(max_ms)
        self._sd = sd
        self._stream = None
        self._fs = A.FS_RED
        self._remuestreador: Optional[A.Remuestreador] = None
        self._cola: deque[np.ndarray] = deque()
        self._muestras_en_cola = 0
        self._lock = threading.Lock()
        self._sonando = False
        self.volumen = 1.0                      # 0 = nada, 1 = tal cual, hasta 3
        self.silenciado = False
        self._nivel = 0.0
        self.error = ""
        self.huecos = 0                         # veces que el colchón se vació (audio entrecortado)
        self.descartadas = 0                    # muestras tiradas por acumulación

    # ── abrir / cerrar ───────────────────────────────────────────────────────────────────────────────────────────────────────

    def abrir(self) -> bool:
        try:
            sd = self._sd or __import__("sounddevice")
            idx = A.buscar(self.dispositivo, False, sd)
            if self.dispositivo and idx is None:
                self.error = f"No encuentro ninguna salida de audio que se llame «{self.dispositivo}»"
                return False
            info = sd.query_devices(idx, "output") if idx is not None else sd.query_devices(kind="output")
            ultimo_error = None
            for fs in (A.FS_RED, 16000, 48000, int(info.get("default_samplerate", 44100))):
                try:
                    self._remuestreador = None if fs == A.FS_RED else A.Remuestreador(A.FS_RED, fs)
                    self._fs = fs
                    self._stream = sd.OutputStream(device=idx, channels=1, samplerate=fs, dtype="float32", callback=self._callback,
                                                   blocksize=int(fs * 0.02))
                    self._stream.start()
                    log.info("Salida de audio abierta: %s a %d Hz", info.get("name", "?"), fs)
                    return True
                except Exception as e:
                    ultimo_error = e
                    self._stream = None
            self.error = f"No se puede abrir la salida de audio: {ultimo_error}"
            return False
        except Exception as e:
            self.error = f"No se puede abrir la salida de audio: {e}"
            return False

    def cerrar(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        with self._lock:
            self._cola.clear()
            self._muestras_en_cola = 0
            self._sonando = False

    # ── entrada de audio de la red ───────────────────────────────────────────────────────────────────────────────────────────

    def alimentar(self, pcm16: bytes) -> None:
        x = A.de_pcm16(pcm16)
        if self._remuestreador is not None:
            x = self._remuestreador.procesar(x)
        if not len(x):
            return
        limite = int(self._fs * self.max_ms / 1000)
        with self._lock:
            self._cola.append(x)
            self._muestras_en_cola += len(x)
            while self._muestras_en_cola > limite and len(self._cola) > 1:        # demasiado atrás: se tira lo más antiguo
                viejo = self._cola.popleft()
                self._muestras_en_cola -= len(viejo)
                self.descartadas += len(viejo)

    # ── salida al altavoz ────────────────────────────────────────────────────────────────────────────────────────────────────

    def _callback(self, outdata, frames, tiempo, estado) -> None:
        salida = self._sacar(frames)
        outdata[:, 0] = salida

    def _sacar(self, n: int) -> np.ndarray:
        """Las próximas `n` muestras para el altavoz (con volumen aplicado); ceros si aún se está llenando el colchón."""
        minimo = int(self._fs * self.retardo_ms / 1000)
        with self._lock:
            if not self._sonando:
                if self._muestras_en_cola >= minimo:
                    self._sonando = True
                else:
                    return np.zeros(n, dtype=np.float32)
            partes, falta = [], n
            while falta > 0 and self._cola:
                bloque = self._cola[0]
                if len(bloque) <= falta:
                    partes.append(bloque)
                    falta -= len(bloque)
                    self._muestras_en_cola -= len(bloque)
                    self._cola.popleft()
                else:
                    partes.append(bloque[:falta])
                    self._cola[0] = bloque[falta:]
                    self._muestras_en_cola -= falta
                    falta = 0
            if falta > 0:                                                    # se vació el colchón: pausa y a llenarlo otra vez
                partes.append(np.zeros(falta, dtype=np.float32))
                self._sonando = False
                self.huecos += 1
        salida = np.concatenate(partes) if partes else np.zeros(n, dtype=np.float32)
        self._nivel = float(np.sqrt(np.mean(salida ** 2))) if len(salida) else 0.0
        if self.silenciado:
            return np.zeros(n, dtype=np.float32)
        return np.clip(salida * float(self.volumen), -1.0, 1.0).astype(np.float32)

    def nivel(self) -> float:
        """Volumen reciente de lo que suena (0..1), para un indicador."""
        return min(1.0, self._nivel * 4.0)
