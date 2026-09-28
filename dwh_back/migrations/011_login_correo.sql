-- ============================================================================
-- 011 — Acceso por correo
-- ============================================================================
-- El requerimiento es que el inicio de sesión del panel sea por CORREO en
-- lugar de por usuario. Esta migración:
--   1. Normaliza los correos existentes de panel_user (minúsculas, sin
--      espacios al inicio/fin; cadena vacía -> NULL).
--   2. Si tras normalizar hay DUPLICADOS, la migración ABORTA con un mensaje
--      claro (no se descarta ni se inventa ningún dato). Un operador debe
--      corregir manualmente los correos en conflicto y volver a ejecutar
--      `python migrate.py`.
--   3. Crea el índice único (case-insensitive) sobre email.
--
-- Los usuarios SIN correo (email IS NULL) no podrán iniciar sesión hasta que
-- un administrador (panel, `PUT /admin/users/{id}`) o el CLI
-- (`manage_users.py set-email`) les asigne uno. No se inventa ningún correo.
-- ============================================================================

-- 1) Normalizar (minúsculas, trim; vacío -> NULL).
UPDATE panel_user
   SET email = NULLIF(lower(trim(email)), '')
 WHERE email IS NOT NULL
   AND email <> NULLIF(lower(trim(email)), '');

UPDATE panel_user
   SET email = NULL
 WHERE email IS NOT NULL AND trim(email) = '';

-- 2) Abortar si hay duplicados tras normalizar (fail-safe: nunca se descarta
--    en silencio). El mensaje incluye los usuarios en conflicto.
DO $$
DECLARE
    dup_summary TEXT;
BEGIN
    SELECT string_agg(format('%s (usuarios: %s)', e, u), '; ')
      INTO dup_summary
      FROM (
          SELECT lower(trim(email)) AS e, string_agg(username, ', ' ORDER BY username) AS u
            FROM panel_user
           WHERE email IS NOT NULL AND trim(email) <> ''
           GROUP BY lower(trim(email))
          HAVING COUNT(*) > 1
      ) d;

    IF dup_summary IS NOT NULL THEN
        RAISE EXCEPTION
            'Migración 011_login_correo abortada: hay correos duplicados en panel_user (sin distinguir '
            'mayúsculas) que deben corregirse antes de aplicar el índice único. Duplicados: %', dup_summary;
    END IF;
END $$;

-- 3) Índice único (case-insensitive) sobre email. Usuarios sin correo (NULL)
--    quedan fuera del índice: pueden coexistir varios sin correo.
CREATE UNIQUE INDEX IF NOT EXISTS ux_panel_user_email ON panel_user (lower(email)) WHERE email IS NOT NULL;
