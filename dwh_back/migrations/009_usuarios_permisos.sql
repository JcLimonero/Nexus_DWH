-- ============================================================================
-- 009 — Usuarios del panel, roles, permisos por grupo, sesiones y auditoría
-- ============================================================================
-- * panel_user: usuarios con contraseña (argon2id). Sin usuario por defecto:
--   el primero se crea con `python manage_users.py create-superadmin`.
-- * panel_permission / panel_role / panel_role_permission: catálogo de permisos
--   y roles sembrados (lectura, operador, atribucion_estructura, ...).
-- * panel_user_role: rol por ALCANCE (group_id NULL = todos los grupos; si no,
--   solo ese grupo). Un usuario puede ser operador en A y lectura en B.
-- * panel_session: sesiones opacas; solo se guarda sha256(token).
-- * panel_audit_log: acciones del panel (mutaciones, inicios/cierres de sesión,
--   intentos fallidos, uso del token estático).
-- * Columnas *_user_id junto a los actores de texto existentes ("admin" se
--   conserva en los registros previos).
-- ============================================================================

CREATE TABLE IF NOT EXISTS panel_user (
    id                     SERIAL       PRIMARY KEY,
    username               VARCHAR(64)  NOT NULL,
    display_name           VARCHAR(120) NOT NULL DEFAULT '',
    email                  VARCHAR(255),
    password_hash          TEXT         NOT NULL,
    is_active              BOOLEAN      NOT NULL DEFAULT TRUE,
    is_superadmin          BOOLEAN      NOT NULL DEFAULT FALSE,
    must_change_password   BOOLEAN      NOT NULL DEFAULT TRUE,
    failed_attempts        INT          NOT NULL DEFAULT 0,
    locked_until           TIMESTAMPTZ,
    last_login_at          TIMESTAMPTZ,
    last_failed_at         TIMESTAMPTZ,
    password_changed_at    TIMESTAMPTZ,
    created_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by             VARCHAR(100) NOT NULL DEFAULT 'system',
    created_by_user_id     INT          REFERENCES panel_user(id) ON DELETE SET NULL,
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT panel_user_username_ck CHECK (username ~ '^[a-z0-9][a-z0-9._-]{2,63}$')
);
-- Unicidad sin distinguir mayúsculas (el código ya guarda el nombre en minúsculas).
CREATE UNIQUE INDEX IF NOT EXISTS ux_panel_user_username ON panel_user (lower(username));


CREATE TABLE IF NOT EXISTS panel_permission (
    code          VARCHAR(64)  PRIMARY KEY,
    description   VARCHAR(255) NOT NULL,
    global_only   BOOLEAN      NOT NULL DEFAULT FALSE
);

INSERT INTO panel_permission (code, description, global_only) VALUES
    ('view',                       'Consultar: salud, cargas, incidencias, ejecuciones, estructura y configuración (sin secretos)', FALSE),
    ('incident.acknowledge',       'Reconocer incidencias y eventos legados', FALSE),
    ('incident.close_queue',       'Cerrar manualmente incidencias de cola local (con motivo)', FALSE),
    ('structure.acknowledge',      'Dar por entendido un cambio estructural (atribuir responsable)', FALSE),
    ('structure.reclassify',       'Reclasificar la atribución de un cambio ya entendido', FALSE),
    ('inventory.approve_baseline', 'Aprobar o reiniciar la línea base de una base monitoreada', FALSE),
    ('inventory.configure',        'Configurar bases monitoreadas (alta, alcance, frecuencia, duplicados, responsable)', FALSE),
    ('inventory.view_definitions', 'Ver el SQL (cifrado) de las definiciones de vistas', FALSE),
    ('credentials.manage',         'Administrar credenciales: secretos de grupos/empresas, tokens de enrolamiento, instalaciones y canales de notificación', FALSE),
    ('config.manage',              'Administrar configuración: grupos, empresas, agencias, catálogo, tareas y umbrales', FALSE),
    ('audit.view',                 'Consultar la auditoría del panel', FALSE),
    ('users.manage',               'Administrar usuarios, roles y sesiones (solo alcance global)', TRUE)
ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description, global_only = EXCLUDED.global_only;


CREATE TABLE IF NOT EXISTS panel_role (
    code          VARCHAR(64)  PRIMARY KEY,
    name          VARCHAR(120) NOT NULL,
    description   VARCHAR(255) NOT NULL DEFAULT '',
    is_system     BOOLEAN      NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS panel_role_permission (
    role_code        VARCHAR(64) NOT NULL REFERENCES panel_role(code) ON DELETE CASCADE,
    permission_code  VARCHAR(64) NOT NULL REFERENCES panel_permission(code) ON DELETE CASCADE,
    PRIMARY KEY (role_code, permission_code)
);

INSERT INTO panel_role (code, name, description) VALUES
    ('lectura',               'Lectura',                   'Solo consulta'),
    ('operador',              'Operador',                  'Consulta + reconocer incidencias/eventos + cerrar incidencias de cola'),
    ('atribucion_estructura', 'Atribución de estructura',  'Consulta + dar por entendido y reclasificar cambios estructurales'),
    ('aprobador_inventario',  'Aprobador de inventario',   'Consulta + aprobar/reiniciar línea base + configurar bases monitoreadas'),
    ('definiciones_vistas',   'Definiciones de vistas',    'Consulta + ver el SQL de vistas (sensible)'),
    ('admin_credenciales',    'Administrador de credenciales', 'Consulta + secretos, tokens, instalaciones y canales'),
    ('admin_config',          'Administrador de configuración', 'Consulta + grupos, empresas, agencias, catálogo y tareas'),
    ('auditor',               'Auditor',                   'Consulta + auditoría del panel'),
    ('admin_usuarios',        'Administrador de usuarios', 'Usuarios, roles y sesiones (solo alcance global) + auditoría')
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description;

INSERT INTO panel_role_permission (role_code, permission_code) VALUES
    ('lectura', 'view'),
    ('operador', 'view'), ('operador', 'incident.acknowledge'), ('operador', 'incident.close_queue'),
    ('atribucion_estructura', 'view'), ('atribucion_estructura', 'structure.acknowledge'),
    ('atribucion_estructura', 'structure.reclassify'),
    ('aprobador_inventario', 'view'), ('aprobador_inventario', 'inventory.approve_baseline'),
    ('aprobador_inventario', 'inventory.configure'),
    ('definiciones_vistas', 'view'), ('definiciones_vistas', 'inventory.view_definitions'),
    ('admin_credenciales', 'view'), ('admin_credenciales', 'credentials.manage'),
    ('admin_config', 'view'), ('admin_config', 'config.manage'),
    ('auditor', 'view'), ('auditor', 'audit.view'),
    ('admin_usuarios', 'view'), ('admin_usuarios', 'users.manage'), ('admin_usuarios', 'audit.view')
ON CONFLICT DO NOTHING;


CREATE TABLE IF NOT EXISTS panel_user_role (
    id           SERIAL      PRIMARY KEY,
    user_id      INT         NOT NULL REFERENCES panel_user(id) ON DELETE CASCADE,
    role_code    VARCHAR(64) NOT NULL REFERENCES panel_role(code) ON DELETE CASCADE,
    group_id     INT         REFERENCES client_group(id) ON DELETE CASCADE,  -- NULL = todos los grupos
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by   VARCHAR(100) NOT NULL DEFAULT 'system'
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_panel_user_role ON panel_user_role (user_id, role_code, COALESCE(group_id, 0));
CREATE INDEX IF NOT EXISTS idx_panel_user_role_user ON panel_user_role (user_id);


CREATE TABLE IF NOT EXISTS panel_session (
    id             BIGSERIAL    PRIMARY KEY,
    token_hash     CHAR(64)     NOT NULL UNIQUE,   -- sha256(token); el token nunca se guarda
    user_id        INT          NOT NULL REFERENCES panel_user(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at     TIMESTAMPTZ  NOT NULL,          -- vencimiento absoluto
    ip             VARCHAR(45),
    user_agent     VARCHAR(200),
    revoked_at     TIMESTAMPTZ,
    revoked_reason VARCHAR(40)
);
CREATE INDEX IF NOT EXISTS idx_panel_session_user ON panel_session (user_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_panel_session_expires ON panel_session (expires_at);


CREATE TABLE IF NOT EXISTS panel_audit_log (
    id            BIGSERIAL    PRIMARY KEY,
    at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor_user_id INT          REFERENCES panel_user(id) ON DELETE SET NULL,
    actor_name    VARCHAR(100) NOT NULL DEFAULT '',
    auth_kind     VARCHAR(20)  NOT NULL DEFAULT '',   -- session | static_token | anonymous
    action        VARCHAR(120) NOT NULL,
    target_type   VARCHAR(60),
    target_id     VARCHAR(80),
    group_id      INT,
    status_code   INT,
    details       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    ip            VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS idx_panel_audit_at ON panel_audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_panel_audit_group ON panel_audit_log (group_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_panel_audit_actor ON panel_audit_log (actor_user_id, at DESC);


-- ── Actor por id junto a los actores de texto existentes ────────────────────
ALTER TABLE incident          ADD COLUMN IF NOT EXISTS acknowledged_by_user_id INT;
ALTER TABLE incident          ADD COLUMN IF NOT EXISTS resolved_by_user_id     INT;
ALTER TABLE incident_event    ADD COLUMN IF NOT EXISTS actor_user_id           INT;
ALTER TABLE structural_change ADD COLUMN IF NOT EXISTS ack_by_user_id          INT;
ALTER TABLE structural_change ADD COLUMN IF NOT EXISTS reclassified_by_user_id INT;
ALTER TABLE structural_change_event ADD COLUMN IF NOT EXISTS actor_user_id     INT;
ALTER TABLE monitored_database ADD COLUMN IF NOT EXISTS baseline_approved_by_user_id INT;
ALTER TABLE monitored_database_event ADD COLUMN IF NOT EXISTS actor_user_id    INT;
ALTER TABLE inventory_baseline_version ADD COLUMN IF NOT EXISTS actor_user_id  INT;
ALTER TABLE client_events     ADD COLUMN IF NOT EXISTS acknowledged_by         VARCHAR(100);
ALTER TABLE client_events     ADD COLUMN IF NOT EXISTS acknowledged_by_user_id INT;
ALTER TABLE client_events     ADD COLUMN IF NOT EXISTS acknowledged_at         TIMESTAMPTZ;
ALTER TABLE installation      ADD COLUMN IF NOT EXISTS revoked_by              VARCHAR(100);
ALTER TABLE installation      ADD COLUMN IF NOT EXISTS revoked_by_user_id      INT;
