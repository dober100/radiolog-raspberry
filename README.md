# RadioLog · imagen de la Raspberry Pi

Imagen lista para grabar: el usuario la graba en una tarjeta SD o un SSD, la mete en la Raspberry Pi, conecta la radio por USB y enciende.
**No hay que instalar ni teclear nada.** El servidor de radio arranca solo y una página en `http://radiolog.local` dice qué escribir en RadioLog.

## Lo que hace la imagen al encender

1. Arranca el servidor de radio (servicio `radiolog-servidor`, se reinicia solo si falla).
2. Busca la radio por USB (Yaesu FTDX10, FT-991A, FT-710 por ahora) y la tarjeta de sonido de la radio.
3. La **primera vez** saca una clave de 12 caracteres (`k7mq-x4ec-9ahp`) que se guarda en esa Raspberry. La clave **no viene en la imagen**: cada Raspberry tiene la suya.
4. Se anuncia en la red como `radiolog.local` y contesta a «Buscar en mi red» de RadioLog (UDP 8766; la clave nunca viaja por ahí).
5. Sirve una página con letra grande en `http://radiolog.local` (servicio `radiolog-panel`): estado de la radio y los tres datos para RadioLog: dirección, puerto y clave.

## Para el usuario final (lo que se le explica)

1. Grabar la imagen con **Raspberry Pi Imager** (opción «Usar personalizada») en la tarjeta SD o el SSD.
2. Si usa **Wi-Fi** y no cable: abrir en el ordenador la unidad que aparece, editar `radiolog-wifi.txt` (nombre y contraseña de su Wi-Fi) y guardar. Con cable de red, no hace falta nada.
3. Meter la tarjeta en la Raspberry, conectar la radio por USB, el cable de red (si no usa Wi-Fi) y la corriente.
4. Esperar un par de minutos (el primer arranque es el más lento) y abrir `http://radiolog.local` en el ordenador o el móvil.
5. En RadioLog: **Configuración → Radio / CAT → Método «Radio remota»**, pulsar **«Buscar en mi red»** (RadioLog encuentra solo la Raspberry y rellena la dirección y el puerto) y escribir la **clave** que dice la página. Si no la encuentra, la página también da la dirección y el puerto para escribirlos a mano.

## Si la Raspberry está lejos (y se enciende a distancia)

El caso típico: la radio y la Raspberry en otro sitio, sin pantalla ni nadie, y el usuario las enciende por control remoto (por ejemplo con enchufes que se manejan desde el móvil).

1. **Una sola vez**, cuando haya alguien junto a la Raspberry (o por VPN): hacer la conexión con RadioLog (botón «Emparejar» de la página `http://radiolog.local` y «Buscar en mi red» en RadioLog). RadioLog guarda la dirección y la clave.
2. En RadioLog, en Radio / CAT, marcar **«Conectar CAT al arrancar»**. Para estar fuera de casa, la dirección tiene que ser alcanzable desde fuera (una VPN como Tailscale: se pone su dirección en vez de `radiolog.local`).
3. A partir de ahí: se enciende la Raspberry y la radio (en cualquier orden) y RadioLog **conecta solo**. Mientras tanto muestra «ESPERANDO AL SERVIDOR…» (la Raspberry aún arranca, tarda alrededor de un minuto) y luego «ESPERANDO A LA RADIO…» (el servidor ya está, falta encender la radio). No sale ninguna ventana de error, y se puede cancelar con el botón.
4. Si se va la luz o se apaga algo, vuelve a esperar y reconecta cuando regrese.

El servidor **arranca siempre**, aunque la radio esté apagada, y la va buscando por USB cada pocos segundos; así RadioLog puede distinguir «Raspberry apagada» de «radio apagada».

No hace falta instalar controladores en la Raspberry: Linux ya trae los de las Yaesu (el puerto serie y la tarjeta de sonido USB). El usuario del servicio ya está en los grupos que dan permiso para usarlos.

## Cómo se construye la imagen (para quien mantiene RadioLog)

Se construye en GitHub, gratis, sin instalar nada en el PC:

1. Subir **esta carpeta** como un repositorio de GitHub nuevo (todo su contenido, incluida la carpeta oculta `.github`).
2. En el repositorio: pestaña **Actions** → **Construir la imagen de la Raspberry Pi** → **Run workflow**.
3. A los 15-25 minutos, al final de la página de esa ejecución, en **Artifacts**, descargar `radiolog-raspberry-imagen` (el `.img.xz` y su suma de control).
4. Se descomprime con 7-Zip (o Raspberry Pi Imager lo acepta tal cual) y se graba.

Para actualizar el servidor dentro de la imagen: en el proyecto de RadioLog, `python tools/paquete_servidor_raspberry.py raspberry_repo/servidor`, subir los cambios y lanzar el flujo otra vez.

## Cosas a saber

- Es una Raspberry Pi **de 64 bits**: Pi 3, 4, 5 y Zero 2 W. No sirve para la Pi 1, 2 ni la Zero original (para esas haría falta otra imagen de 32 bits).
- La página `http://radiolog.local` enseña la clave a **cualquiera que esté en la misma red de casa**. Es lo que la hace fácil; por eso no se debe abrir el puerto 80 ni el 8765 del router a internet. Para usarlo desde fuera de casa, una VPN (Tailscale).
- Si `radiolog.local` no se encuentra en algún ordenador, la página también enseña la dirección numérica (`192.168.x.x`), que vale igual.
- Para que quien mantiene el equipo pueda entrar a mirar qué pasa: poner `ssh_clave = ...` en `radiolog.txt` (usuario `radiolog`). Sin eso, nadie puede entrar.
- Los registros del servidor están en `/var/lib/radiolog/servidor.log`.

## Lo que NO está probado todavía

Esta imagen **no se ha construido ni probado en una Raspberry real**. Lo que sí está comprobado con pruebas automáticas: la búsqueda de la radio y del audio, la clave, la lectura de los ajustes y del Wi-Fi, el arranque y la página. Lo que solo se verá al probarla:

- que el flujo de GitHub construya la imagen a la primera (si falla, el registro de esa ejecución dice dónde);
- que el primer arranque no pida crear un usuario;
- que `radiolog.local` se encuentre desde Windows;
- que el Wi-Fi se conecte con `radiolog-wifi.txt`;
- que la radio y el audio se detecten con una FTDX10 de verdad (en la FTDX10 salen dos puertos USB; se usa el primero que contesta).
