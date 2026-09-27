-- ============================================================================
-- 006 — Índices para el modelo de salud y la retención del historial
-- ============================================================================
-- El evaluador de salud y /admin/health/* consultan por tarea la última
-- ejecución, la última carga exitosa (hora acotada por la recepción del
-- servidor), el historial de duraciones y las ejecuciones en curso. Estos
-- índices (de expresión y parciales) evitan recorrer task_execution completo
-- a medida que crece. La retención ([health] execution_retention_days) borra
-- por received_at.
-- ============================================================================

-- Última ejecución por tarea: ORDER BY COALESCE(started_at, received_at) DESC
CREATE INDEX IF NOT EXISTS idx_texec_task_last
    ON task_execution (task_id, (COALESCE(started_at, received_at)) DESC);

-- Última carga exitosa por tarea / por instalación (hora acotada por el servidor)
CREATE INDEX IF NOT EXISTS idx_texec_task_success
    ON task_execution (task_id, (LEAST(COALESCE(finished_at, updated_at), updated_at)) DESC)
    WHERE status = 'success';
CREATE INDEX IF NOT EXISTS idx_texec_inst_success
    ON task_execution (installation_id, (LEAST(COALESCE(finished_at, updated_at), updated_at)) DESC)
    WHERE status = 'success';

-- Historial de duraciones (p90)
CREATE INDEX IF NOT EXISTS idx_texec_task_success_recv
    ON task_execution (task_id, received_at DESC)
    WHERE status = 'success';
-- ¿Hay una ejecución más nueva de esta tarea en esta instalación? (orden de evidencia)
CREATE INDEX IF NOT EXISTS idx_texec_task_inst_seq
    ON task_execution (task_id, installation_id, (COALESCE(start_agent_seq, last_agent_seq)));

-- Ejecuciones en curso por tarea
CREATE INDEX IF NOT EXISTS idx_texec_task_running
    ON task_execution (task_id, received_at DESC)
    WHERE status = 'running';

-- Retención del historial
CREATE INDEX IF NOT EXISTS idx_incident_event_created ON incident_event (created_at);
CREATE INDEX IF NOT EXISTS idx_incident_open_task_cat ON incident (task_id, category) WHERE status = 'open';
