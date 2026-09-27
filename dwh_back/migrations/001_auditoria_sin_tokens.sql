-- ============================================================================
-- 001 — Auditoría sin tokens completos
-- ============================================================================
-- * activity_log y client_events dejan de guardar el token completo: solo un
--   prefijo corto (8 caracteres) + ids resueltos (grupo/empresa/agencia/
--   instalación) y el tipo de credencial usada (auth_kind).
-- * Se rellenan los ids de los registros existentes a partir del token completo
--   ANTES de recortarlo (así /monitor/clients sigue agrupando el histórico).
-- * source en client_events: 'legacy' (POST /client-event) o 'agent' (API /agent).
-- Idempotente: ADD COLUMN IF NOT EXISTS y UPDATE con condiciones.
-- ============================================================================

ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS auth_kind       VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS group_id        INT;
ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS company_id      INT;
ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS agency_id       INT;
ALTER TABLE activity_log ADD COLUMN IF NOT EXISTS installation_id UUID;

ALTER TABLE client_events ADD COLUMN IF NOT EXISTS source          VARCHAR(10) NOT NULL DEFAULT 'legacy';
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS auth_kind       VARCHAR(20) NOT NULL DEFAULT '';
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS group_id        INT;
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS company_id      INT;
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS agency_id       INT;
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS task_id         INT;
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS installation_id UUID;
ALTER TABLE client_events ADD COLUMN IF NOT EXISTS execution_id    UUID;

-- ── Relleno de ids a partir del token completo (solo filas sin ids) ─────────
-- client_events: token de company
UPDATE client_events ce
   SET company_id = c.id, group_id = c.group_id, auth_kind = 'company'
  FROM company c
 WHERE ce.group_id IS NULL AND length(ce.token) > 8 AND ce.token = c.company_token;

-- client_events: token de agencia
UPDATE client_events ce
   SET agency_id = a.id, company_id = c.id, group_id = c.group_id, auth_kind = 'agency'
  FROM agency a JOIN company c ON c.id = a.company_id
 WHERE ce.group_id IS NULL AND length(ce.token) > 8 AND ce.token = a.agency_token;

-- client_events: token de grupo
UPDATE client_events ce
   SET group_id = g.id, auth_kind = 'group'
  FROM client_group g
 WHERE ce.group_id IS NULL AND length(ce.token) > 8 AND ce.token = g.group_token;

-- client_events: tarea (config_id numérico) → task/agencia/empresa, solo si
-- pertenece al mismo grupo ya resuelto.
UPDATE client_events ce
   SET task_id = t.id,
       agency_id = COALESCE(ce.agency_id, a.id),
       company_id = COALESCE(ce.company_id, a.company_id)
  FROM agency_task t
  JOIN agency a  ON a.id = t.agency_id
  JOIN company c ON c.id = a.company_id
 WHERE ce.task_id IS NULL
   AND ce.config_id ~ '^[0-9]{1,9}$'
   AND t.id = ce.config_id::int
   AND ce.group_id = c.group_id;

-- activity_log
UPDATE activity_log al
   SET company_id = c.id, group_id = c.group_id, auth_kind = 'company'
  FROM company c
 WHERE al.group_id IS NULL AND length(al.token) > 8 AND al.token = c.company_token;

UPDATE activity_log al
   SET agency_id = a.id, company_id = c.id, group_id = c.group_id, auth_kind = 'agency'
  FROM agency a JOIN company c ON c.id = a.company_id
 WHERE al.group_id IS NULL AND length(al.token) > 8 AND al.token = a.agency_token;

UPDATE activity_log al
   SET group_id = g.id, auth_kind = 'group'
  FROM client_group g
 WHERE al.group_id IS NULL AND length(al.token) > 8 AND al.token = g.group_token;

-- ── Recorte de tokens a prefijo ──────────────────────────────────────────────
UPDATE client_events SET token = left(token, 8) WHERE length(token) > 8;
UPDATE activity_log  SET token = left(token, 8) WHERE length(token) > 8;

-- El detalle de error de activity_log nunca debe guardar trazas completas.
UPDATE activity_log
   SET error_detail = left(error_detail, 2000)
 WHERE length(error_detail) > 2000;

CREATE INDEX IF NOT EXISTS idx_ce_company_id      ON client_events(company_id);
CREATE INDEX IF NOT EXISTS idx_ce_group_id        ON client_events(group_id);
CREATE INDEX IF NOT EXISTS idx_ce_installation_id ON client_events(installation_id);
CREATE INDEX IF NOT EXISTS idx_actlog_company_id  ON activity_log(company_id);
CREATE INDEX IF NOT EXISTS idx_actlog_install_id  ON activity_log(installation_id);
