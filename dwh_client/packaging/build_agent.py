"""
build_agent.py — compila el agente con Nuitka (modo standalone) y arma el paquete.

Uso (desde dwh_client/, con el venv de build activo; ver build_agent.ps1 / build_agent.sh):

  python packaging/build_agent.py                 # build/dist/NexusAgent/ + release.json
  python packaging/build_agent.py --jobs 4 --allow-downloads

Qué hace:
  1. Nuitka ``--mode=standalone``: el código propio (client_postgres.py +
     nexus_agent/*) y las dependencias Python se traducen a C y se compilan
     dentro de ``NexusAgent(.exe)``; NO se distribuyen .py ni .pyc propios.
     Las extensiones binarias de terceros (psycopg2, pyodbc, cryptography…) y
     sus DLL van junto al ejecutable.
  2. Copia ``config.example.ini`` (plantilla SIN valores), los scripts de
     servicio/actualización (solo Windows) y LEEME.txt.
  3. Escribe ``release.json`` (versión, SHA-256 de cada archivo, datos del build)
     SIN firmar: la firma Authenticode (sign_release.ps1) y la firma Ed25519 del
     manifiesto (tools/sign_manifest.py) son pasos posteriores y separados.
  4. Ejecuta ``verify_package.py`` sobre el resultado (falla el build si hay
     fuentes, SQL, pruebas, configuración con valores o secretos).

Por qué standalone y no onefile: onefile se autoextrae en %TEMP% en cada
arranque (deja archivos temporales, arranque más lento, más falsos positivos de
antivirus y peor para firmar/validar archivo por archivo).
"""

import argparse
import datetime as _dt
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_DIR = os.path.dirname(HERE)
sys.path.insert(0, CLIENT_DIR)
sys.dont_write_bytecode = True

from nexus_agent import AGENT_VERSION  # noqa: E402
from nexus_agent.updates import build_manifest, manifest_bytes  # noqa: E402

PRODUCT_NAME = "Nexus DWH Agent"
COMPANY_NAME = "Nexus"
EXE_BASENAME = "NexusAgent"


def platform_tag() -> str:
    mach = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach)
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    return f"{osname}-{arch}"


def nuitka_command(out_dir: str, report: str, jobs: int, allow_downloads: bool) -> list:
    cmd = [
        sys.executable, "-m", "nuitka",
        "--mode=standalone",
        f"--output-dir={out_dir}",
        f"--output-folder-name={EXE_BASENAME}",
        f"--output-filename={EXE_BASENAME}",
        # El ejecutable no carga site-packages, variables PYTHON* ni el directorio actual.
        "--python-flag=no_site",
        "--python-flag=isolated",
        "--python-flag=safe_path",
        # Sin docstrings en el binario (no aportan en ejecución y describen el diseño interno).
        # Las demás cadenas constantes (mensajes, nombres, consultas de catálogo) SIGUEN siendo legibles.
        "--python-flag=no_docstrings",
        # Código propio y drivers (algunos se importan de forma perezosa dentro de funciones).
        "--include-package=nexus_agent",
        "--include-package=psycopg2",
        "--include-package=pymysql",
        "--include-module=pyodbc",
        "--include-module=_json",
        "--include-module=_bisect",
        "--include-package=fdb",
        "--include-module=cryptography.hazmat.primitives.asymmetric.ed25519",
        "--include-module=cryptography.hazmat.primitives.serialization",
        # Nada de pruebas ni herramientas de desarrollo en el paquete.
        "--nofollow-import-to=tests",
        "--nofollow-import-to=*.tests",
        "--nofollow-import-to=*.test",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=_pytest",
        "--nofollow-import-to=pip",
        "--nofollow-import-to=nuitka",
        "--noinclude-pytest-mode=nofollow",
        "--noinclude-setuptools-mode=nofollow",
        "--noinclude-unittest-mode=nofollow",
        "--remove-output",
        f"--report={report}",
        f"--jobs={jobs}",
    ]
    if sys.platform == "win32":
        ver4 = ".".join((AGENT_VERSION.split(".") + ["0", "0", "0"])[:4])
        cmd += [
            "--msvc=latest",
            # Consola: el mismo .exe sirve para --selftest/--version/--verify-update; como
            # servicio no se muestra ninguna ventana (sesión 0).
            "--windows-console-mode=force",
            "--include-module=win32timezone",
            "--include-module=servicemanager",
            "--include-module=win32serviceutil",
            f"--company-name={COMPANY_NAME}",
            f"--product-name={PRODUCT_NAME}",
            f"--file-version={ver4}",
            f"--product-version={ver4}",
            "--file-description=Nexus DWH Agent (ETL)",
            f"--copyright=(c) {_dt.date.today().year} {COMPANY_NAME}",
        ]
    if allow_downloads:
        cmd.append("--assume-yes-for-downloads")
    cmd.append(os.path.join(CLIENT_DIR, "client_postgres.py"))
    return cmd


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=CLIENT_DIR, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def nuitka_version() -> str:
    try:
        out = subprocess.run([sys.executable, "-m", "nuitka", "--version"], capture_output=True, text=True,
                             timeout=60).stdout
        return out.splitlines()[0].strip() if out else ""
    except Exception:  # noqa: BLE001
        return ""


def copy_extras(dist: str) -> None:
    shutil.copyfile(os.path.join(CLIENT_DIR, "config_postgres.ini.example"), os.path.join(dist, "config.example.ini"))
    shutil.copyfile(os.path.join(HERE, "LEEME.txt"), os.path.join(dist, "LEEME.txt"))
    if sys.platform == "win32":
        scripts = os.path.join(dist, "scripts")
        os.makedirs(scripts, exist_ok=True)
        for name in ("install_service.ps1", "uninstall_service.ps1", "update_agent.ps1",
                     "set_enrollment_token.ps1"):
            shutil.copyfile(os.path.join(HERE, "windows", name), os.path.join(scripts, name))


def write_manifest(dist: str, extra_build: dict) -> str:
    exe = EXE_BASENAME + (".exe" if sys.platform == "win32" else "")
    build = {
        "created_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_commit": git_commit(),
        "python": platform.python_version(),
        "compiler": "nuitka " + nuitka_version(),
        "mode": "standalone",
        **extra_build,
    }
    manifest = build_manifest(dist, AGENT_VERSION, platform=platform_tag(), build=build,
                              authenticode={"signed": False, "note": "SIN FIRMAR"}, executables=[exe])
    path = os.path.join(dist, "release.json")
    with open(path, "wb") as fh:
        fh.write(manifest_bytes(manifest))
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compila el agente Nexus con Nuitka (standalone)")
    ap.add_argument("--build-dir", default=os.path.join(CLIENT_DIR, "build"))
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--allow-downloads", action="store_true",
                    help="Permite a Nuitka descargar herramientas que falten (p. ej. en CI)")
    ap.add_argument("--skip-verify", action="store_true")
    args = ap.parse_args(argv)

    nuitka_out = os.path.join(args.build_dir, "nuitka")
    dist_root = os.path.join(args.build_dir, "dist")
    dist = os.path.join(dist_root, EXE_BASENAME)
    report = os.path.join(args.build_dir, "nuitka-report.xml")
    for d in (nuitka_out, dist):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(dist_root, exist_ok=True)

    cmd = nuitka_command(nuitka_out, report, args.jobs, args.allow_downloads)
    print("Nuitka:", " ".join(cmd[1:]), flush=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    rc = subprocess.run(cmd, cwd=CLIENT_DIR, env=env).returncode
    if rc != 0:
        print(f"ERROR: Nuitka terminó con código {rc}", file=sys.stderr)
        return rc
    candidates = [os.path.join(nuitka_out, n) for n in (EXE_BASENAME + ".dist", EXE_BASENAME, "client_postgres.dist")]
    produced = next((c for c in candidates if os.path.isdir(c)), None)
    if produced is None:
        print(f"ERROR: no se encontró la carpeta generada por Nuitka en {nuitka_out}", file=sys.stderr)
        return 1
    shutil.move(produced, dist)
    copy_extras(dist)
    manifest = write_manifest(dist, {})
    print(f"Paquete: {dist}\nManifiesto (sin firmar): {manifest}", flush=True)

    if args.skip_verify:
        return 0
    from verify_package import main as verify_main  # packaging/verify_package.py

    return verify_main([dist])


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    sys.exit(main())
