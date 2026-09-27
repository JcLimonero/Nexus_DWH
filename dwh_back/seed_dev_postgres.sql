-- ============================================================================
-- DATOS DE EJEMPLO — SOLO PARA DESARROLLO LOCAL
-- ============================================================================
-- Hosts, usuarios, contraseñas y tokens son FICTICIOS. No usar en producción.
-- Las credenciales van en texto plano (sin ENC:) a propósito: el backend las
-- devuelve tal cual; las que se editen desde el panel web se guardarán cifradas
-- si el backend tiene config_secret_key.
--
-- Idempotente: se puede ejecutar varias veces (ON CONFLICT DO NOTHING).
-- Requiere haber aplicado antes schema_postgres.sql.
-- ============================================================================

INSERT INTO client_group
    (name, group_token, warehouse_host, warehouse_port, warehouse_database,
     warehouse_username, warehouse_password, is_enabled)
VALUES
    ('Grupo Demo', 'dev-group-token-demo-0000000000000000000000', 'dwh.demo.invalid', 5432,
     'dwh_demo', 'dwh_user_demo', 'fake-dwh-password', TRUE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO company
    (group_id, name, company_token, source_type, source_host, source_port,
     source_database, source_username, source_password, refresh_seconds)
SELECT g.id, 'Empresa Demo SQL Server', 'dev-company-token-demo-00000000000000000000',
       'sqlserver', 'dms.demo.invalid', 1433, 'DMS_DEMO', 'dms_reader', 'fake-src-password', 60
FROM client_group g WHERE g.name = 'Grupo Demo'
ON CONFLICT (group_id, name) DO NOTHING;

INSERT INTO company
    (group_id, name, company_token, source_type, source_host, source_port,
     source_database, source_username, source_password, refresh_seconds, is_enabled)
SELECT g.id, 'Empresa Demo Firebird', 'dev-company-token-fb-0000000000000000000000',
       'firebird', 'fb.demo.invalid', 3050, '/data/demo.fdb', 'SYSDBA', 'fake-fb-password', 120, FALSE
FROM client_group g WHERE g.name = 'Grupo Demo'
ON CONFLICT (group_id, name) DO NOTHING;

INSERT INTO agency (company_id, name, agency_token)
SELECT c.id, 'Agencia Centro', 'dev-agency-token-centro-000000000000000000'
FROM company c WHERE c.name = 'Empresa Demo SQL Server'
ON CONFLICT (company_id, name) DO NOTHING;

INSERT INTO agency (company_id, name, agency_token)
SELECT c.id, 'Agencia Norte', NULL
FROM company c WHERE c.name = 'Empresa Demo SQL Server'
ON CONFLICT (company_id, name) DO NOTHING;

INSERT INTO object_catalog
    (company_id, name, description, destination_table, create_table_sql,
     upsert_keys, constraint_name, create_constraint_sql, static_columns)
SELECT c.id, 'Inventory', 'Inventario de vehículos (demo)', 'inventory',
       $$CREATE TABLE IF NOT EXISTS inventory (
    "idAgency" VARCHAR(64) NOT NULL,
    "vin" VARCHAR(32) NOT NULL,
    "model" VARCHAR(128),
    "timestamp_dms" TIMESTAMP,
    PRIMARY KEY ("idAgency", "vin")
)$$,
       'idAgency,vin', NULL, NULL, NULL
FROM company c WHERE c.name = 'Empresa Demo SQL Server'
ON CONFLICT (company_id, name) DO NOTHING;

INSERT INTO object_catalog
    (company_id, name, description, destination_table, upsert_keys)
SELECT c.id, 'Customers', 'Clientes (demo)', 'customers', 'idAgency,ndClientDMS'
FROM company c WHERE c.name = 'Empresa Demo SQL Server'
ON CONFLICT (company_id, name) DO NOTHING;

INSERT INTO agency_task (agency_id, object_catalog_id, extract_sql, schedule_seconds, is_active)
SELECT a.id, oc.id,
       $$SELECT * FROM view_get_dwh_inventory WHERE timestamp_dms >= '{last_run}' ORDER BY timestamp_dms ASC$$,
       3600, TRUE
FROM agency a
JOIN object_catalog oc ON oc.company_id = a.company_id AND oc.name = 'Inventory'
WHERE a.name = 'Agencia Centro'
ON CONFLICT (agency_id, object_catalog_id) DO NOTHING;

INSERT INTO agency_task (agency_id, object_catalog_id, extract_sql, schedule_seconds, is_active)
SELECT a.id, oc.id,
       $$SELECT * FROM view_get_dwh_customers WHERE updated_at >= '{last_run}'$$,
       7200, FALSE
FROM agency a
JOIN object_catalog oc ON oc.company_id = a.company_id AND oc.name = 'Customers'
WHERE a.name = 'Agencia Centro'
ON CONFLICT (agency_id, object_catalog_id) DO NOTHING;

-- Eventos de ejemplo (solo si la tabla está vacía)
INSERT INTO client_events
    (token, group_name, company_name, config_id, task_name, event_type, detail, rows_loaded, is_acknowledged)
SELECT 'dev-company-token-demo-00000000000000000000', 'Grupo Demo', 'Empresa Demo SQL Server',
       '1', 'Grupo Demo | Empresa Demo SQL Server | Agencia Centro', v.event_type, v.detail, v.rows_loaded, v.ack
FROM (VALUES
    ('ok',    '120 filas cargadas en ''inventory''', 120, 1),
    ('error', 'Traceback (demo): timeout al conectar con el origen', 0, 0)
) AS v(event_type, detail, rows_loaded, ack)
WHERE NOT EXISTS (SELECT 1 FROM client_events);
