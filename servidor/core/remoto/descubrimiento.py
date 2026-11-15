"""
core/remoto/descubrimiento.py -- RadioLog
Encontrar el servidor de radio en la red de casa sin escribir su dirección: RadioLog pregunta «¿hay algún servidor de radio por aquí?» por UDP (a toda la red
local) y cada servidor contesta quién es, su puerto y qué radio tiene. La CLAVE no viaja nunca por aquí: solo sirve para saber dónde está el servidor.

  petición:   b"RADIOLOG-BUSCA 1"
  respuesta:  {"radiolog": 1, "id": "<único de cada servidor>", "nombre": "radiolog", "puerto": 8765, "radio": "Yaesu FTDX10", "cifrado": true, "emparejando": false}

El servidor solo contesta a equipos de redes privadas (casa, Tailscale) o, si se ha limitado con --redes-permitidas, a esas redes. Sin Qt.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import uuid
from typing import Iterable, Optional

logger = logging.getLogger("radiolog.remoto.descubrimiento")

PUERTO_DESCUBRIMIENTO = 8766
PETICION = b"RADIOLOG-BUSCA 1"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")           # la red de Tailscale


def _origen_permitido(ip: str, redes: Optional[list]) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if redes:
        for r in redes:
            try:
                if a in ipaddress.ip_network(str(r).strip(), strict=False):
                    return True
            except ValueError:
                continue
        return False
    return a.is_private or a.is_loopback or a.is_link_local or a in _CGNAT


class Respondedor:
    """Escucha las preguntas «¿hay servidor?» y contesta. Un hilo; se para con stop()."""

    def __init__(self, puerto_tcp: int, nombre: Optional[str] = None, radio: str = "", cifrado: bool = True,
                 puerto_udp: int = PUERTO_DESCUBRIMIENTO, host: str = "0.0.0.0", redes_permitidas: Optional[list] = None, emparejando=None):
        self.puerto_tcp, self.radio, self.cifrado = int(puerto_tcp), radio, bool(cifrado)
        self.nombre = nombre or socket.gethostname()
        self.host, self.puerto_udp, self.redes = host, int(puerto_udp), [r for r in (redes_permitidas or []) if str(r).strip()]
        self._sock: Optional[socket.socket] = None
        self._hilo: Optional[threading.Thread] = None
        self._parar = threading.Event()
        self.contestadas = 0
        self._emparejando = emparejando                    # función sin argumentos: ¿está abierto el emparejamiento? (para avisar a RadioLog)
        self.id = uuid.uuid4().hex[:12]                    # para no contar dos veces el mismo servidor si se le oye por dos tarjetas de red

    def respuesta(self) -> bytes:
        return json.dumps({"radiolog": 1, "id": self.id, "nombre": self.nombre, "puerto": self.puerto_tcp, "radio": self.radio, "cifrado": self.cifrado,
                           "emparejando": self._esta_emparejando()}, ensure_ascii=False).encode("utf-8")

    def _esta_emparejando(self) -> bool:
        try:
            return bool(self._emparejando()) if self._emparejando else False
        except Exception:
            return False

    def iniciar(self) -> bool:
        """True si queda escuchando. Si el puerto está ocupado (otro servidor en el mismo equipo) no pasa nada: solo no se anuncia."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.puerto_udp))
            s.settimeout(0.5)
        except OSError as exc:
            logger.warning("No se puede anunciar el servidor en la red (UDP %s): %s", self.puerto_udp, exc)
            return False
        self._sock = s
        self.puerto_udp = s.getsockname()[1]
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="radiolog-descubrimiento", daemon=True)
        self._hilo.start()
        return True

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:
                datos, origen = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if datos.strip() != PETICION or not _origen_permitido(origen[0], self.redes):
                continue
            try:
                self._sock.sendto(self.respuesta(), origen)
                self.contestadas += 1
            except OSError:
                pass

    def detener(self) -> None:
        self._parar.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        if self._hilo is not None:
            self._hilo.join(timeout=2.0)


def _ips_locales() -> list[str]:
    ips: list[str] = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(sa[0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                 # UDP: no manda nada, solo elige la interfaz de salida
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    vistas, salida = set(), []
    for ip in ips:
        if ip not in vistas and not ip.startswith(("127.", "169.254.")):
            vistas.add(ip)
            salida.append(ip)
    return salida


def destinos_de_difusion() -> list[str]:
    """A dónde preguntar: la difusión general y la de la red /24 de cada tarjeta de red del equipo."""
    d = ["255.255.255.255"]
    for ip in _ips_locales():
        base = ip.rsplit(".", 1)[0] + ".255"
        if base not in d:
            d.append(base)
    return d


def buscar_servidores(espera: float = 1.5, puerto: int = PUERTO_DESCUBRIMIENTO, destinos: Optional[Iterable[str]] = None) -> list[dict]:
    """Pregunta en la red y recoge las respuestas durante `espera` segundos. Devuelve [{host, puerto, nombre, radio, cifrado}], un elemento por servidor
    (si un servidor contesta por varias direcciones, se queda con una)."""
    destinos = list(destinos) if destinos is not None else destinos_de_difusion()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    encontrados: dict[str, dict] = {}
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        for d in destinos:
            try:
                s.sendto(PETICION, (d, puerto))
            except OSError:
                continue
        fin = time.monotonic() + espera
        while True:
            resto = fin - time.monotonic()
            if resto <= 0:
                break
            s.settimeout(resto)
            try:
                datos, origen = s.recvfrom(2048)
            except (socket.timeout, OSError):
                break
            try:
                d = json.loads(datos.decode("utf-8"))
                if not isinstance(d, dict) or d.get("radiolog") != 1:
                    continue
                p = int(d["puerto"])
            except (ValueError, KeyError, TypeError):
                continue
            clave = str(d.get("id") or f"{origen[0]}:{p}")
            nuevo = {"host": origen[0], "puerto": p, "nombre": str(d.get("nombre", "")), "radio": str(d.get("radio", "")), "cifrado": bool(d.get("cifrado", True)),
                     "emparejando": bool(d.get("emparejando", False))}
            if clave not in encontrados or origen[0] < encontrados[clave]["host"]:      # mismo servidor por otra dirección: se queda la menor (resultado estable)
                encontrados[clave] = nuevo
    finally:
        s.close()
    return sorted(encontrados.values(), key=lambda x: (x["host"], x["puerto"]))
