"""
make_manifest.py — (re)genera release.json de un paquete ya armado.

Se usa DESPUÉS de la firma Authenticode (que modifica los .exe y por tanto sus
SHA-256) y ANTES de la firma Ed25519 del manifiesto:

  python packaging/make_manifest.py --package build/dist/NexusAgent --authenticode-signed \
      --signer-subject "CN=Nexus ..." --signer-thumbprint ABCD...

Sin --authenticode-signed el manifiesto declara "SIN FIRMAR". Si existía un
release.json.sig se elimina (ya no correspondería al manifiesto nuevo).
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.dont_write_bytecode = True

from nexus_agent.updates import MANIFEST_NAME, SIGNATURE_NAME, build_manifest, manifest_bytes  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Regenera release.json")
    ap.add_argument("--package", required=True)
    ap.add_argument("--authenticode-signed", action="store_true")
    ap.add_argument("--signer-subject", default="")
    ap.add_argument("--signer-thumbprint", default="")
    ap.add_argument("--min-from-version", default="")
    args = ap.parse_args(argv)
    path = os.path.join(args.package, MANIFEST_NAME)
    with open(path, "rb") as fh:
        old = json.loads(fh.read().decode("utf-8"))
    authn = ({"signed": True, "subject": args.signer_subject, "thumbprint": args.signer_thumbprint.upper()}
             if args.authenticode_signed else {"signed": False, "note": "SIN FIRMAR"})
    manifest = build_manifest(args.package, old["version"], platform=old["platform"], build=old.get("build") or {},
                              authenticode=authn, min_from_version=args.min_from_version or old.get("min_from_version", ""),
                              executables=old.get("executables") or ["NexusAgent.exe"])
    with open(path, "wb") as fh:
        fh.write(manifest_bytes(manifest))
    sig = os.path.join(args.package, SIGNATURE_NAME)
    if os.path.exists(sig):
        os.unlink(sig)
        print("Se eliminó release.json.sig anterior (hay que volver a firmar el manifiesto).")
    print(f"release.json regenerado: {len(manifest['files'])} archivos · authenticode "
          f"{'firmado' if args.authenticode_signed else 'SIN FIRMAR'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
