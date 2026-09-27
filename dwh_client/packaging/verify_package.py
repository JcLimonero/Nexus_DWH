"""
verify_package.py — revisa que un paquete del agente no lleve lo que no debe.

Uso:
  python packaging/verify_package.py build/dist/NexusAgent [--json] [--allow-third-party-py]

Falla (código 1) si encuentra:
  * código fuente o bytecode PROPIO (client_postgres, nexus_agent, client.py…)
    — y, por defecto, cualquier .py/.pyc/.pyo o carpeta __pycache__;
  * texto de nuestro código fuente embebido en binarios ("canarios": líneas de
    código que solo existen en las fuentes, no en las constantes compiladas) o
    docstrings propios (el build compila sin docstrings);
  * archivos .sql, carpetas o archivos de pruebas;
  * config.ini u otro .ini que no sea la plantilla ``config.example.ini``, o una
    plantilla con valores en claves sensibles (token, group_token, agency_token,
    password, secret…);
  * datos locales del agente (agent_data, *.dpapi, agent_credential*,
    agent_state.db*, enrollment_token*), logs, volcados de memoria (*.dmp),
    .env, llaves privadas (PEM "PRIVATE KEY", *.pfx, *.p12, *.key);
  * si hay release.json: archivos no declarados o con SHA-256 distinto.

Informa (sin fallar): binarios de terceros (.pyd/.so/.dll/.dylib), tamaño total,
y si el manifiesto está firmado.
"""

import argparse
import configparser
import fnmatch
import hashlib
import json
import os
import re
import sys
from typing import Dict, List

OWN_PACKAGES = {"nexus_agent"}
OWN_MODULES = {"client_postgres", "client", "client_last", "main_postgres", "main", "build_agent", "verify_package"}
# Líneas que solo existen en las FUENTES (no son constantes de cadena): si aparecen en un
# binario, el paquete lleva código fuente incrustado.
SOURCE_CANARIES = (
    b"def ensure_credential(self, force_enroll",
    b"class StopCoordinator:",
    b"def verify_release(package_dir: str, *, installed_version",
    b"self.store.save(cred)",
)
# Fragmentos de DOCSTRINGS propios: el build usa --python-flag=no_docstrings, así que no deben aparecer
# (las demás cadenas constantes —mensajes, nombres— sí quedan en el binario; es esperado).
DOCSTRING_CANARIES = (
    b"Coordina la parada pedida por el SCM",
    b"Rutas relativas (con '/') de todos los archivos del paquete",
    b"Ruta relativa segura en Windows y POSIX",
)
FORBIDDEN_PATTERNS = (
    "*.sql", "config.ini", "*.dpapi", "agent_credential*", "agent_state.db*", "enrollment_token*",
    "*.log", "*.dmp", "*.mdmp", ".env", ".env.*", "*.pfx", "*.p12", "*.key", "*.ed25519", "*.sqlite",
    "*.db", "conftest.py", "pytest.ini",
)
FORBIDDEN_DIRS = {"tests", "test", "testing", "agent_data", "__pycache__", "logs", ".git"}
SENSITIVE_KEYS = re.compile(r"(token|password|passwd|pwd|secret|api_key|private)", re.I)
TEXT_EXT = {".ini", ".txt", ".json", ".ps1", ".md", ".cfg", ".xml", ".pem", ".crt", ".yaml", ".yml", ".toml"}
BINARY_EXT = {".exe", ".dll", ".pyd", ".so", ".dylib", ".bin"}


def _is_binary_name(name: str) -> bool:
    low = name.lower()
    return os.path.splitext(low)[1] in BINARY_EXT or ".so." in low or "." not in low


def check_config_template(path: str, errors: List[str]) -> None:
    ini = configparser.ConfigParser()
    try:
        ini.read(path, encoding="utf-8")
    except configparser.Error as exc:
        errors.append(f"config.example.ini ilegible: {exc}")
        return
    for sec in ini.sections():
        for key, val in ini.items(sec, raw=True):
            if SENSITIVE_KEYS.search(key) and val.strip():
                errors.append(f"config.example.ini tiene valor en [{sec}] {key}")


def scan(dist: str, allow_third_party_py: bool = False) -> Dict[str, object]:
    errors: List[str] = []
    warnings: List[str] = []
    binaries: List[str] = []
    total = 0
    if not os.path.isdir(dist):
        return {"ok": False, "errors": [f"No existe la carpeta {dist}"], "warnings": [], "binaries": [], "files": 0,
                "bytes": 0}
    files = 0
    for root, dirs, names in os.walk(dist):
        rel_root = os.path.relpath(root, dist)
        for d in list(dirs):
            if d.lower() in FORBIDDEN_DIRS:
                errors.append(f"Carpeta no permitida: {os.path.join(rel_root, d)}")
        for name in names:
            files += 1
            full = os.path.join(root, name)
            rel = os.path.normpath(os.path.join(rel_root, name)).replace(os.sep, "/")
            size = os.path.getsize(full)
            total += size
            low = name.lower()
            ext = os.path.splitext(low)[1]
            if ext in (".py", ".pyc", ".pyo", ".pyw"):
                parts = rel.split("/")
                own = parts[0] in OWN_PACKAGES or os.path.splitext(parts[-1])[0] in OWN_MODULES
                if own or not allow_third_party_py:
                    errors.append(f"{'Fuente/bytecode PROPIO' if own else 'Fuente/bytecode'}: {rel}")
                else:
                    warnings.append(f"Fuente/bytecode de terceros: {rel}")
                continue
            if low.startswith("test_") or low.endswith("_test.py"):
                errors.append(f"Archivo de pruebas: {rel}")
            for pat in FORBIDDEN_PATTERNS:
                if fnmatch.fnmatch(low, pat):
                    errors.append(f"Archivo no permitido ({pat}): {rel}")
                    break
            if ext == ".ini" and low != "config.example.ini":
                errors.append(f"Archivo .ini no permitido (solo config.example.ini): {rel}")
            if low == "config.example.ini":
                check_config_template(full, errors)
            if ext in TEXT_EXT and size <= 8 * 1024 * 1024:
                with open(full, "rb") as fh:
                    data = fh.read()
                if b"PRIVATE KEY-----" in data:
                    errors.append(f"Contiene una llave privada PEM: {rel}")
            if _is_binary_name(name):
                binaries.append(rel)
                with open(full, "rb") as fh:
                    blob = fh.read()
                for canary in SOURCE_CANARIES:
                    if canary in blob:
                        errors.append(f"Texto de código fuente propio embebido en {rel}: {canary[:40]!r}")
                        break
                for canary in DOCSTRING_CANARIES:
                    if canary in blob:
                        errors.append(f"Docstring propio embebido en {rel} (falta no_docstrings): {canary[:40]!r}")
                        break
    manifest = os.path.join(dist, "release.json")
    signed = os.path.exists(os.path.join(dist, "release.json.sig"))
    if os.path.exists(manifest):
        errors.extend(check_manifest(dist, manifest))
    else:
        warnings.append("Sin release.json (manifiesto de publicación).")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "binaries": sorted(binaries),
            "files": files, "bytes": total, "manifest_signed": signed}


def check_manifest(dist: str, path: str) -> List[str]:
    errs: List[str] = []
    try:
        with open(path, "rb") as fh:
            m = json.loads(fh.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"release.json ilegible: {type(exc).__name__}"]
    declared = {}
    for f in m.get("files") or []:
        declared[f.get("path")] = f
    present = set()
    for root, _d, names in os.walk(dist):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), dist).replace(os.sep, "/")
            if rel in ("release.json", "release.json.sig"):
                continue
            present.add(rel)
            entry = declared.get(rel)
            if entry is None:
                errs.append(f"Archivo no declarado en release.json: {rel}")
                continue
            h = hashlib.sha256()
            with open(os.path.join(root, name), "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != entry.get("sha256"):
                errs.append(f"SHA-256 distinto al de release.json: {rel}")
    for missing in sorted(set(declared) - present):
        errs.append(f"release.json declara un archivo inexistente: {missing}")
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica el contenido de un paquete del agente")
    ap.add_argument("dist")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-third-party-py", action="store_true",
                    help="Tratar .py/.pyc de terceros como aviso (los propios siempre son error)")
    args = ap.parse_args(argv)
    res = scan(args.dist, args.allow_third_party_py)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"Paquete: {args.dist}")
        print(f"Archivos: {res['files']} · {res['bytes'] / 1048576:.1f} MB · binarios: {len(res['binaries'])} · "
              f"manifiesto firmado: {'sí' if res.get('manifest_signed') else 'no'}")
        for w in res["warnings"]:
            print(f"AVISO  {w}")
        for e in res["errors"]:
            print(f"ERROR  {e}")
        print("Resultado: " + ("OK" if res["ok"] else f"{len(res['errors'])} problema(s)"))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
