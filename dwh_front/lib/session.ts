/** Compartido entre middleware (edge), route handlers (node) y el navegador. */
/** Cookie httpOnly con el token OPACO de sesión del backend (nunca la contraseña). */
export const SESSION_COOKIE = "dwh_panel_session";
/**
 * Cabecera anti-CSRF exigida por el servidor del panel en toda petición que
 * modifica (además de SameSite=Strict y la verificación de Origin). Un
 * formulario de otro sitio no puede enviar cabeceras personalizadas.
 */
export const CSRF_HEADER = "x-nexus-csrf";
export const CSRF_VALUE = "1";
