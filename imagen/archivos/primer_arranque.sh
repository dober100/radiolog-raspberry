#!/bin/bash
# RadioLog · en cada arranque, antes de que haya red: aplica el Wi-Fi y los ajustes que el usuario dejó en los ficheros de texto
# de la partición de arranque (la que se ve desde Windows al grabar la tarjeta): radiolog-wifi.txt y radiolog.txt.
# Si no hay nada que aplicar, no hace nada. Nunca debe impedir el arranque: todo lleva «|| true».

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
WIFI="$BOOT/radiolog-wifi.txt"
AJUSTES="$BOOT/radiolog.txt"
ESTADO=/var/lib/radiolog-primer-arranque
mkdir -p "$ESTADO"

# valor FICHERO CLAVE -> el texto tras «clave = » (sin comillas ni espacios de los lados); las líneas que empiezan por # se saltan
valor() {
  [ -f "$1" ] || return 0
  tr -d '\r' < "$1" | awk -v k="$2" '
    /^[ \t]*#/ { next }
    { i = index($0, "="); if (i == 0) next
      key = substr($0, 1, i - 1); gsub(/^[ \t]+|[ \t]+$/, "", key)
      if (tolower(key) == k) {
        v = substr($0, i + 1); gsub(/^[ \t]+|[ \t]+$/, "", v)
        if (v ~ /^".*"$/) v = substr(v, 2, length(v) - 2)
        print v; exit } }'
}

# ── Wi-Fi ────────────────────────────────────────────────────────────────────────────────────────────────────────────
RED=$(valor "$WIFI" red)
CLAVE=$(valor "$WIFI" clave)
PAIS=$(valor "$WIFI" pais)
if [ -n "$RED" ] && [ "$RED" != "NOMBRE_DE_SU_WIFI" ]; then
  HUELLA=$(printf '%s|%s|%s' "$RED" "$CLAVE" "$PAIS" | sha256sum | cut -d' ' -f1)
  if [ "$HUELLA" != "$(cat "$ESTADO/wifi.sha" 2>/dev/null)" ]; then
    [ -n "$PAIS" ] || PAIS=ES
    raspi-config nonint do_wifi_country "$PAIS" || true
    rfkill unblock wifi || true
    nmcli connection delete radiolog-wifi >/dev/null 2>&1 || true
    if [ -n "$CLAVE" ]; then
      nmcli connection add type wifi ifname '*' con-name radiolog-wifi ssid "$RED" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$CLAVE" connection.autoconnect yes && echo "$HUELLA" > "$ESTADO/wifi.sha"
    else
      nmcli connection add type wifi ifname '*' con-name radiolog-wifi ssid "$RED" connection.autoconnect yes && echo "$HUELLA" > "$ESTADO/wifi.sha"
    fi
    nmcli connection up radiolog-wifi || true
  fi
fi

# ── Acceso por SSH (solo si el usuario —normalmente quien lo mantiene— lo pide en radiolog.txt: ssh_clave = ...) ───────
SSH_CLAVE=$(valor "$AJUSTES" ssh_clave)
if [ -n "$SSH_CLAVE" ]; then
  echo "radiolog:$SSH_CLAVE" | chpasswd || true
  systemctl enable --now ssh || true
fi
exit 0
