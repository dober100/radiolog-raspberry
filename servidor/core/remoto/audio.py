"""
core/remoto/audio.py -- RadioLog
Audio del mando remoto. Formato en la red: PCM de 16 bits, mono, 8000 Hz (de sobra para voz de banda lateral y para FT8, que no pasa de 3 kHz),
en tramas de 40 ms (320 muestras = 640 bytes).

  CapturaRX     -- (en el servidor) lee la tarjeta de sonido de la radio y entrega tramas
  Remuestreador -- convierte entre frecuencias de muestreo (filtro + interpolación), con memoria entre bloques
  buscar()      -- elige un dispositivo por trozo de su nombre («USB AUDIO CODEC»)

Necesita numpy y sounddevice (en Raspberry Pi: sudo apt install libportaudio2 y pip install numpy sounddevice). Nada de Qt.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

log = logging.getLogger("radiolog.remoto.audio")

FS_RED = 8000
TRAMA_MS = 40
MUESTRAS_TRAMA = FS_RED * TRAMA_MS // 1000          # 320


# ── conversión de formato ──────────────────────────────────────────────────────────────────────────────────────────────────

def a_pcm16(x: np.ndarray) -> bytes:
    """Señal en coma flotante (-1..1) a PCM de 16 bits little-endian, recortando lo que se pase."""
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def de_pcm16(datos: bytes) -> np.ndarray:
    return np.frombuffer(datos, dtype="<i2").astype(np.float32) / 32768.0


# ── remuestreo ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

class Remuestreador:
    """Cambia la frecuencia de muestreo de un flujo que llega por bloques de cualquier tamaño.

    Filtro paso bajo (sinc con ventana de Hann) y después interpolación lineal; la fase y las últimas muestras se guardan entre bloques, así que el
    resultado es el mismo se parta como se parta la señal. Suficiente para voz; no pretende ser un remuestreador de calidad de estudio."""

    TAPS = 65

    def __init__(self, fs_in: float, fs_out: float):
        self.fs_in, self.fs_out = float(fs_in), float(fs_out)
        self.paso = self.fs_in / self.fs_out
        corte = 0.45 * min(self.fs_in, self.fs_out) / self.fs_in            # relativo a fs_in
        n = np.arange(self.TAPS) - (self.TAPS - 1) / 2
        h = np.sinc(2 * corte * n) * np.hanning(self.TAPS)
        self._h = (h / h.sum()).astype(np.float32)
        self._hist = np.zeros(self.TAPS - 1, dtype=np.float32)
        self._pos = 0.0                                                     # posición (en muestras filtradas) de la próxima muestra de salida
        self._filtrado_pendiente = np.zeros(0, dtype=np.float32)

    def procesar(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=np.float32).ravel()
        if self.fs_in == self.fs_out:
            return x
        ext = np.concatenate((self._hist, x))
        filtrado = np.convolve(ext, self._h, mode="valid")                  # len(x) muestras
        self._hist = ext[-(self.TAPS - 1):]
        y_all = np.concatenate((self._filtrado_pendiente, filtrado))
        # muestras de salida que se pueden interpolar con lo que hay
        if len(y_all) < 2:
            self._filtrado_pendiente = y_all
            return np.zeros(0, dtype=np.float32)
        n_sal = int(np.floor((len(y_all) - 1 - self._pos) / self.paso)) + 1
        if n_sal <= 0:
            self._filtrado_pendiente = y_all
            return np.zeros(0, dtype=np.float32)
        pos = self._pos + self.paso * np.arange(n_sal)
        i0 = np.floor(pos).astype(np.int64)
        frac = (pos - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, len(y_all) - 1)
        salida = y_all[i0] * (1.0 - frac) + y_all[i1] * frac
        siguiente = self._pos + self.paso * n_sal
        descartar = min(int(np.floor(siguiente)), len(y_all))          # (si la próxima muestra cae más allá de lo que hay, la posición sigue contando en el bloque siguiente)
        self._filtrado_pendiente = y_all[descartar:]
        self._pos = siguiente - descartar
        return salida.astype(np.float32)


# ── dispositivos ───────────────────────────────────────────────────────────────────────────────────────────────────────────

_abiertos = 0
_cerrojo_abiertos = threading.Lock()


def contar_flujo(delta: int) -> None:
    """Anota que se abre (+1) o se cierra (-1) un flujo de audio de la radio remota: mientras haya alguno, no se debe reiniciar PortAudio."""
    global _abiertos
    with _cerrojo_abiertos:
        _abiertos = max(0, _abiertos + int(delta))


def hay_flujos_abiertos() -> bool:
    with _cerrojo_abiertos:
        return _abiertos > 0


def listar(entrada: bool = True, sd=None) -> list[dict]:
    """Dispositivos de entrada (o de salida) con su índice, nombre, API de audio y frecuencia por defecto."""
    if sd is None:
        import sounddevice as sd  # noqa: F811
    clave = "max_input_channels" if entrada else "max_output_channels"
    salida = []
    for i, d in enumerate(sd.query_devices()):
        if int(d.get(clave, 0)) > 0:
            try:
                api = sd.query_hostapis(d["hostapi"])["name"]
            except Exception:
                api = ""
            salida.append({"index": i, "nombre": str(d["name"]), "api": api, "canales": int(d[clave]), "fs": float(d.get("default_samplerate", 0))})
    return salida


def buscar(nombre: Optional[str], entrada: bool = True, sd=None) -> Optional[int]:
    """Índice del dispositivo cuyo nombre contiene `nombre` (sin mirar mayúsculas). Con varias APIs de audio para la misma tarjeta (Windows) se
    prefiere la de MME/ALSA, que remuestrea sola. None si no hay ninguno; con nombre vacío, None = el dispositivo por defecto del sistema."""
    if not nombre:
        return None
    if sd is None:
        import sounddevice as sd  # noqa: F811
    def limpio(t: str) -> str:
        return " ".join(t.lower().split())                 # Windows pone a veces dos espacios seguidos («USB AUDIO  CODEC»)

    coincidencias = [d for d in listar(entrada, sd) if limpio(nombre) in limpio(d["nombre"])]
    if not coincidencias:
        return None
    preferidas = [d for d in coincidencias if d["api"] in ("MME", "ALSA")]
    return (preferidas or coincidencias)[0]["index"]


# ── captura (servidor) ─────────────────────────────────────────────────────────────────────────────────────────────────────

class CapturaRX:
    """Lee la tarjeta de sonido de la radio y llama a `entregar(bytes)` con cada trama de 40 ms (PCM16, 8000 Hz, mono)."""

    def __init__(self, entregar: Callable[[bytes], None], dispositivo: Optional[str] = None, sd=None):
        self._entregar = entregar
        self.dispositivo = dispositivo
        self._sd = sd
        self._stream = None
        self._remuestreador: Optional[Remuestreador] = None
        self._pendiente = np.zeros(0, dtype=np.float32)
        self.error = ""

    def abrir(self) -> bool:
        try:
            sd = self._sd or __import__("sounddevice")
            idx = buscar(self.dispositivo, True, sd)
            if self.dispositivo and idx is None:
                self.error = f"No encuentro ninguna tarjeta de sonido que se llame «{self.dispositivo}»"
                return False
            info = sd.query_devices(idx, "input") if idx is not None else sd.query_devices(kind="input")
            canales = 1 if int(info["max_input_channels"]) < 2 else 2
            ultimo_error = None
            for fs in (FS_RED, 16000, 48000, int(info.get("default_samplerate", 44100))):
                try:
                    self._remuestreador = None if fs == FS_RED else Remuestreador(fs, FS_RED)
                    self._stream = sd.InputStream(device=idx, channels=canales, samplerate=fs, dtype="float32",
                                                  blocksize=int(fs * TRAMA_MS / 1000), callback=self._callback)
                    self._stream.start()
                    contar_flujo(+1)
                    self._contado = True
                    log.info("Captura de audio abierta: %s a %d Hz", info.get("name", "?"), fs)
                    return True
                except Exception as e:           # la tarjeta no admite esa frecuencia: se prueba la siguiente
                    ultimo_error = e
                    self._stream = None
            self.error = f"No se puede abrir la tarjeta de sonido: {ultimo_error}"
            return False
        except Exception as e:
            self.error = f"No se puede abrir la tarjeta de sonido: {e}"
            return False

    def _callback(self, indata, frames, tiempo, estado) -> None:
        try:
            mono = indata.mean(axis=1) if indata.ndim == 2 and indata.shape[1] > 1 else indata.reshape(-1)
            if self._remuestreador is not None:
                mono = self._remuestreador.procesar(mono)
            self._pendiente = np.concatenate((self._pendiente, mono.astype(np.float32)))
            while len(self._pendiente) >= MUESTRAS_TRAMA:
                trama, self._pendiente = self._pendiente[:MUESTRAS_TRAMA], self._pendiente[MUESTRAS_TRAMA:]
                self._entregar(a_pcm16(trama))
        except Exception:
            log.exception("Error en la captura de audio")

    def cerrar(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        if getattr(self, "_contado", False):
            self._contado = False
            contar_flujo(-1)
