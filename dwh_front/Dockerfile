# Panel Nexus DWH (Next.js 14, salida standalone) — imagen para Coolify/Docker (DWH_README.md §23).
# Contexto de construcción: dwh_front/ (Coolify: base directory /dwh_front, build pack Dockerfile).
# DWH_API_URL, DWH_PANEL_PROXY_KEY, DWH_PUBLIC_ORIGIN, DWH_CLIENT_IP_HEADER, ... son SOLO de
# servidor y se leen en tiempo de ejecución: se definen en el contenedor, nunca al construir.

ARG NODE_IMAGE=node:22-alpine
ARG PNPM_VERSION=9.12.0

# ── Dependencias ───────────────────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS deps
ARG PNPM_VERSION
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@${PNPM_VERSION} --activate
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# ── Compilación ────────────────────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS builder
ARG PNPM_VERSION
# Única variable pública (se incrusta en el JS del navegador; no es secreta): zona horaria de
# visualización. Vacía = America/Mexico_City.
ARG NEXT_PUBLIC_DWH_TIMEZONE=""
ENV NEXT_PUBLIC_DWH_TIMEZONE=${NEXT_PUBLIC_DWH_TIMEZONE} \
    NEXT_TELEMETRY_DISABLED=1 \
    NODE_ENV=production
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@${PNPM_VERSION} --activate
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

# ── Ejecución ──────────────────────────────────────────────────────────────────────────────────
FROM ${NODE_IMAGE} AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
WORKDIR /app
RUN addgroup -S -g 10001 nexus && adduser -S -u 10001 -G nexus -H -h /app nexus
# Solo el servidor autónomo y los estáticos (sin node_modules de desarrollo ni código fuente).
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
# El servidor escribe la caché de Next (ISR/imágenes) en .next/cache.
RUN mkdir -p .next/cache && chown -R nexus:nexus .next/cache
USER nexus
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/login',{redirect:'manual'}).then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))"]
CMD ["node", "server.js"]
