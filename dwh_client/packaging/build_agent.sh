#!/usr/bin/env bash
# Compila el agente con Nuitka en macOS/Linux. SOLO como prueba de humo de la configuración de
# Nuitka (el binario de producción es Windows x64: build_agent.ps1 o el job de CI).
# Requisitos: Python 3.12 y un compilador C (clang/gcc).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3.12}"
VENV="build/venv-build"
export PYTHONDONTWRITEBYTECODE=1
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check -q -r requirements_build.txt
"$VENV/bin/python" packaging/build_agent.py "$@"
DIST="build/dist/NexusAgent"
"$DIST/NexusAgent" --version
"$DIST/NexusAgent" --selftest
echo "Listo: $DIST (no distribuir: la plataforma soportada es Windows x64)"
