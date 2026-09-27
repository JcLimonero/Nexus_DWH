export type SourceType = "sqlserver" | "mysql" | "postgresql" | "pervasive" | "firebird";

interface Timestamps {
  created_at: string;
  updated_at: string;
}

interface SecretInfo {
  has_password: boolean;
  encrypted_fields: string[];
  decrypt_errors: string[];
}

export interface Group extends Timestamps, SecretInfo {
  id: number;
  name: string;
  group_token: string | null;
  warehouse_host: string | null;
  warehouse_port: number;
  warehouse_database: string | null;
  warehouse_username: string | null;
  is_enabled: boolean;
  company_count: number;
}

export interface Company extends Timestamps, SecretInfo {
  id: number;
  group_id: number;
  group_name: string;
  name: string;
  company_token: string;
  source_type: SourceType;
  source_host: string | null;
  source_port: number;
  source_database: string | null;
  source_username: string | null;
  source_dsn: string | null;
  verbose_logging: boolean;
  refresh_seconds: number;
  is_enabled: boolean;
  group_enabled: boolean;
  agency_count: number;
  object_count: number;
}

export interface Agency extends Timestamps {
  id: number;
  company_id: number;
  company_name: string;
  group_id: number;
  group_name: string;
  name: string;
  agency_token: string | null;
  is_enabled: boolean;
  task_count: number;
}

export interface CatalogObject extends Timestamps {
  id: number;
  company_id: number;
  company_name: string;
  group_id: number;
  group_name: string;
  name: string;
  description: string | null;
  destination_table: string;
  create_table_sql: string | null;
  upsert_keys: string | null;
  constraint_name: string | null;
  create_constraint_sql: string | null;
  static_columns: string | null;
  is_enabled: boolean;
  task_count: number;
}

export interface Task extends Timestamps {
  id: number;
  agency_id: number;
  agency_name: string;
  company_id: number;
  company_name: string;
  group_id: number;
  group_name: string;
  object_catalog_id: number;
  object_name: string;
  destination_table: string;
  extract_sql: string;
  schedule_seconds: number;
  is_active: boolean;
  run_on_company_token: boolean;
  last_run_at: string | null;
  effective_active: boolean;
  query_version?: number | null;
}

export interface Stats {
  groups: number;
  groups_enabled: number;
  companies: number;
  companies_enabled: number;
  agencies: number;
  agencies_enabled: number;
  objects: number;
  tasks: number;
  tasks_active: number;
  pending_errors: number;
  events_24h: number;
  errors_24h: number;
}

export interface MonitorClient {
  grupo: string;
  razon_social: string;
  token_preview: string;
  rs_enabled: boolean;
  grupo_enabled: boolean;
  last_seen: string | null;
  requests_total: number;
  http_errors_total: number;
  http_errors_1h: number;
  executions_total: number;
  exec_errors_total: number;
  exec_errors_pending: number;
  exec_errors_1h: number;
  last_execution: string | null;
}

export interface ClientEvent {
  id: number;
  timestamp: string;
  grupo: string;
  razon_social: string;
  config_id: string;
  task_name: string;
  event_type: "ok" | "error";
  detail: string | null;
  rows_loaded: number;
  acknowledged: boolean;
}

export interface ActivityItem {
  id: number;
  timestamp: string;
  grupo: string;
  razon_social: string;
  token: string;
  method: string;
  endpoint: string;
  status_code: number;
  response_ms: number;
  error_detail: string | null;
  client_ip: string;
}

export interface ListResponse<T> {
  items: T[];
}

export type InstallationScope = "group" | "company" | "agency";

export interface InstallationHeartbeat {
  uptime_seconds: number | null;
  queue_depth: number | null;
  queue_overflow_total: number | null;
  running: { task_id: number | null; execution_id: string | null }[];
  config_age_seconds: number | null;
  received_at: string | null;
}

export interface Installation {
  id: string;
  name: string;
  hostname: string;
  os_info: string;
  scope_type: InstallationScope;
  group_id: number;
  group_name: string;
  company_id: number | null;
  company_name: string | null;
  agency_id: number | null;
  agency_name: string | null;
  status: "active" | "revoked";
  enrolled_via: InstallationScope;
  enrollment_token_prefix: string;
  client_version: string;
  last_seen_at: string | null;
  last_ip?: string;
  last_heartbeat: InstallationHeartbeat | null;
  credential_rotated_at: string | null;
  rotation_required: boolean;
  rotation_in_grace: boolean;
  revoked_at: string | null;
  revoked_reason: string | null;
  created_at: string;
  failures_24h: number;
  last_execution_at: string | null;
  legacy: false;
}

export interface LegacyClient {
  auth_kind: InstallationScope;
  group_id: number | null;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
  agency_id: number | null;
  agency_name: string | null;
  token_prefix: string;
  last_seen_at: string | null;
  requests_30d: number;
  http_errors_30d: number;
  last_ip: string | null;
  legacy: true;
}

export type ExecutionStatus = "running" | "success" | "failed" | "interrupted";
export type FailureStage = "config" | "extract" | "transform" | "load" | "report";

export interface Execution {
  execution_id: string;
  installation_id: string;
  installation_name: string | null;
  task_id: number;
  group_id: number | null;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
  agency_id: number | null;
  agency_name: string | null;
  object_name: string | null;
  destination_table: string | null;
  client_version: string;
  query_version: number | null;
  attempt: number;
  status: ExecutionStatus;
  failure_stage: FailureStage | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  rows_read: number | null;
  rows_loaded: number | null;
  rows_inserted: number | null;
  rows_updated: number | null;
  error_code: string | null;
  error_message_sanitized: string | null;
  warnings: string[];
  checkpoint_confirmed: string | null;
  checkpoint_kind: string | null;
  received_at: string;
}
