"""
gen_release_key.py — genera el par de claves Ed25519 de PUBLICACIÓN de versiones del agente.

Uso (en una máquina de confianza, idealmente fuera de línea):
  python tools/gen_release_key.py --out C:\\Custodia\\nexus_release_2026.ed25519

  * Escribe la clave PRIVADA en ``--out`` (PEM PKCS#8 cifrado con una frase de
    paso que se pide por teclado, sin eco; nunca por argumento ni variable de
    entorno para no dejarla en el historial).
  * Imprime la clave PÚBLICA (base64, 32 bytes) y su ``key_id``: eso es lo que se
    agrega a ``nexus_agent/release_keys.py`` (es pública, se versiona).

Custodia recomendada: la privada fuera del repositorio y del CI público
(HSM/token, bóveda de secretos o medio fuera de línea con respaldo), acceso
de 2 personas; rotación = nueva clave en una versión, retirar la vieja en la
siguiente. El .gitignore del repo ya excluye *.ed25519 y *.pem privados.
"""

import argparse
import base64
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.dont_write_bytecode = True

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nexus_agent.updates import key_id_for  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Genera la clave Ed25519 de publicación del agente")
    ap.add_argument("--out", required=True, help="Archivo de la clave PRIVADA (no debe existir)")
    args = ap.parse_args(argv)
    if os.path.exists(args.out):
        print(f"ERROR: {args.out} ya existe; no se sobrescribe.", file=sys.stderr)
        return 2
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.abspath(args.out).startswith(repo + os.sep):
        print("ERROR: no guarde la clave privada dentro del repositorio.", file=sys.stderr)
        return 2
    p1 = getpass.getpass("Frase de paso para cifrar la clave privada: ")
    p2 = getpass.getpass("Repita la frase de paso: ")
    if p1 != p2 or len(p1) < 12:
        print("ERROR: las frases no coinciden o tienen menos de 12 caracteres.", file=sys.stderr)
        return 2
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(p1.encode("utf-8")))
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(f"Clave privada: {args.out} (cifrada; custódiela fuera del repositorio)")
    print("Agregue a nexus_agent/release_keys.py → TRUSTED_RELEASE_KEYS:")
    print(f'    "{key_id_for(raw)}": "{base64.b64encode(raw).decode()}",')
    return 0


if __name__ == "__main__":
    sys.exit(main())
