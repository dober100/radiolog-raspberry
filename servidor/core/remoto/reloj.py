"""
core/remoto/reloj.py -- RadioLog
Sincronía de relojes entre el cliente y el servidor (la Raspberry), para que una transmisión digital (FT8, FT4…) salga a la hora exacta aunque la
red tenga latencia y la latencia varíe.

Cada «ping» lleva la hora (monótona) del cliente y el «pong» devuelve, además, la hora (monótona) del servidor. Con eso se calcula la diferencia entre
los dos relojes: `desfase = hora_servidor − hora_cliente_en_el_medio_del_viaje`. El error de cada medida es como mucho la mitad del ida-y-vuelta, así que
se queda con la de MENOR ida-y-vuelta de las últimas (la más fiable: la que menos esperó en cola). Sin Qt.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional

VENTANA = 24                   # medidas recientes entre las que se elige la mejor (≈ 24 s con un ping por segundo)


class EstimadorReloj:
    def __init__(self, ventana: int = VENTANA) -> None:
        self._medidas: deque = deque(maxlen=int(ventana))      # (ida_y_vuelta_s, desfase_s)
        self._lock = threading.Lock()

    def medir(self, t_envio: float, t_recibido: float, t_servidor: float) -> None:
        """Una respuesta: el cliente envió a `t_envio`, la recibió a `t_recibido` (los dos con su reloj) y el servidor la contestó a `t_servidor` (con el suyo)."""
        rtt = t_recibido - t_envio
        if rtt < 0 or rtt > 30.0:
            return
        with self._lock:
            self._medidas.append((rtt, t_servidor - (t_envio + rtt / 2.0)))

    @property
    def listo(self) -> bool:
        with self._lock:
            return bool(self._medidas)

    @property
    def desfase(self) -> Optional[float]:
        """Hora del servidor menos hora del cliente (s), o None si aún no hay ninguna medida."""
        with self._lock:
            if not self._medidas:
                return None
            return min(self._medidas)[1]

    @property
    def incertidumbre_s(self) -> Optional[float]:
        """Lo máximo que puede equivocarse `desfase` (mitad del mejor ida-y-vuelta)."""
        with self._lock:
            if not self._medidas:
                return None
            return min(self._medidas)[0] / 2.0

    def a_servidor(self, t_cliente: float) -> Optional[float]:
        d = self.desfase
        return None if d is None else t_cliente + d

    def a_cliente(self, t_servidor: float) -> Optional[float]:
        d = self.desfase
        return None if d is None else t_servidor - d

    def reiniciar(self) -> None:
        with self._lock:
            self._medidas.clear()
