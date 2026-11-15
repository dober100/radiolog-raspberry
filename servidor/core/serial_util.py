"""
core/serial_util.py -- RadioLog
Apertura de puertos serie de CAT sin pulsar las líneas de control.

pyserial abre `Serial(port=...)` con DTR y RTS en alto y solo después se pueden bajar: en ese
instante, si la radio tiene el PTT o la clave en RTS/DTR (menú de la radio), se oye el relé. Aquí se
crea el objeto SIN puerto, se fijan DTR/RTS y el control de flujo, y solo entonces se abre: el estado
inicial se aplica durante la apertura (misma técnica que usa el keyer de CW). Algunos controladores
USB suben las líneas por su cuenta al abrir; eso no lo puede evitar el programa.
"""
from __future__ import annotations


def abrir_puerto(port: str, baudrate: int, timeout: float, rtscts: bool = False,
                 dtr: bool = False, rts: bool = False, serial_mod=None):
    """Abre `port` a 8N1 con DTR/RTS en el estado pedido desde el primer instante.
    Lanza la misma excepción que `serial.Serial(...)` si no se puede abrir."""
    if serial_mod is None:
        import serial as serial_mod
    ser = serial_mod.Serial()
    ser.port = port
    ser.baudrate = int(baudrate)
    ser.bytesize = serial_mod.EIGHTBITS
    ser.parity = serial_mod.PARITY_NONE
    ser.stopbits = serial_mod.STOPBITS_ONE
    ser.timeout = timeout
    ser.xonxoff = False
    ser.rtscts = bool(rtscts)
    ser.dtr = bool(dtr)
    ser.rts = bool(rts) and not rtscts
    ser.open()
    return ser
