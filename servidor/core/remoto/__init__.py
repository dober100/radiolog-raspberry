"""
core/remoto -- RadioLog
Mando de una radio a distancia: un programa servidor junto a la radio (puede ser una Raspberry Pi) y RadioLog como cliente.

  protocolo.py  -- mensajes, autenticación por clave compartida y lista de órdenes permitidas
  servidor.py   -- el servidor: tiene el CAT de la radio y atiende al cliente (sin Qt: funciona también en Linux)
  cliente.py    -- el «backend» CAT de RadioLog que habla con ese servidor

Etapa 1 (CAT por red local o VPN): frecuencia, modo, VFO, teclas y medidor S. El audio y la cascada vienen en etapas posteriores
(ver claude/REMOTO.md). Nada de este paquete usa PyQt.
"""
