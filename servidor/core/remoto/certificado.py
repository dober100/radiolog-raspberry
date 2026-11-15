"""
core/remoto/certificado.py -- RadioLog
Certificado propio del servidor de radio para cifrar el mando remoto (TLS). Se crea solo la primera vez que arranca el servidor y se guarda en su
carpeta; el cliente NO necesita instalar nada: no se fía del certificado por sí mismo, sino de la clave compartida, que se une a la huella del
certificado en la autenticación (ver protocolo.respuesta). Así nadie puede ponerse en medio con otro certificado.

Se genera con la biblioteca `cryptography` si está instalada o, si no, con el programa `openssl` (que trae Raspberry Pi OS y Git para Windows).
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import ssl
import subprocess
from pathlib import Path

log = logging.getLogger("radiolog.remoto.certificado")

NOMBRE_CERT = "servidor-cert.pem"
NOMBRE_CLAVE = "servidor-clave.pem"


def huella(ruta_cert) -> bytes:
    """SHA-256 del certificado (en DER), la huella que une el certificado con la clave compartida."""
    der = ssl.PEM_cert_to_DER_cert(Path(ruta_cert).read_text(encoding="ascii"))
    return hashlib.sha256(der).digest()


def huella_de_der(der: bytes) -> bytes:
    return hashlib.sha256(der).digest()


def _con_cryptography(cert: Path, clave: Path) -> bool:
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:
        return False
    k = ec.generate_private_key(ec.SECP256R1())
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RadioLog servidor")])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(ahora - datetime.timedelta(days=1))
         .not_valid_after(ahora + datetime.timedelta(days=3650)).sign(k, hashes.SHA256()))
    clave.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return True


def _con_openssl(cert: Path, clave: Path) -> bool:
    exe = shutil.which("openssl")
    if not exe:
        return False
    r = subprocess.run([exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "3650",
                        "-subj", "/CN=RadioLog servidor", "-keyout", str(clave), "-out", str(cert)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.warning("openssl no pudo crear el certificado: %s", r.stderr.strip()[-300:])
        return False
    return cert.exists() and clave.exists()


def asegurar(carpeta) -> tuple[Path, Path]:
    """Devuelve (certificado, clave privada) del servidor en `carpeta`; los crea si no existen."""
    carpeta = Path(carpeta)
    cert, clave = carpeta / NOMBRE_CERT, carpeta / NOMBRE_CLAVE
    if cert.exists() and clave.exists():
        return cert, clave
    carpeta.mkdir(parents=True, exist_ok=True)
    for f in (cert, clave):
        if f.exists():
            f.unlink()
    if not (_con_cryptography(cert, clave) or _con_openssl(cert, clave)):
        raise RuntimeError("No se puede crear el certificado del servidor: instala «cryptography» (pip install cryptography) o el programa «openssl»")
    try:
        os.chmod(clave, 0o600)
    except OSError:
        pass
    log.info("Certificado del servidor creado en %s", carpeta)
    return cert, clave
