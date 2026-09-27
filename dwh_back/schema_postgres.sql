-- ============================================================================
-- ESQUEMA NEXUS DWH — BD de CONFIGURACIÓN (PostgreSQL, nombres en inglés)
-- ============================================================================
-- Jerarquía: client_group > company > agency > agency_task
--                            company > object_catalog  (plantillas por company)
--
-- Script IDEMPOTENTE: se puede ejecutar varias veces sobre una BD nueva o ya
-- existente. No borra tablas ni datos:
--   * CREATE TABLE / INDEX IF NOT EXISTS
--   * ALTER TABLE ... ADD COLUMN IF NOT EXISTS para columnas que se agregaron
--     después de la versión original (group_token, agency_token,
--     run_on_company_token, static_columns).
--   * Los campos sensibles se amplían a TEXT (un valor "ENC:..." cifrado con
--     Fernet es bastante más largo que el texto plano).
--
-- Uso (ejemplo):
--   psql -h HOST -U postgres -d mgd_dwh_config -f schema_postgres.sql
--
-- Reconstruido a partir de las consultas de main_postgres.py, de los campos que
-- consume dwh_client/client_postgres.py y del esquema original del repositorio.
-- Requiere PostgreSQL 11+ (probado en 16).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Función común para mantener updated_at
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ============================================================================
-- 1. CLIENT_GROUP — grupo de empresas; conexión al DWH compartida.
-- ============================================================================
CREATE TABLE IF NOT EXISTS client_group (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(255) NOT NULL UNIQUE,
    group_token         VARCHAR(128),
    warehouse_host      TEXT         NOT NULL DEFAULT '',
    warehouse_port      INT          NOT NULL DEFAULT 5432,
    warehouse_database  TEXT         NOT NULL DEFAULT '',
    warehouse_username  TEXT         NOT NULL DEFAULT '',
    warehouse_password  TEXT         NOT NULL DEFAULT '',
    is_enabled          BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Columnas agregadas en versiones posteriores (BD existentes)
ALTER TABLE client_group ADD COLUMN IF NOT EXISTS group_token VARCHAR(128);
DO $$
BEGIN
    -- Crea el índice único solo si no existe ya uno (p. ej. client_group_group_token_key).
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = 'client_group'
          AND indexdef ILIKE 'CREATE UNIQUE INDEX%(group_token)%'
    ) THEN
        CREATE UNIQUE INDEX uq_client_group_group_token ON client_group(group_token);
    END IF;
END $$;

-- Ampliar campos sensibles (varchar -> text no reescribe la tabla)
ALTER TABLE client_group ALTER COLUMN warehouse_host     TYPE TEXT;
ALTER TABLE client_group ALTER COLUMN warehouse_database TYPE TEXT;
ALTER TABLE client_group ALTER COLUMN warehouse_username TYPE TEXT;
ALTER TABLE client_group ALTER COLUMN warehouse_password TYPE TEXT;

DROP TRIGGER IF EXISTS client_group_updated_at ON client_group;
CREATE TRIGGER client_group_updated_at
    BEFORE UPDATE ON client_group
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


-- ============================================================================
-- 2. COMPANY — razón social; conexión a la BD de origen.
--    source_type: 'sqlserver' | 'mysql' | 'postgresql' | 'pervasive' | 'firebird'
-- ============================================================================
CREATE TABLE IF NOT EXISTS company (
    id               SERIAL PRIMARY KEY,
    group_id         INT          NOT NULL REFERENCES client_group(id) ON UPDATE CASCADE ON DELETE CASCADE,
    name             VARCHAR(255) NOT NULL,
    company_token    VARCHAR(128) NOT NULL UNIQUE,
    source_type      VARCHAR(20)  NOT NULL DEFAULT 'sqlserver',
    source_dsn       TEXT         NOT NULL DEFAULT '',
    source_host      TEXT         NOT NULL DEFAULT '',
    source_port      INT          NOT NULL DEFAULT 1433,
    source_database  TEXT         NOT NULL DEFAULT '',
    source_username  TEXT         NOT NULL DEFAULT '',
    source_password  TEXT         NOT NULL DEFAULT '',
    verbose_logging  BOOLEAN      NOT NULL DEFAULT FALSE,
    refresh_seconds  INT          NOT NULL DEFAULT 60,
    is_enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (group_id, name)
);

ALTER TABLE company ALTER COLUMN source_dsn      TYPE TEXT;
ALTER TABLE company ALTER COLUMN source_host     TYPE TEXT;
ALTER TABLE company ALTER COLUMN source_database TYPE TEXT;
ALTER TABLE company ALTER COLUMN source_username TYPE TEXT;
ALTER TABLE company ALTER COLUMN source_password TYPE TEXT;

DROP TRIGGER IF EXISTS company_updated_at ON company;
CREATE TRIGGER company_updated_at
    BEFORE UPDATE ON company
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


-- ============================================================================
-- 3. AGENCY — sede / agencia de una company.
-- ============================================================================
CREATE TABLE IF NOT EXISTS agency (
    id            SERIAL PRIMARY KEY,
    company_id    INT          NOT NULL REFERENCES company(id) ON UPDATE CASCADE ON DELETE CASCADE,
    name          VARCHAR(255) NOT NULL,
    agency_token  VARCHAR(128),
    is_enabled    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, name)
);

ALTER TABLE agency ADD COLUMN IF NOT EXISTS agency_token VARCHAR(128);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = 'agency'
          AND indexdef ILIKE 'CREATE UNIQUE INDEX%(agency_token)%'
    ) THEN
        CREATE UNIQUE INDEX uq_agency_agency_token ON agency(agency_token);
    END IF;
END $$;

DROP TRIGGER IF EXISTS agency_updated_at ON agency;
CREATE TRIGGER agency_updated_at
    BEFORE UPDATE ON agency
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


-- ============================================================================
-- 4. OBJECT_CATALOG — plantillas de objetos a cargar (por company).
--    upsert_keys: lista separada por comas (se convierte en PRIMARY KEY/UNIQUE).
--    create_table_sql / create_constraint_sql: DDL PostgreSQL para el DWH.
-- ============================================================================
CREATE TABLE IF NOT EXISTS object_catalog (
    id                     SERIAL PRIMARY KEY,
    company_id             INT          NOT NULL REFERENCES company(id) ON UPDATE CASCADE ON DELETE CASCADE,
    name                   VARCHAR(255) NOT NULL,
    description            TEXT,
    destination_table      VARCHAR(255) NOT NULL,
    create_table_sql       TEXT,
    upsert_keys            TEXT,
    constraint_name        VARCHAR(255),
    create_constraint_sql  TEXT,
    static_columns         TEXT,
    is_enabled             BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (company_id, name)
);

ALTER TABLE object_catalog ADD COLUMN IF NOT EXISTS static_columns TEXT;

DROP TRIGGER IF EXISTS object_catalog_updated_at ON object_catalog;
CREATE TRIGGER object_catalog_updated_at
    BEFORE UPDATE ON object_catalog
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


-- ============================================================================
-- 5. AGENCY_TASK — objeto del catálogo asignado a una agencia (tarea ETL).
--    extract_sql puede usar el marcador '{last_run}' (lo sustituye el cliente).
--    run_on_company_token: si FALSE la tarea solo sale en /agency-configs y
--    /group-configs (no en /configs).
-- ============================================================================
CREATE TABLE IF NOT EXISTS agency_task (
    id                    SERIAL PRIMARY KEY,
    agency_id             INT       NOT NULL REFERENCES agency(id) ON UPDATE CASCADE ON DELETE CASCADE,
    object_catalog_id     INT       NOT NULL REFERENCES object_catalog(id) ON UPDATE CASCADE ON DELETE CASCADE,
    extract_sql           TEXT      NOT NULL,
    schedule_seconds      INT       NOT NULL DEFAULT 3600,
    last_run_at           TIMESTAMP NULL,
    is_active             BOOLEAN   NOT NULL DEFAULT TRUE,
    run_on_company_token  BOOLEAN   NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (agency_id, object_catalog_id)
);

ALTER TABLE agency_task ADD COLUMN IF NOT EXISTS run_on_company_token BOOLEAN NOT NULL DEFAULT TRUE;

DROP TRIGGER IF EXISTS agency_task_updated_at ON agency_task;
CREATE TRIGGER agency_task_updated_at
    BEFORE UPDATE ON agency_task
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


-- ============================================================================
-- 6. ACTIVITY_LOG — auditoría HTTP del backend (middleware).
-- ============================================================================
CREATE TABLE IF NOT EXISTS activity_log (
    id            BIGSERIAL PRIMARY KEY,
    token         VARCHAR(128) NOT NULL DEFAULT '',
    company_name  VARCHAR(255) NOT NULL DEFAULT '',
    group_name    VARCHAR(255) NOT NULL DEFAULT '',
    method        VARCHAR(10)  NOT NULL,
    endpoint      VARCHAR(255) NOT NULL,
    status_code   INT          NOT NULL,
    response_ms   INT          NOT NULL DEFAULT 0,
    error_detail  TEXT,
    client_ip     VARCHAR(45)  NOT NULL DEFAULT '',
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- ============================================================================
-- 7. CLIENT_EVENTS — eventos ok/error reportados por los clientes ETL.
--    is_acknowledged es SMALLINT (0/1): el backend compara con 0 y 1.
-- ============================================================================
CREATE TABLE IF NOT EXISTS client_events (
    id               SERIAL PRIMARY KEY,
    created_at       TIMESTAMP     NOT NULL DEFAULT NOW(),
    token            VARCHAR(128)  NOT NULL DEFAULT '',
    group_name       VARCHAR(255)  NOT NULL DEFAULT '',
    company_name     VARCHAR(255)  NOT NULL DEFAULT '',
    config_id        VARCHAR(32)   NOT NULL DEFAULT '',
    task_name        VARCHAR(512)  NOT NULL DEFAULT '',
    event_type       VARCHAR(10)   NOT NULL DEFAULT 'error' CHECK (event_type IN ('ok', 'error')),
    detail           TEXT,
    rows_loaded      INT           NOT NULL DEFAULT 0,
    is_acknowledged  SMALLINT      NOT NULL DEFAULT 0
);


-- ============================================================================
-- ÍNDICES
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_company_group_id               ON company(group_id);
CREATE INDEX IF NOT EXISTS idx_agency_company_id              ON agency(company_id);
CREATE INDEX IF NOT EXISTS idx_object_catalog_company_id      ON object_catalog(company_id);
CREATE INDEX IF NOT EXISTS idx_agency_task_agency_id          ON agency_task(agency_id);
CREATE INDEX IF NOT EXISTS idx_agency_task_object_catalog_id  ON agency_task(object_catalog_id);
CREATE INDEX IF NOT EXISTS idx_actlog_token                   ON activity_log(token);
CREATE INDEX IF NOT EXISTS idx_actlog_created                 ON activity_log(created_at);
CREATE INDEX IF NOT EXISTS idx_actlog_status                  ON activity_log(status_code);
CREATE INDEX IF NOT EXISTS idx_actlog_group_name              ON activity_log(group_name);
CREATE INDEX IF NOT EXISTS idx_ce_token                       ON client_events(token);
CREATE INDEX IF NOT EXISTS idx_ce_group_name                  ON client_events(group_name);
CREATE INDEX IF NOT EXISTS idx_ce_company_name                ON client_events(company_name);
CREATE INDEX IF NOT EXISTS idx_ce_event_type                  ON client_events(event_type);
CREATE INDEX IF NOT EXISTS idx_ce_is_acknowledged             ON client_events(is_acknowledged);
CREATE INDEX IF NOT EXISTS idx_ce_created_at                  ON client_events(created_at);
