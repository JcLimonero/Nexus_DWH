#!/bin/sh
# Entrada del contenedor del backend (DWH_README.md §23).
# NEXUS_RUN_MIGRATIONS=true -> aplica migrate.py (línea base + migraciones pendientes, con advisory
# lock) antes de arrancar; si falla, el contenedor NO arranca. Defecto false: migraciones a mano
# (python migrate.py desde la terminal del contenedor).
set -eu

case "$(printf '%s' "${NEXUS_RUN_MIGRATIONS:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    echo "NEXUS_RUN_MIGRATIONS: aplicando migraciones..."
    python migrate.py
    ;;
esac

exec "$@"
