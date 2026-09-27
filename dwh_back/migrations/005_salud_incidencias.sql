-- ============================================================================
-- 005 — Salud, incidencias y notificaciones
-- ============================================================================
-- agency_task.expected_duration_seconds / delay_tolerance_seconds: opcionales
--   por tarea. Si son NULL se usan los valores de [health] del backend
--   (duración esperada = p90 de las últimas ejecuciones exitosas o el defecto).
-- task_health_state: estado que mantiene el evaluador de salud por tarea
--   (desde cuándo está efectivamente activa: evita alertas de retraso justo al
--   habilitar una tarea o su agencia/empresa/grupo).
-- incident: una fila por INCIDENCIA. Las recurrencias de la misma falla
--   (misma categoría + instalación + tarea = dedup_key) se agrupan en la misma
--   incidencia abierta (contador, primera/última ocurrencia). Solo puede haber
--   UNA abierta por dedup_key (índice único parcial).
--   RECONOCIDA (acknowledged_*) y RESUELTA (status/resolved_*) son conceptos
--   distintos: reconocer nunca cambia el estado técnico.
-- incident_event: historial de cada incidencia (apertura, recurrencia,
--   reconocimiento, resolución, notificaciones...).
-- notification_channel / notification_outbox: interfaz configurable de
--   notificaciones externas (webhook firmado con HMAC o log). La URL y el
--   secreto de firma se guardan cifrados (ENC: Fernet) y son de solo escritura.
--   El outbox es transaccional: la notificación se encola en la misma
--   transacción que el cambio de estado de la incidencia y se entrega con
--   reintentos y backoff (idempotency_key única por canal).
-- Todas las fechas en UTC (timestamptz).
-- ============================================================================

ALTER TABLE agency_task ADD COLUMN IF NOT EXISTS expected_duration_seconds INT;
ALTER TABLE agency_task ADD COLUMN IF NOT EXISTS delay_tolerance_seconds  INT;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agency_task_expected_duration_ck') THEN
        ALTER TABLE agency_task ADD CONSTRAINT agency_task_expected_duration_ck
            CHECK (expected_duration_seconds IS NULL OR expected_duration_seconds BETWEEN 1 AND 31536000);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agency_task_delay_tolerance_ck') THEN
        ALTER TABLE agency_task ADD CONSTRAINT agency_task_delay_tolerance_ck
            CHECK (delay_tolerance_seconds IS NULL OR delay_tolerance_seconds BETWEEN 0 AND 31536000);
    END IF;
END $$;


CREATE TABLE IF NOT EXISTS task_health_state (
    task_id            INT          PRIMARY KEY REFERENCES agency_task(id) ON DELETE CASCADE,
    effective_active   BOOLEAN      NOT NULL,
    active_since       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    evaluated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS incident (
    id                        BIGSERIAL    PRIMARY KEY,
    dedup_key                 VARCHAR(200) NOT NULL,
    category                  VARCHAR(40)  NOT NULL CHECK (category IN (
                                  'disconnected', 'task_failed', 'task_delayed', 'task_running_long',
                                  'checkpoint_kind_mismatch', 'queue_dead_letter', 'queue_overflow')),
    severity                  VARCHAR(10)  NOT NULL CHECK (severity IN ('info', 'warning', 'error', 'critical')),
    status                    VARCHAR(10)  NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    installation_id           UUID         REFERENCES installation(id) ON DELETE SET NULL,
    installation_name         VARCHAR(255),
    task_id                   INT,          -- sin FK: la historia se conserva aunque se borre la tarea
    group_id                  INT,
    company_id                INT,
    agency_id                 INT,
    object_catalog_id         INT,
    title                     VARCHAR(300) NOT NULL DEFAULT '',
    last_error_code           VARCHAR(64),
    last_message_sanitized    VARCHAR(1000),
    details                   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    opened_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    occurrences               INT          NOT NULL DEFAULT 1,
    first_execution_id        UUID,
    last_execution_id         UUID,
    -- Orden de la evidencia (agent_seq de inicio de la ejecución más nueva que
    -- tocó la incidencia): un evento más viejo no la reabre ni la resuelve.
    last_evidence_seq         BIGINT,
    resolved_at               TIMESTAMPTZ,
    resolution_reason         VARCHAR(40),
    resolution_comment        VARCHAR(500),
    resolved_by               VARCHAR(100),
    resolved_execution_id     UUID,
    resolved_evidence_seq     BIGINT,
    duration_seconds          BIGINT,
    acknowledged_at           TIMESTAMPTZ,
    acknowledged_by           VARCHAR(100),
    ack_comment               VARCHAR(500),
    last_notified_at          TIMESTAMPTZ,
    notify_count              INT          NOT NULL DEFAULT 0,
    created_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT incident_resolved_ck CHECK (
        (status = 'open' AND resolved_at IS NULL) OR (status = 'resolved' AND resolved_at IS NOT NULL))
);
-- Una sola incidencia ABIERTA por clave de agrupación.
CREATE UNIQUE INDEX IF NOT EXISTS ux_incident_open_key ON incident(dedup_key) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_incident_status_opened ON incident(status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_key_resolved  ON incident(dedup_key, resolved_at DESC);
CREATE INDEX IF NOT EXISTS idx_incident_installation  ON incident(installation_id);
CREATE INDEX IF NOT EXISTS idx_incident_task          ON incident(task_id);
CREATE INDEX IF NOT EXISTS idx_incident_group         ON incident(group_id);


CREATE TABLE IF NOT EXISTS incident_event (
    id            BIGSERIAL    PRIMARY KEY,
    incident_id   BIGINT       NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    event_type    VARCHAR(30)  NOT NULL,
    actor         VARCHAR(100) NOT NULL DEFAULT 'system',
    message       VARCHAR(1000),
    execution_id  UUID,
    data          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_incident_event_inc ON incident_event(incident_id, id);


CREATE TABLE IF NOT EXISTS notification_channel (
    id                         SERIAL       PRIMARY KEY,
    name                       VARCHAR(100) NOT NULL UNIQUE,
    kind                       VARCHAR(20)  NOT NULL CHECK (kind IN ('webhook', 'log')),
    url_enc                    TEXT,        -- ENC:... (solo escritura; puede contener un token)
    url_display                VARCHAR(300) NOT NULL DEFAULT '',  -- esquema://host/… (sin ruta ni query)
    signing_secret_enc         TEXT,        -- ENC:... secreto HMAC (solo escritura)
    is_enabled                 BOOLEAN      NOT NULL DEFAULT TRUE,
    min_severity               VARCHAR(10)  NOT NULL DEFAULT 'warning'
                               CHECK (min_severity IN ('info', 'warning', 'error', 'critical')),
    group_id                   INT          REFERENCES client_group(id) ON DELETE CASCADE,
    categories                 TEXT[]       NOT NULL DEFAULT '{}',  -- vacío = todas
    notify_on_open             BOOLEAN      NOT NULL DEFAULT TRUE,
    notify_on_resolve          BOOLEAN      NOT NULL DEFAULT TRUE,
    reminder_interval_minutes  INT          NOT NULL DEFAULT 0 CHECK (reminder_interval_minutes BETWEEN 0 AND 10080),
    timeout_seconds            INT          NOT NULL DEFAULT 10 CHECK (timeout_seconds BETWEEN 1 AND 60),
    verify_tls                 BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS notification_outbox (
    id                BIGSERIAL    PRIMARY KEY,
    channel_id        INT          NOT NULL REFERENCES notification_channel(id) ON DELETE CASCADE,
    incident_id       BIGINT       REFERENCES incident(id) ON DELETE CASCADE,
    transition        VARCHAR(20)  NOT NULL CHECK (transition IN ('opened', 'resolved', 'reminder', 'test')),
    idempotency_key   VARCHAR(120) NOT NULL,
    payload           JSONB        NOT NULL,
    status            VARCHAR(12)  NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sending', 'delivered', 'failed', 'skipped')),
    attempts          INT          NOT NULL DEFAULT 0,
    next_attempt_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_error        VARCHAR(300),
    last_status_code  INT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    delivered_at      TIMESTAMPTZ,
    CONSTRAINT ux_outbox_idem UNIQUE (channel_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending  ON notification_outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_incident ON notification_outbox(incident_id);
CREATE INDEX IF NOT EXISTS idx_outbox_created  ON notification_outbox(created_at DESC);
