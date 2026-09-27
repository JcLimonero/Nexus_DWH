"""
sign_manifest.py — firma con Ed25519 el release.json de un paquete del agente.

Uso (en la máquina que custodia la clave privada, DESPUÉS de la firma
Authenticode y de regenerar el manifiesto con packaging/make_manifest.py):

  python tools/sign_manifest.py --package build/dist/NexusAgent --key C:\\Custodia\\nexus_release_2026.ed25519

  * Pide la frase de paso por teclado (sin eco).
  * Escribe ``release.json.sig`` junto al manifiesto: {"algorithm", "key_id", "signature"}.
  * Verifica inmediatamente la firma con la clave pública derivada y avisa si esa
    clave NO está en nexus_agent/release_keys.py (los agentes la rechazarían).

La firma cubre los bytes EXACTOS de release.json, que a su vez fija el SHA-256
de cada archivo del paquete: cualquier cambio posterior invalida el paquete.
"""

import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.dont_write_bytecode = True

from cryptography.hazmat.primitives import serialization  # noqa: E402

from nexus_agent.release_keys import TRUSTED_RELEASE_KEYS  # noqa: E402
from nexus_agent.updates import (  # noqa: E402
    MANIFEST_NAME, SIGNATURE_NAME, UpdateVerificationError, sign_manifest, verify_manifest_signature,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Firma release.json (Ed25519)")
    ap.add_argument("--package", required=True, help="Carpeta del paquete (contiene release.json)")
    ap.add_argument("--key", required=True, help="Clave privada PEM (PKCS#8, cifrada)")
    args = ap.parse_args(argv)
    manifest = os.path.join(args.package, MANIFEST_NAME)
    with open(manifest, "rb") as fh:
        data = fh.read()
    with open(args.key, "rb") as fh:
        pem = fh.read()
    password = getpass.getpass("Frase de paso de la clave de publicación: ").encode("utf-8")
    key = serialization.load_pem_private_key(pem, password=password or None)
    sig = sign_manifest(data, key)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import base64

    try:
        verify_manifest_signature(data, sig, {sig["key_id"]: base64.b64encode(raw).decode()})
    except UpdateVerificationError as exc:
        print(f"ERROR: la firma recién creada no verifica: {exc}", file=sys.stderr)
        return 1
    with open(os.path.join(args.package, SIGNATURE_NAME), "w", encoding="utf-8") as fh:
        json.dump(sig, fh, indent=2)
        fh.write("\n")
    print(f"Firmado {manifest} con key_id {sig['key_id']}.")
    if sig["key_id"] not in TRUSTED_RELEASE_KEYS:
        print("AVISO: esta clave NO está en nexus_agent/release_keys.py; los agentes instalados rechazarán el "
              "paquete hasta que una versión que la incluya esté instalada.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
