#!/bin/bash
# RadioLog · se ejecuta DENTRO de la imagen de Raspberry Pi OS Lite (la lanza el flujo de GitHub, en un entorno que emula la Raspberry).
# Deja instalado todo lo que el servidor necesita y los servicios activados, para que al encender la Raspberry arranque solo.
set -euxo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

# 1. Programas que necesita el servidor (Python y las bibliotecas de serie, audio y cifrado, y el nombre «radiolog.local» en la red)
apt-get update
apt-get install -y --no-install-recommends python3 python3-serial python3-numpy python3-sounddevice libportaudio2 openssl avahi-daemon alsa-utils

# 2. Un usuario sin contraseña para el servicio (nadie entra con él; si hace falta, se activa con ssh_clave en radiolog.txt)
id radiolog >/dev/null 2>&1 || useradd -m -s /bin/bash -G dialout,audio,plugdev radiolog
passwd -l radiolog
command -v cancel-rename >/dev/null 2>&1 && cancel-rename radiolog || true    # que el primer arranque no pida crear un usuario

# 3. El servidor y sus servicios
install -d /opt/radiolog
cp -a "$REPO/servidor/." /opt/radiolog/
install -m 755 "$REPO/imagen/archivos/primer_arranque.sh" /opt/radiolog/primer_arranque.sh
cp "$REPO"/imagen/archivos/*.service /etc/systemd/system/
systemctl enable radiolog-servidor.service radiolog-panel.service radiolog-primer-arranque.service

# 4. Nombre en la red: radiolog.local (el equipo se llamaba raspberrypi) y hora de España
echo radiolog > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tradiolog/' /etc/hosts; else printf '127.0.1.1\tradiolog\n' >> /etc/hosts; fi
ln -sf /usr/share/zoneinfo/Europe/Madrid /etc/localtime
echo Europe/Madrid > /etc/timezone

# 5. Los dos ficheros de texto que el usuario puede editar desde Windows (están en la partición de arranque)
if mountpoint -q /boot/firmware; then BOOT=/boot/firmware
elif mountpoint -q /boot; then BOOT=/boot
else echo "ERROR: no encuentro la partición de arranque montada; los ficheros radiolog.txt y radiolog-wifi.txt no quedarían a la vista" >&2; exit 1
fi
cp "$REPO/imagen/archivos/radiolog.txt" "$REPO/imagen/archivos/radiolog-wifi.txt" "$BOOT/"

# 6. Limpieza para que la imagen pese menos
apt-get clean
rm -rf /var/lib/apt/lists/*
echo "Imagen de RadioLog preparada."
