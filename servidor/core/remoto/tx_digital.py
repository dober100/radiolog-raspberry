"""
core/remoto/tx_digital.py -- RadioLog
La salida de audio del SERVIDOR (la Raspberry) para las transmisiones de modos digitales (FT8, FT4, JT65, RTTY…) del cliente.

A diferencia de la voz (que se manda en directo y suena según llega), una transmisión digital se manda POR ADELANTADO y suena por una LÍNEA DE
TIEMPO ABSOLUTA: el cliente dice a qué hora (del reloj del servidor) tiene que sonar la primera muestra y el servidor saca cada muestra en su sitio, con su
propio reloj. Así la red solo tiene que traer el audio ANTES de que haga falta; que la latencia cambie a mitad de transmisión no estropea nada, y el
instante de salida es exacto (lo que FT8 necesita).

  * El flujo de salida se abre al principio y saca silencio hasta la hora de empezar (así la tarjeta ya está «despierta» y no se come el principio).
  * Si un trozo llega tarde, ese tramo sale en silencio y el resto sigue en su sitio (se cuenta como «hueco» cuando llega); nunca se desplaza el resto.
  * Si la primera muestra llega ya pasada su hora, empieza en cuanto puede (no se recorta el principio) y se anota el retraso.
  * `parar_suave()` baja el volumen en 30 ms (un corte en seco ensancha la señal) y da el audio por terminado.
  * `t_fin_audible` es el instante (reloj del servidor) en que sonó de verdad la última muestra, contando la latencia de la tarjeta: hasta entonces no se
    suelta el PTT.

Sin Qt. Necesita numpy y sounddevice.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from core.remoto import audio as A

log = logging.getLogger("radiolog.remoto.tx_digital")

FS = A.FS_RED
RAMPA_S = 0.03
COLA_FINAL = 64                          # muestras (8 ms) de silencio que se añaden al final para que el remuestreador suelte lo último


class SalidaProgramada:
    def __init__(self, dispositivo: Optional[str] = None, sd=None, reloj=time.monotonic, latencia="high") -> None:
        self.dispositivo = dispositivo
        self._sd = sd
        self._reloj = reloj
        self._latencia_pedida = latencia
        self._stream = None
        self._fs = FS
        self._rem: Optional[A.Remuestreador] = None
        self._lock = threading.Lock()
        self._chunks: deque = deque()                 # (índice de la primera muestra en la línea de tiempo del dispositivo, audio float32)
        self._n_dev = 0                               # muestras del dispositivo recibidas en total (el final de lo que hay)
        self._sig8k = 0                               # próxima muestra (a 8 kHz) que se espera
        self._pos = 0                                 # muestras ya sacadas desde la primera
        self._t0: Optional[float] = None
        self._arrancado = False
        self._finalizado = False
        self._parando = False
        self._fade_total = 1
        self._fade_n = 0
        self._terminada = False
        self.t_inicio_real: Optional[float] = None    # cuándo sonó (o sonará) de verdad la primera muestra
        self.t_fin_audible: Optional[float] = None
        self.retraso_s = 0.0                          # cuánto se retrasó la primera muestra por llegar ya pasada su hora
        self.huecos = 0                               # veces que faltó audio que ya tocaba sacar
        self.subdesbordes = 0                         # avisos de la propia tarjeta (se quedó sin datos)
        self.error = ""
        self._contado = False
        self.ultimo_callback: Optional[float] = None   # la última vez que la tarjeta pidió audio (si deja de pedirlo, se ha atascado o la han desenchufado)

    # ── abrir / cerrar ───────────────────────────────────────────────────────────────────────────────────────────────────────

    def abrir(self) -> bool:
        try:
            sd = self._sd or __import__("sounddevice")
            idx = A.buscar(self.dispositivo, False, sd)
            if self.dispositivo and idx is None:
                self.error = f"No encuentro ninguna salida de audio que se llame «{self.dispositivo}»"
                return False
            info = sd.query_devices(idx, "output") if idx is not None else sd.query_devices(kind="output")
            ultimo = None
            for fs in (A.FS_RED, 16000, 48000, int(info.get("default_samplerate", 44100))):
                try:
                    self._rem = None if fs == A.FS_RED else A.Remuestreador(A.FS_RED, fs)
                    self._fs = fs
                    self._stream = sd.OutputStream(device=idx, channels=1, samplerate=fs, dtype="float32", callback=self._callback,
                                                   blocksize=int(fs * 0.02), latency=self._latencia_pedida)
                    self._stream.start()
                    A.contar_flujo(+1)
                    self._contado = True
                    log.info("Salida de transmisión digital abierta: %s a %d Hz", info.get("name", "?"), fs)
                    return True
                except Exception as e:
                    ultimo = e
                    self._stream = None
            self.error = f"No se puede abrir la salida de audio: {ultimo}"
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
        if self._contado:
            self._contado = False
            A.contar_flujo(-1)
        with self._lock:
            self._chunks.clear()
            self._terminada = True

    # ── la línea de tiempo ───────────────────────────────────────────────────────────────────────────────────────────────────

    def anclar(self, t0: float) -> None:
        """A qué hora (reloj del servidor) tiene que sonar la primera muestra. Solo vale la primera vez."""
        with self._lock:
            if self._t0 is None:
                self._t0 = float(t0)

    def poner(self, indice: int, x: np.ndarray) -> None:
        """Audio mono (float32, 8 kHz) cuya primera muestra es la número `indice` de la transmisión. Lo que falte por detrás se rellena con silencio."""
        x = np.asarray(x, dtype=np.float32).ravel()
        with self._lock:
            if self._finalizado or self._parando or self._terminada:
                return
            if indice > self._sig8k:
                x = np.concatenate((np.zeros(indice - self._sig8k, dtype=np.float32), x))
            elif indice < self._sig8k:
                x = x[self._sig8k - indice:]
            if not x.size:
                return
            self._añadir(x)

    def _añadir(self, x: np.ndarray, cuenta: bool = True) -> None:
        if cuenta and self._arrancado and self._n_dev < self._pos:
            self.huecos += 1                               # este trozo llegó cuando ya tocaba sonar: ese tramo salió en silencio (o recortado)
        self._sig8k += x.size
        y = self._rem.procesar(x) if self._rem is not None else x
        if y.size:
            self._chunks.append((self._n_dev, np.ascontiguousarray(y, dtype=np.float32)))
            self._n_dev += y.size

    def finalizar(self) -> None:
        """No llegará más audio: cuando se acabe de sacar, la transmisión ha terminado."""
        with self._lock:
            if self._finalizado:
                return
            self._añadir(np.zeros(COLA_FINAL, dtype=np.float32), cuenta=False)
            self._finalizado = True

    def parar_suave(self) -> None:
        """Corta lo que quede con una bajada de 30 ms."""
        with self._lock:
            if self._terminada or self._parando:
                return
            if not self._arrancado:
                self._parando = True
                self._terminada = True
                self.t_fin_audible = self._reloj()
                return
            self._parando = True
            self._fade_total = max(1, int(self._fs * RAMPA_S))
            self._fade_n = self._fade_total

    # ── consulta ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

    @property
    def abierta(self) -> bool:
        return self._stream is not None

    @property
    def latencia_s(self) -> float:
        try:
            return max(0.0, min(2.0, float(self._stream.latency))) if self._stream is not None else 0.0
        except Exception:
            return 0.0

    @property
    def terminada(self) -> bool:
        return self._terminada

    @property
    def atascada(self) -> bool:
        """¿Ha dejado la tarjeta de pedir audio (más de 0,5 s) con la transmisión en marcha? Entonces el audio no está saliendo."""
        u = self.ultimo_callback
        return bool(self._arrancado and not self._terminada and u is not None and self._reloj() - u > 0.5)

    @property
    def arrancada(self) -> bool:
        return self._arrancado

    @property
    def posicion(self) -> int:
        """Muestras (a 8 kHz) de la transmisión que ya han salido."""
        with self._lock:
            return int(self._pos * FS / self._fs) if self._arrancado else 0

    @property
    def recibido(self) -> int:
        with self._lock:
            return self._sig8k

    # ── la tarjeta pide audio ────────────────────────────────────────────────────────────────────────────────────────────────

    def _lat(self, tiempo) -> float:
        try:
            d = float(tiempo.outputBufferDacTime) - float(tiempo.currentTime)
            if 0.0 <= d < 2.0:
                return d
        except Exception:
            pass
        return self.latencia_s

    def _callback(self, outdata, frames, tiempo, estado) -> None:
        outdata[:, 0] = self._generar(frames, tiempo, estado)

    def _generar(self, n: int, tiempo, estado) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self._lock:
            if estado:
                self.subdesbordes += 1
            ahora = self._reloj()
            self.ultimo_callback = ahora
            if self._terminada or self._t0 is None:
                return out
            d = self._lat(tiempo)
            desde = 0
            if not self._arrancado:
                falta = self._t0 - (ahora + d)                 # segundos hasta que tenga que sonar la primera muestra
                if falta > 0:
                    k = int(round(falta * self._fs))
                    if k >= n:
                        return out
                    desde = k
                    self.t_inicio_real = self._t0
                else:
                    self.retraso_s = -falta
                    self.t_inicio_real = ahora + d
                self._arrancado = True
            m = n - desde
            seg, _sin_datos = self._tomar(m)
            hasta = m                                          # muestras válidas de este bloque (el resto, silencio)
            if self._finalizado and self._pos + m >= self._n_dev:
                hasta = max(0, self._n_dev - self._pos)
                seg[hasta:] = 0.0
                self._terminada = True
                self.t_fin_audible = self.t_inicio_real + self._n_dev / self._fs      # cuándo sonó la última muestra (aunque la orden de acabar llegue después)
            self._pos += m
            if self._parando and not self._terminada:
                k = min(m, self._fade_n)
                factor = np.linspace(self._fade_n / self._fade_total, (self._fade_n - k) / self._fade_total, k, dtype=np.float32) if k else np.zeros(0, dtype=np.float32)
                seg[:k] *= factor
                seg[k:] = 0.0
                self._fade_n -= k
                if self._fade_n <= 0:
                    self._terminada = True
                    self.t_fin_audible = ahora + d + (desde + k) / self._fs
            out[desde:] = seg
        return out

    def _tomar(self, m: int) -> tuple[np.ndarray, bool]:
        """Las muestras [pos, pos+m) de la línea de tiempo (silencio donde aún no ha llegado nada) y si faltó audio."""
        seg = np.zeros(m, dtype=np.float32)
        ini, fin = self._pos, self._pos + m
        while self._chunks and self._chunks[0][0] + self._chunks[0][1].size <= ini:
            self._chunks.popleft()                                      # ya sonó
        for c_ini, c in self._chunks:
            c_fin = c_ini + c.size
            if c_ini >= fin:
                break
            a, b = max(ini, c_ini), min(fin, c_fin)
            if b > a:
                seg[a - ini:b - ini] = c[a - c_ini:b - c_ini]
        return seg, fin > self._n_dev
