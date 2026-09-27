-- ============================================================================
-- 007 — Inventario estructural y cambios de estructura ("Dar por entendido")
-- ============================================================================
-- monitored_database: una fila por BASE MONITOREADA con identidad ESTABLE
--   (identity_key = sha256(tipo | motor | host:puerto/base normalizados), la
--   calcula el backend a partir de la configuración y el agente la verifica).
--   Un DWH compartido por varias agencias/instalaciones es UNA sola fila: el
--   inventario lo hace UNA instalación a la vez (lease con vencimiento).
--   engine_identity (sha256 de system_identifier + oid/nombre de la base que
--   reporta el agente) detecta la misma base configurada con otro nombre de
--   host (duplicate_of_id) y cambios de servidor detrás de la misma dirección.
--   El origen (DMS) solo se monitorea si un administrador lo crea y habilita
--   explícitamente (kind = 'source', enabled = FALSE por defecto).
-- monitored_database_link: grupos/empresas que referencian la base (filtros
--   y aislamiento por grupo para la fase de RBAC).
-- inventory_snapshot: cada intento de inventario (completo / parcial / no
--   confiable), sin objetos (el detalle vive en inventory_object_state).
-- inventory_object_state: último estado OBSERVADO de cada objeto
--   (base, esquema, nombre, tipo) con su estructura normalizada y huella.
-- inventory_baseline: referencia APROBADA por objeto (+ historial en
--   inventory_baseline_version). Nunca se aprueba sola.
-- structural_change: alerta por objeto (agregado / eliminado / modificado)
--   con el detalle granular, anterior vs actual, primera detección, última
--   observación, estado (pending/acknowledged/superseded/reverted), atribución
--   MANUAL (client/nexus) separada de la evidencia técnica y row version
--   (concurrencia optimista). Una sola pendiente por objeto.
-- structural_change_event / monitored_database_event: historial.
-- task_execution.ddl_applied: marcas NO sensibles (objeto + acción + columnas,
--   sin SQL) del DDL que aplicó el agente Nexus (evidencia, no autoría).
-- Las definiciones SQL de vistas solo se guardan si la base lo habilita
--   (view_definitions_enabled) y siempre CIFRADAS (ENC: Fernet); si el backend
--   no tiene config_secret_key solo se guarda el hash.
-- Todas las fechas en UTC (timestamptz).
-- ============================================================================

CREATE TABLE IF NOT EXISTS monitored_database (
    id                        SERIAL       PRIMARY KEY,
    kind                      VARCHAR(10)  NOT NULL CHECK (kind IN ('dwh', 'source')),
    engine                    VARCHAR(20)  NOT NULL DEFAULT 'postgresql',
    identity_key              CHAR(64)     NOT NULL UNIQUE,
    display_name              VARCHAR(255) NOT NULL DEFAULT '',
    group_id                  INT          REFERENCES client_group(id) ON DELETE SET NULL,
    company_id                INT          REFERENCES company(id) ON DELETE SET NULL,
    enabled                   BOOLEAN      NOT NULL DEFAULT TRUE,
    scan_interval_seconds     INT          NOT NULL DEFAULT 3600 CHECK (scan_interval_seconds BETWEEN 60 AND 2592000),
    schema_include            TEXT[]       NOT NULL DEFAULT '{}',
    schema_exclude            TEXT[]       NOT NULL DEFAULT '{}',
    view_definitions_enabled  BOOLEAN      NOT NULL DEFAULT FALSE,
    engine_identity           CHAR(64),
    engine_identity_strength  VARCHAR(10),
    duplicate_of_id           INT          REFERENCES monitored_database(id) ON DELETE SET NULL,
    lease_installation_id     UUID         REFERENCES installation(id) ON DELETE SET NULL,
    lease_acquired_at         TIMESTAMPTZ,
    lease_until               TIMESTAMPTZ,
    scan_requested_at         TIMESTAMPTZ,
    -- awaiting_first_snapshot → baseline_pending (propuesta) → monitoring
    state                     VARCHAR(30)  NOT NULL DEFAULT 'awaiting_first_snapshot'
                                  CHECK (state IN ('awaiting_first_snapshot', 'baseline_pending', 'monitoring')),
    -- never | verified | partial | unverifiable
    verification_status       VARCHAR(20)  NOT NULL DEFAULT 'never'
                                  CHECK (verification_status IN ('never', 'verified', 'partial', 'unverifiable')),
    last_attempt_at           TIMESTAMPTZ,
    last_verified_at          TIMESTAMPTZ,
    last_verified_snapshot_id BIGINT,
    last_snapshot_id          BIGINT,
    last_reason_code          VARCHAR(64),
    baseline_version          INT          NOT NULL DEFAULT 0,
    baseline_approved_at      TIMESTAMPTZ,
    baseline_approved_by      VARCHAR(100),
    created_by                VARCHAR(100) NOT NULL DEFAULT 'system',
    created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_monitored_database_group   ON monitored_database(group_id);
CREATE INDEX IF NOT EXISTS idx_monitored_database_company ON monitored_database(company_id);
CREATE INDEX IF NOT EXISTS idx_monitored_database_engine  ON monitored_database(kind, engine_identity);


CREATE TABLE IF NOT EXISTS monitored_database_link (
    monitored_database_id  INT         NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    group_id               INT         NOT NULL,
    company_id             INT         NOT NULL DEFAULT 0,   -- 0 = todo el grupo (DWH)
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (monitored_database_id, group_id, company_id)
);
CREATE INDEX IF NOT EXISTS idx_mdb_link_group ON monitored_database_link(group_id, company_id);


CREATE TABLE IF NOT EXISTS monitored_database_event (
    id                     BIGSERIAL    PRIMARY KEY,
    monitored_database_id  INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    event_type             VARCHAR(40)  NOT NULL,
    actor                  VARCHAR(100) NOT NULL DEFAULT 'system',
    message                VARCHAR(1000),
    data                   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mdb_event ON monitored_database_event(monitored_database_id, id DESC);


CREATE TABLE IF NOT EXISTS inventory_snapshot (
    id                     BIGSERIAL    PRIMARY KEY,
    snapshot_uuid          UUID         NOT NULL UNIQUE,     -- idempotencia (lo genera el agente)
    monitored_database_id  INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    installation_id        UUID         REFERENCES installation(id) ON DELETE SET NULL,
    captured_at            TIMESTAMPTZ,                      -- reloj del agente (UTC)
    received_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status                 VARCHAR(12)  NOT NULL CHECK (status IN ('complete', 'partial', 'unreliable')),
    reason_code            VARCHAR(64),
    object_count           INT          NOT NULL DEFAULT 0,
    snapshot_fingerprint   CHAR(64),
    schemas_verified       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    schemas_unverifiable   JSONB        NOT NULL DEFAULT '[]'::jsonb,
    agent_version          VARCHAR(50)  NOT NULL DEFAULT '',
    server_version         VARCHAR(50)  NOT NULL DEFAULT '',
    engine_identity        CHAR(64),
    processing             JSONB        NOT NULL DEFAULT '{}'::jsonb   -- resumen del diff (conteos)
);
CREATE INDEX IF NOT EXISTS idx_inventory_snapshot_mdb ON inventory_snapshot(monitored_database_id, id DESC);


CREATE TABLE IF NOT EXISTS inventory_object_state (
    monitored_database_id  INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    schema_name            VARCHAR(128) NOT NULL,
    object_name            VARCHAR(128) NOT NULL,
    object_type            VARCHAR(20)  NOT NULL CHECK (object_type IN ('table', 'view', 'matview', 'foreign_table')),
    fingerprint            CHAR(64)     NOT NULL,
    structure              JSONB        NOT NULL,
    definition_hash        CHAR(64),
    definition_enc         TEXT,                 -- ENC:... solo con view_definitions_enabled
    present                BOOLEAN      NOT NULL DEFAULT TRUE,
    first_seen_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    missing_since          TIMESTAMPTZ,
    last_snapshot_id       BIGINT,
    PRIMARY KEY (monitored_database_id, schema_name, object_name, object_type)
);


CREATE TABLE IF NOT EXISTS inventory_baseline (
    monitored_database_id  INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    schema_name            VARCHAR(128) NOT NULL,
    object_name            VARCHAR(128) NOT NULL,
    object_type            VARCHAR(20)  NOT NULL,
    fingerprint            CHAR(64)     NOT NULL,
    structure              JSONB        NOT NULL,
    definition_hash        CHAR(64),
    definition_enc         TEXT,
    version                INT          NOT NULL DEFAULT 1,
    origin                 VARCHAR(20)  NOT NULL CHECK (origin IN ('initial_approval', 'acknowledged_change')),
    change_id              BIGINT,
    approved_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    approved_by            VARCHAR(100) NOT NULL,
    PRIMARY KEY (monitored_database_id, schema_name, object_name, object_type)
);


CREATE TABLE IF NOT EXISTS inventory_baseline_version (
    id                     BIGSERIAL    PRIMARY KEY,
    monitored_database_id  INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    baseline_version       INT          NOT NULL,       -- versión global de la línea base de la BD
    schema_name            VARCHAR(128) NOT NULL,
    object_name            VARCHAR(128) NOT NULL,
    object_type            VARCHAR(20)  NOT NULL,
    action                 VARCHAR(20)  NOT NULL CHECK (action IN ('approved', 'acknowledged', 'removed', 'reset')),
    fingerprint            CHAR(64),
    structure              JSONB,
    definition_hash        CHAR(64),
    change_id              BIGINT,
    actor                  VARCHAR(100) NOT NULL,
    comment                VARCHAR(500),
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_baseline_version_mdb ON inventory_baseline_version(monitored_database_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_baseline_version_obj
    ON inventory_baseline_version(monitored_database_id, schema_name, object_name, object_type, id DESC);


CREATE TABLE IF NOT EXISTS structural_change (
    id                       BIGSERIAL    PRIMARY KEY,
    monitored_database_id    INT          NOT NULL REFERENCES monitored_database(id) ON DELETE CASCADE,
    schema_name              VARCHAR(128) NOT NULL,
    object_name              VARCHAR(128) NOT NULL,
    object_type              VARCHAR(20)  NOT NULL,
    change_kind              VARCHAR(20)  NOT NULL CHECK (change_kind IN ('object_added', 'object_removed', 'object_modified')),
    change_types             TEXT[]       NOT NULL DEFAULT '{}',     -- granular (column_added, index_changed…)
    diffs                    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    baseline_fingerprint     CHAR(64),                               -- anterior (NULL si el objeto es nuevo)
    observed_fingerprint     VARCHAR(64)  NOT NULL,                  -- actual ('absent' si se eliminó)
    baseline_version         INT          NOT NULL DEFAULT 0,        -- versión de la línea base comparada
    previous_structure       JSONB,
    current_structure        JSONB,
    previous_definition_hash CHAR(64),
    current_definition_hash  CHAR(64),
    previous_definition_enc  TEXT,
    current_definition_enc   TEXT,
    first_detected_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_observed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    observation_count        INT          NOT NULL DEFAULT 1,
    first_snapshot_id        BIGINT,
    last_snapshot_id         BIGINT,
    status                   VARCHAR(15)  NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'acknowledged', 'superseded', 'reverted')),
    status_changed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    supersedes_id            BIGINT       REFERENCES structural_change(id) ON DELETE SET NULL,
    superseded_by_id         BIGINT       REFERENCES structural_change(id) ON DELETE SET NULL,
    -- Atribución MANUAL (no es prueba de autoría). Separada de la evidencia.
    attribution              VARCHAR(10)  CHECK (attribution IN ('client', 'nexus')),
    ack_by                   VARCHAR(100),
    ack_at                   TIMESTAMPTZ,
    ack_comment              VARCHAR(1000),
    ticket_ref               VARCHAR(100),
    reclassified_at          TIMESTAMPTZ,
    reclassified_by          VARCHAR(100),
    -- Evidencia técnica (ejecuciones Nexus con DDL, coincidencia con catálogo).
    evidence                 JSONB        NOT NULL DEFAULT '[]'::jsonb,
    row_version              INT          NOT NULL DEFAULT 1,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT structural_change_ack_ck CHECK (
        status <> 'acknowledged' OR (attribution IS NOT NULL AND ack_at IS NOT NULL AND ack_by IS NOT NULL))
);
-- Una sola alerta PENDIENTE por objeto (base, esquema, nombre, tipo).
CREATE UNIQUE INDEX IF NOT EXISTS ux_structural_change_pending
    ON structural_change(monitored_database_id, schema_name, object_name, object_type) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_structural_change_status ON structural_change(status, first_detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_structural_change_mdb    ON structural_change(monitored_database_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_structural_change_obj
    ON structural_change(monitored_database_id, schema_name, object_name, object_type, id DESC);


CREATE TABLE IF NOT EXISTS structural_change_event (
    id          BIGSERIAL    PRIMARY KEY,
    change_id   BIGINT       NOT NULL REFERENCES structural_change(id) ON DELETE CASCADE,
    event_type  VARCHAR(30)  NOT NULL,
    actor       VARCHAR(100) NOT NULL DEFAULT 'system',
    message     VARCHAR(1000),
    data        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_structural_change_event ON structural_change_event(change_id, id);


-- Evidencia del DDL que aplicó el agente Nexus en una ejecución (sin SQL).
ALTER TABLE task_execution ADD COLUMN IF NOT EXISTS ddl_applied JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS idx_task_execution_ddl
    ON task_execution(finished_at DESC) WHERE ddl_applied <> '[]'::jsonb;
