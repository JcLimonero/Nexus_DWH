-- ============================================================================
-- 010 — Destino (DWH) configurable: esquema, SSL/TLS, destino por empresa y
--       "Probar conexión" ejecutada por el agente
-- ============================================================================
-- * client_group: esquema destino por defecto (warehouse_schema, 'public') y
--   opciones SSL/TLS de la conexión al DWH (warehouse_sslmode, por defecto
--   'prefer' = comportamiento previo de libpq; warehouse_sslrootcert = PEM de
--   la CA para verify-ca / verify-full; no es secreto, se guarda tal cual).
-- * company: warehouse_mode 'inherit' (usa el destino del grupo; defecto y
--   valor de todas las empresas existentes) o 'custom' (destino propio con
--   los mismos campos que el grupo; host/base/usuario/contraseña cifrados
--   ENC: igual que el resto de secretos).
-- * connection_test: solicitudes de "Probar conexión" del panel. Nexus NUNCA
--   se conecta a las bases de los clientes: la prueba la toma y la ejecuta un
--   agente en línea con alcance sobre el destino/origen y devuelve el
--   resultado saneado (sin credenciales).
-- Solo agrega columnas/tablas con valores por defecto: rápida y compatible.
-- ============================================================================

ALTER TABLE client_group ADD COLUMN IF NOT EXISTS warehouse_schema     VARCHAR(63) NOT NULL DEFAULT 'public';
ALTER TABLE client_group ADD COLUMN IF NOT EXISTS warehouse_sslmode    VARCHAR(12) NOT NULL DEFAULT 'prefer';
ALTER TABLE client_group ADD COLUMN IF NOT EXISTS warehouse_sslrootcert TEXT       NOT NULL DEFAULT '';

ALTER TABLE client_group DROP CONSTRAINT IF EXISTS client_group_warehouse_schema_ck;
ALTER TABLE client_group ADD CONSTRAINT client_group_warehouse_schema_ck
    CHECK (warehouse_schema ~ '^[a-z_][a-z0-9_]{0,62}$');
ALTER TABLE client_group DROP CONSTRAINT IF EXISTS client_group_warehouse_sslmode_ck;
ALTER TABLE client_group ADD CONSTRAINT client_group_warehouse_sslmode_ck
    CHECK (warehouse_sslmode IN ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'));
ALTER TABLE client_group DROP CONSTRAINT IF EXISTS client_group_warehouse_sslrootcert_ck;
ALTER TABLE client_group ADD CONSTRAINT client_group_warehouse_sslrootcert_ck
    CHECK (length(warehouse_sslrootcert) <= 16384);

ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_mode        VARCHAR(10) NOT NULL DEFAULT 'inherit';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_host        TEXT        NOT NULL DEFAULT '';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_port        INT         NOT NULL DEFAULT 5432;
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_database    TEXT        NOT NULL DEFAULT '';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_username    TEXT        NOT NULL DEFAULT '';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_password    TEXT        NOT NULL DEFAULT '';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_schema      VARCHAR(63) NOT NULL DEFAULT 'public';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_sslmode     VARCHAR(12) NOT NULL DEFAULT 'prefer';
ALTER TABLE company ADD COLUMN IF NOT EXISTS warehouse_sslrootcert TEXT        NOT NULL DEFAULT '';

ALTER TABLE company DROP CONSTRAINT IF EXISTS company_warehouse_mode_ck;
ALTER TABLE company ADD CONSTRAINT company_warehouse_mode_ck
    CHECK (warehouse_mode IN ('inherit', 'custom'));
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_warehouse_port_ck;
ALTER TABLE company ADD CONSTRAINT company_warehouse_port_ck
    CHECK (warehouse_port BETWEEN 1 AND 65535);
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_warehouse_schema_ck;
ALTER TABLE company ADD CONSTRAINT company_warehouse_schema_ck
    CHECK (warehouse_schema ~ '^[a-z_][a-z0-9_]{0,62}$');
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_warehouse_sslmode_ck;
ALTER TABLE company ADD CONSTRAINT company_warehouse_sslmode_ck
    CHECK (warehouse_sslmode IN ('disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'));
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_warehouse_sslrootcert_ck;
ALTER TABLE company ADD CONSTRAINT company_warehouse_sslrootcert_ck
    CHECK (length(warehouse_sslrootcert) <= 16384);

-- Tareas que Nexus retiene a un agente anterior a 5.3 (destino por empresa, SSL obligatorio o DDL del
-- catálogo sin esquema): el panel lo muestra en Instalaciones ("actualice el agente").
ALTER TABLE installation ADD COLUMN IF NOT EXISTS withheld_tasks JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE installation ADD COLUMN IF NOT EXISTS withheld_at    TIMESTAMPTZ;


CREATE TABLE IF NOT EXISTS connection_test (
    id                    UUID         PRIMARY KEY,
    target_kind           VARCHAR(20)  NOT NULL
                          CHECK (target_kind IN ('group_dwh', 'company_dwh', 'company_source')),
    group_id              INT          NOT NULL REFERENCES client_group(id) ON UPDATE CASCADE ON DELETE CASCADE,
    company_id            INT          REFERENCES company(id) ON UPDATE CASCADE ON DELETE CASCADE,
    status                VARCHAR(12)  NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'running', 'ok', 'failed', 'expired', 'no_agent')),
    requested_by          VARCHAR(100) NOT NULL DEFAULT '',
    requested_by_user_id  INT          REFERENCES panel_user(id) ON DELETE SET NULL,
    installation_id       UUID         REFERENCES installation(id) ON DELETE SET NULL,
    -- Huella (sin contraseña) de la configuración probada: el panel avisa si cambió después.
    config_fingerprint    CHAR(64),
    eligible_installations INT         NOT NULL DEFAULT 0,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at            TIMESTAMPTZ,
    finished_at           TIMESTAMPTZ,
    expires_at            TIMESTAMPTZ  NOT NULL,
    result                JSONB,
    error_code            VARCHAR(64),
    message               TEXT,
    CONSTRAINT connection_test_company_ck CHECK (target_kind = 'group_dwh' OR company_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_connection_test_open
    ON connection_test (group_id, created_at) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_connection_test_group_created
    ON connection_test (group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_connection_test_company_created
    ON connection_test (company_id, created_at DESC);
