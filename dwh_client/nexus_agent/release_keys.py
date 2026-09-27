"""
release_keys.py — claves PÚBLICAS Ed25519 de publicación de versiones del agente.

El agente instalado (y el script de actualización, que lo invoca con
``--verify-update``) solo acepta un paquete nuevo si su ``release.json`` está
firmado con una de estas claves. Quedan compiladas dentro del ejecutable
(Nuitka), así que un paquete nuevo lo valida el binario YA instalado: el ancla
de confianza es la versión anterior, no el paquete que se quiere instalar.

Son claves PÚBLICAS: pueden versionarse sin riesgo. La clave PRIVADA nunca
entra al repositorio ni al CI público; se genera y custodia fuera de línea
(ver ``dwh_client/tools/gen_release_key.py`` y DWH_README.md §21.5).

Formato: ``{key_id: clave_publica_base64}`` donde ``clave_publica_base64`` son
los 32 bytes crudos de la clave Ed25519 en base64 y ``key_id`` son los primeros
16 caracteres hexadecimales de ``sha256(clave_cruda)``
(``nexus_agent.updates.key_id_for``). Rotación: agregar la clave nueva en una
versión, publicar con la nueva y retirar la vieja en la versión siguiente.

PENDIENTE: todavía no existe la clave de publicación de producción; mientras
este diccionario esté vacío, toda actualización requiere el modo explícito
``-AllowUnsignedManifest`` (solo integridad por SHA-256, sin autenticidad).
"""

from typing import Dict

TRUSTED_RELEASE_KEYS: Dict[str, str] = {}
