-- ============================================================================
-- 002 — Identidad por instalación (agente) y auditoría de descargas
-- ============================================================================
-- installation: cada máquina donde corre el agente ETL. Se da de alta con un
--   token de enrolamiento (grupo / empresa / agencia) vía POST /agent/enroll y
--   recibe un secreto propio. En la BD solo se guarda sha256(secreto).
--   El alcance (scope) se copia del token de enrolamiento y es lo ÚNICO que
--   decide qué tareas puede ver/reportar la instalación.
-- installation_heartbeat: historial de latidos (retención configurable).
-- agent_event: eventos genéricos del agente (p. ej. queue_overflow),
--   deduplicados por event_id.
-- task_download_log: auditoría de cada entrega de tareas/queries a un agente
--   (sin el SQL).
-- ============================================================================

CREATE TABLE IF NOT EXISTS installation (
    id                        UUID         PRIMARY KEY,
    name                      VARCHAR(255) NOT NULL,
    hostname                  VARCHAR(255) NOT NULL DEFAULT '',
    os_info                   VARCHAR(255) NOT NULL DEFAULT '',
    fingerprint               JSONB        NOT NULL DEFAULT '{}'::jsonb,
    scope_type                VARCHAR(10)  NOT NULL CHECK (scope_type IN ('group', 'company', 'agency')),
    group_id                  INT          NOT NULL REFERENCES client_group(id) ON UPDATE CASCADE ON DELETE CASCADE,
    company_id                INT          REFERENCES company(id) ON UPDATE CASCADE ON DELETE CASCADE,
    agency_id                 INT          REFERENCES agency(id)  ON UPDATE CASCADE ON DELETE CASCADE,
    status                    VARCHAR(10)  NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    credential_hash           CHAR(64)     NOT NULL,
    previous_credential_hash  CHAR(64),
    previous_valid_until      TIMESTAMPTZ,
    credential_rotated_at     TIMESTAMPTZ,
    rotation_required         BOOLEAN      NOT NULL DEFAULT FALSE,
    enrolled_via              VARCHAR(10)  NOT NULL CHECK (enrolled_via IN ('group', 'company', 'agency')),
    enrollment_token_prefix   VARCHAR(8)   NOT NULL DEFAULT '',
    client_version            VARCHAR(50)  NOT NULL DEFAULT '',
    last_seen_at              TIMESTAMPTZ,
    last_ip                   VARCHAR(45)  NOT NULL DEFAULT '',
    last_agent_seq            BIGINT       NOT NULL DEFAULT 0,
    last_heartbeat            JSONB,
    revoked_at                TIMESTAMPTZ,
    revoked_reason            VARCHAR(255),
    created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT installation_scope_ck CHECK (
        (scope_type = 'group'   AND company_id IS NULL AND agency_id IS NULL) OR
        (scope_type = 'company' AND company_id IS NOT NULL AND agency_id IS NULL) OR
        (scope_type = 'agency'  AND company_id IS NOT NULL AND agency_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_installation_group   ON installation(group_id);
CREATE INDEX IF NOT EXISTS idx_installation_company ON installation(company_id);
CREATE INDEX IF NOT EXISTS idx_installation_agency  ON installation(agency_id);
CREATE INDEX IF NOT EXISTS idx_installation_status  ON installation(status);

DROP TRIGGER IF EXISTS installation_updated_at ON installation;
CREATE TRIGGER installation_updated_at
    BEFORE UPDATE ON installation
    FOR EACH ROW EXECUTE PROCEDURE update_updated_at();


CREATE TABLE IF NOT EXISTS installation_heartbeat (
    id               BIGSERIAL    PRIMARY KEY,
    installation_id  UUID         NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    event_time       TIMESTAMPTZ,
    agent_seq        BIGINT,
    client_version   VARCHAR(50)  NOT NULL DEFAULT '',
    uptime_seconds   BIGINT,
    queue_depth      INT,
    running          JSONB        NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_hb_installation_received ON installation_heartbeat(installation_id, received_at);


CREATE TABLE IF NOT EXISTS agent_event (
    event_id         UUID         PRIMARY KEY,
    installation_id  UUID         NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    event_type       VARCHAR(40)  NOT NULL,
    agent_seq        BIGINT,
    event_time       TIMESTAMPTZ,
    received_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    payload          JSONB        NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_agent_event_inst ON agent_event(installation_id, received_at);


CREATE TABLE IF NOT EXISTS task_download_log (
    id               BIGSERIAL    PRIMARY KEY,
    installation_id  UUID         NOT NULL REFERENCES installation(id) ON DELETE CASCADE,
    task_id          INT          NOT NULL,
    query_version    INT          NOT NULL,
    downloaded_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    client_ip        VARCHAR(45)  NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tdl_inst_time ON task_download_log(installation_id, downloaded_at);
CREATE INDEX IF NOT EXISTS idx_tdl_task      ON task_download_log(task_id);
