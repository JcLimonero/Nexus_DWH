-- ============================================================================
-- 008 — Ajustes del inventario estructural (observaciones de validación)
-- ============================================================================
-- * engine_identity_weak: identidad "débil" (dirección/puerto del servidor +
--   oid/nombre de la base) además de la "fuerte" (system_identifier). Solo se
--   declara "otro servidor" cuando un componente COMPARABLE difiere
--   (fuerte vs fuerte, o débil vs débil si alguno no tiene fuerte).
-- * allow_engine_duplicate: el administrador deshizo una detección de
--   duplicado ("deshacer duplicado"): no se vuelve a marcar sola.
-- * structural_change.status 'out_of_scope': alertas pendientes de esquemas
--   que luego se excluyeron del alcance (se cierran, el historial queda).
-- * monitored_database_link se limpia al borrar el grupo (FK) o la empresa
--   (trigger; company_id = 0 significa "todo el grupo").
-- ============================================================================

ALTER TABLE monitored_database ADD COLUMN IF NOT EXISTS engine_identity_weak   CHAR(64);
ALTER TABLE monitored_database ADD COLUMN IF NOT EXISTS allow_engine_duplicate BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE inventory_snapshot ADD COLUMN IF NOT EXISTS engine_identity_weak   CHAR(64);

-- Identidades débiles ya guardadas como "principales".
UPDATE monitored_database SET engine_identity_weak = engine_identity
 WHERE engine_identity_strength = 'weak' AND engine_identity_weak IS NULL;

ALTER TABLE structural_change DROP CONSTRAINT IF EXISTS structural_change_status_check;
ALTER TABLE structural_change ADD CONSTRAINT structural_change_status_check
    CHECK (status IN ('pending', 'acknowledged', 'superseded', 'reverted', 'out_of_scope'));

DELETE FROM monitored_database_link l
 WHERE NOT EXISTS (SELECT 1 FROM client_group g WHERE g.id = l.group_id);
DELETE FROM monitored_database_link l
 WHERE l.company_id <> 0 AND NOT EXISTS (SELECT 1 FROM company c WHERE c.id = l.company_id);
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'monitored_database_link_group_fk') THEN
        ALTER TABLE monitored_database_link ADD CONSTRAINT monitored_database_link_group_fk
            FOREIGN KEY (group_id) REFERENCES client_group(id) ON DELETE CASCADE;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION nexus_mdb_link_company_cleanup() RETURNS trigger AS $$
BEGIN
    DELETE FROM monitored_database_link WHERE company_id = OLD.id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_mdb_link_company_cleanup ON company;
CREATE TRIGGER trg_mdb_link_company_cleanup AFTER DELETE ON company
    FOR EACH ROW EXECUTE FUNCTION nexus_mdb_link_company_cleanup();
