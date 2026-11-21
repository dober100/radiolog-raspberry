"""
core/remoto/tailscale.py -- RadioLog
Lo mínimo para llevarse bien con Tailscale, la VPN gratuita que se usa para llegar a una radio remota que está lejos. RadioLog NO gestiona la VPN: Tailscale es un programa
aparte, ya iniciado en el equipo con la cuenta del usuario. Aquí solo se le pregunta (con su propio comando `tailscale status --json`, que no pide ninguna contraseña) qué
equipos hay en su red privada, para poder buscar entre ellos el servidor de radio. Sin Qt.
"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from typing import Callable, Optional

_RUTAS_WINDOWS = (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def es_direccion_tailscale(ip: str) -> bool:
    """¿Es una dirección de la red de Tailscale (100.64.0.0/10)?"""
    try:
        return ipaddress.ip_address(str(ip)) in _CGNAT
    except ValueError:
        return False


def encontrar_programa() -> Optional[str]:
    """Ruta del comando `tailscale` si está instalado, o None."""
    en_path = shutil.which("tailscale")
    if en_path:
        return en_path
    if sys.platform == "win32":
        for r in _RUTAS_WINDOWS:
            if os.path.isfile(r):
                return r
    return None


def _ejecutar(programa: str, timeout: float = 4.0) -> str:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)          # sin ventana negra en Windows
    r = subprocess.run([programa, "status", "--json"], capture_output=True, text=True, timeout=timeout, creationflags=flags)
    return r.stdout or ""


def equipos(ejecutar: Optional[Callable[[str], str]] = None, programa: Optional[str] = None) -> list[dict]:
    """Equipos de la red privada de Tailscale que están conectados: [{nombre, ip, es_este}] (solo direcciones IPv4). Vacío si Tailscale no está instalado, no ha
    iniciado sesión o no hay otros equipos. Nunca lanza excepciones."""
    try:
        programa = programa or encontrar_programa()
        if not programa:
            return []
        datos = json.loads((ejecutar or _ejecutar)(programa))
        if not isinstance(datos, dict) or datos.get("BackendState") != "Running":
            return []
        res: list[dict] = []
        yo = datos.get("Self") or {}
        for es_este, nodo in [(True, yo)] + [(False, p) for p in (datos.get("Peer") or {}).values()]:
            if not isinstance(nodo, dict) or (not es_este and not nodo.get("Online")):
                continue
            ips = [i for i in (nodo.get("TailscaleIPs") or []) if es_direccion_tailscale(i)]
            if ips:
                res.append({"nombre": str(nodo.get("HostName") or nodo.get("DNSName") or ips[0]), "ip": ips[0], "es_este": es_este})
        return res
    except Exception:
        return []


def direcciones_para_buscar(**kw) -> list[str]:
    """Las direcciones de los OTROS equipos conectados de la red privada (a esos se les pregunta «¿hay servidor de radio?»)."""
    return [e["ip"] for e in equipos(**kw) if not e["es_este"]]


# ── la Raspberry: ¿está unida a la VPN? y empezar a unirla ─────────────────────────────────────────────────────────────────

def estado_propio(ejecutar: Optional[Callable[[str], str]] = None, programa: Optional[str] = None) -> dict:
    """Cómo está ESTE equipo respecto a Tailscale: {instalado, estado, ip, nombre, nombre_vpn, url_login, caduca}. `nombre_vpn` = cómo se llama en la lista de equipos de la
    cuenta (si ya había otro «radiolog», Tailscale le pone «radiolog-1»; `nombre` es el del sistema y no cambia).
    `estado` es el de Tailscale («Running» = unida y conectada, «NeedsLogin» = falta iniciar sesión, «Stopped»…); `url_login` es el enlace para autorizarla con la cuenta
    (aparece mientras se está uniendo); `caduca` es la fecha en que la clave de ESTE equipo caduca ('' si la caducidad está desactivada: lo que se quiere en una estación lejana)."""
    base = {"instalado": False, "estado": "", "ip": "", "nombre": "", "nombre_vpn": "", "url_login": "", "caduca": ""}
    try:
        programa = programa or encontrar_programa()
        if not programa:
            return base
        base["instalado"] = True
        datos = json.loads((ejecutar or _ejecutar)(programa))
        if not isinstance(datos, dict):
            return base
        yo = datos.get("Self") or {}
        base["nombre_vpn"] = str(yo.get("DNSName") or "").split(".")[0]
        ips = [i for i in (yo.get("TailscaleIPs") or []) if es_direccion_tailscale(i)]
        caduca = str(yo.get("KeyExpiry") or "")
        if caduca.startswith("0001-"):                           # «fecha cero» de Go = sin caducidad
            caduca = ""
        base.update(estado=str(datos.get("BackendState") or ""), ip=ips[0] if ips else "", nombre=str(yo.get("HostName") or ""),
                    url_login=str(datos.get("AuthURL") or ""), caduca=caduca)
    except Exception:
        pass
    return base


def estado_red(ejecutar: Optional[Callable[[str], str]] = None, programa: Optional[str] = None) -> dict:
    """Foto de Tailscale en ESTE equipo para el asistente de VPN: {instalado, estado, url_login, nombre, pares}.
    `pares` = los otros equipos de la cuenta: [{nombre, ip, en_linea, caduca}] (`caduca` = fecha de caducidad de su clave, '' si no caduca). Nunca lanza excepciones."""
    base = {"instalado": False, "estado": "", "url_login": "", "nombre": "", "pares": []}
    try:
        programa = programa or encontrar_programa()
        if not programa:
            return base
        base["instalado"] = True
        datos = json.loads((ejecutar or _ejecutar)(programa))
        if not isinstance(datos, dict):
            return base
        base.update(estado=str(datos.get("BackendState") or ""), url_login=str(datos.get("AuthURL") or ""),
                    nombre=str((datos.get("Self") or {}).get("HostName") or ""))
        for nodo in (datos.get("Peer") or {}).values():
            if not isinstance(nodo, dict):
                continue
            ips = [i for i in (nodo.get("TailscaleIPs") or []) if es_direccion_tailscale(i)]
            caduca = str(nodo.get("KeyExpiry") or "")
            if caduca.startswith("0001-"):
                caduca = ""
            if nodo.get("Expired") and not caduca:
                caduca = "caducada"
            base["pares"].append({"nombre": str(nodo.get("HostName") or nodo.get("DNSName") or ""), "ip": ips[0] if ips else "",
                                  "en_linea": bool(nodo.get("Online")), "caduca": caduca})
    except Exception:
        pass
    return base


_proceso_union = None


def iniciar_union(nombre: str = "radiolog", programa: Optional[str] = None, fichero_salida: Optional[str] = None) -> bool:
    """Empieza a unir esta Raspberry a la cuenta de Tailscale: lanza `tailscale up` (sigue esperando en segundo plano a que alguien autorice el enlace). El enlace sale en
    estado_propio()['url_login']. Necesita que el usuario del servicio sea el «operador» de Tailscale (lo deja hecho el primer arranque). True si se lanzó."""
    global _proceso_union
    try:
        programa = programa or encontrar_programa()
        if not programa:
            return False
        if _proceso_union is not None and _proceso_union.poll() is None:        # ya se está uniendo
            return True
        salida = open(fichero_salida, "w", encoding="utf-8") if fichero_salida else subprocess.DEVNULL
        _proceso_union = subprocess.Popen([programa, "up", "--hostname", nombre], stdout=salida, stderr=subprocess.STDOUT, start_new_session=True)
        return True
    except Exception:
        return False
