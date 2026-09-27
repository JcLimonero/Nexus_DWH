export type SourceType = "sqlserver" | "mysql" | "postgresql" | "pervasive" | "firebird";

interface Timestamps {
  created_at: string;
  updated_at: string;
}

interface SecretInfo {
  has_password: boolean;
  encrypted_fields: string[];
  decrypt_errors: string[];
  /** true = sin credentials.manage: host/base/usuario llegan en null */
  secrets_hidden?: boolean;
}

/** Tokens de enrolamiento: solo con credentials.manage sobre el grupo. */
interface TokenInfo {
  has_token?: boolean;
  token_hidden?: boolean;
}

export interface Group extends Timestamps, SecretInfo, TokenInfo {
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

export interface Company extends Timestamps, SecretInfo, TokenInfo {
  id: number;
  group_id: number;
  group_name: string;
  name: string;
  company_token: string | null;
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

export interface Agency extends Timestamps, TokenInfo {
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
  expected_duration_seconds?: number | null;
  delay_tolerance_seconds?: number | null;
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
  /** null sin credentials.manage sobre el grupo */
  token_preview: string | null;
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
  group_id?: number;
  company_id?: number;
  /** ISO UTC con zona (aditivo; backend fase 2). */
  last_seen_utc?: string | null;
  last_execution_utc?: string | null;
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
  group_id?: number | null;
  company_id?: number | null;
  agency_id?: number | null;
  acknowledged_by?: string | null;
  acknowledged_at?: string | null;
}

export interface ActivityItem {
  id: number;
  timestamp: string;
  grupo: string;
  razon_social: string;
  /** prefijo del token legado; null sin credentials.manage */
  token: string | null;
  method: string;
  endpoint: string;
  status_code: number;
  response_ms: number;
  error_detail: string | null;
  client_ip: string;
  auth_kind?: string;
  group_id?: number | null;
}

export interface PanelUserRole {
  role: string;
  role_name: string;
  group_id: number | null;
  group_name: string | null;
}

export interface PanelUser {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  is_active: boolean;
  is_superadmin: boolean;
  must_change_password: boolean;
  failed_attempts: number;
  locked: boolean;
  locked_until: string | null;
  last_login_at: string | null;
  password_changed_at: string | null;
  created_at: string;
  created_by: string;
  roles: PanelUserRole[];
  active_sessions: number;
}

export interface PanelRole {
  code: string;
  name: string;
  description: string;
  permissions: string[];
  global_only: boolean;
}

export interface PanelSession {
  id: number;
  user_id: number;
  username: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  ip: string | null;
  user_agent: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  current: boolean;
  is_superadmin?: boolean;
}

export interface AuditEntry {
  id: number;
  at: string;
  actor_user_id: number | null;
  actor_name: string;
  auth_kind: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  group_id: number | null;
  group_name: string | null;
  status_code: number | null;
  details: Record<string, unknown>;
  ip: string | null;
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

// ── Salud, incidencias y notificaciones (fase 2) ─────────────────────────────
export type Severity = "info" | "warning" | "error" | "critical";
export type IncidentCategory =
  | "disconnected"
  | "task_failed"
  | "task_delayed"
  | "task_running_long"
  | "checkpoint_kind_mismatch"
  | "queue_dead_letter"
  | "queue_overflow";
export type TaskHealthState = "ok" | "running" | "failing" | "delayed" | "never_run" | "disabled";
export type Connectivity = "online" | "offline" | "never" | "revoked" | "scope_disabled";

export interface OpenIncidentRef {
  id: number;
  category: IncidentCategory;
  severity: Severity;
  acknowledged: boolean;
}

export interface TaskHealth {
  task_id: number;
  group_id: number;
  group_name: string;
  company_id: number;
  company_name: string;
  agency_id: number;
  agency_name: string;
  object_catalog_id: number;
  object_name: string;
  destination_table: string;
  is_active: boolean;
  effective_active: boolean;
  state: TaskHealthState;
  delayed: boolean;
  failing: boolean;
  running: boolean;
  running_long: boolean;
  schedule_seconds: number;
  expected_duration_seconds: number;
  expected_duration_source: "configured" | "history" | "default";
  delay_tolerance_seconds: number;
  delay_tolerance_source: "configured" | "default";
  running_long_after_seconds: number;
  active_since: string | null;
  last_execution: {
    execution_id: string;
    status: ExecutionStatus;
    started_at: string | null;
    finished_at: string | null;
    error_code: string | null;
    rows_loaded: number | null;
    installation_id: string | null;
  } | null;
  last_success_at: string | null;
  last_success_rows: number | null;
  last_success_execution_id: string | null;
  failing_installations: {
    incident_id: number;
    installation_id: string | null;
    installation_name: string | null;
    error_code: string | null;
    acknowledged: boolean;
    occurrences: number;
    last_seen_at: string | null;
  }[];
  watermark: string | null;
  watermark_kind: string | null;
  current_error_code: string | null;
  last_status: string | null;
  consecutive_failures: number;
  last_failure_at: string | null;
  running_execution: {
    execution_id: string;
    installation_id: string;
    installation_name: string | null;
    since: string | null;
    elapsed_seconds: number | null;
    confirmed_by_heartbeat: boolean;
  } | null;
  next_expected_run_at: string | null;
  delay_deadline_at: string | null;
  installations_covering: number;
  open_incidents: OpenIncidentRef[];
}

export interface InstallationHealth {
  id: string;
  name: string;
  hostname: string;
  scope_type: InstallationScope;
  group_id: number;
  group_name: string;
  company_id: number | null;
  company_name: string | null;
  agency_id: number | null;
  agency_name: string | null;
  status: "active" | "revoked";
  connectivity: Connectivity;
  client_version: string;
  last_seen_at: string | null;
  seconds_since_contact: number | null;
  last_heartbeat_at: string | null;
  last_execution_at: string | null;
  last_success_at: string | null;
  open_incidents: number;
  queue_depth: number | null;
  dead_letter_total: number | null;
  parked_total: number | null;
  queue_overflow_total: number | null;
  running: { task_id: number | null; execution_id: string | null }[];
  uptime_seconds: number | null;
  disconnect_after_seconds: number;
}

export interface HealthSummary {
  installations: Partial<Record<Connectivity, number>>;
  tasks: Partial<Record<TaskHealthState, number>>;
  incidents: {
    open_by_severity: Record<Severity, number>;
    open_total: number;
    open_unacknowledged: number;
    resolved_24h: number;
  };
  disconnect_after_seconds: number;
}

/** Acciones permitidas al usuario sobre un recurso de inventario (calculadas en el backend). */
export interface AllowedActions {
  configure: boolean;
  approve_baseline: boolean;
  acknowledge: boolean;
  reclassify: boolean;
  view_definitions: boolean;
}

export interface Incident {
  id: number;
  category: IncidentCategory;
  category_label: string;
  severity: Severity;
  status: "open" | "resolved";
  installation_id: string | null;
  installation_name: string | null;
  task_id: number | null;
  group_id: number | null;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
  agency_id: number | null;
  agency_name: string | null;
  object_catalog_id: number | null;
  object_name: string | null;
  title: string;
  last_error_code: string | null;
  last_message_sanitized: string | null;
  details: Record<string, unknown>;
  opened_at: string;
  last_seen_at: string;
  occurrences: number;
  first_execution_id: string | null;
  last_execution_id: string | null;
  resolved_at: string | null;
  resolution_reason: string | null;
  resolution_label: string | null;
  resolution_comment: string | null;
  resolved_by: string | null;
  resolved_execution_id: string | null;
  duration_seconds: number | null;
  acknowledged: boolean;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  ack_comment: string | null;
  last_notified_at: string | null;
  notify_count: number;
  manual_resolvable: boolean;
}

export interface IncidentEvent {
  id: number;
  event_type: string;
  actor: string;
  message: string | null;
  execution_id: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export interface Delivery {
  id: number;
  channel_id: number;
  channel_name: string | null;
  incident_id?: number | null;
  incident_title?: string | null;
  category?: string | null;
  transition: "opened" | "resolved" | "reminder" | "test";
  status: "pending" | "sending" | "delivered" | "failed" | "skipped";
  attempts: number;
  last_error: string | null;
  last_status_code: number | null;
  created_at: string;
  delivered_at: string | null;
  next_attempt_at: string | null;
}

export interface IncidentDetail extends Incident {
  events: IncidentEvent[];
  deliveries: Delivery[];
  executions: {
    execution_id: string;
    installation_id: string | null;
    installation_name: string | null;
    status: ExecutionStatus;
    failure_stage: string | null;
    started_at: string | null;
    finished_at: string | null;
    duration_ms: number | null;
    rows_loaded: number | null;
    error_code: string | null;
    error_message_sanitized: string | null;
  }[];
  current_health: (TaskHealth | InstallationHealth) | null;
}

export interface IncidentBadge {
  open_unacknowledged: number;
  open_total: number;
  serious_unacknowledged: number;
}

export interface NotificationChannel {
  id: number;
  name: string;
  kind: "webhook" | "log";
  url_display: string;
  has_url: boolean;
  has_secret: boolean;
  encrypted: boolean;
  is_enabled: boolean;
  min_severity: Severity;
  group_id: number | null;
  group_name: string | null;
  categories: IncidentCategory[];
  notify_on_open: boolean;
  notify_on_resolve: boolean;
  reminder_interval_minutes: number;
  timeout_seconds: number;
  verify_tls: boolean;
  pending: number;
  failed: number;
  last_delivery_at: string | null;
  created_at: string;
  updated_at: string;
}

// ── Inventario estructural (sección 19) ─────────────────────────────────────
export type MonitoredKind = "dwh" | "source";
export type MonitoredState = "awaiting_first_snapshot" | "baseline_pending" | "monitoring";
export type VerificationStatus = "never" | "verified" | "partial" | "unverifiable";
export type EffectiveStatus = VerificationStatus | "disabled" | "duplicate";
export type ObjectType = "table" | "view" | "matview" | "foreign_table";
export type ChangeKind = "object_added" | "object_removed" | "object_modified";
export type ChangeStatus = "pending" | "acknowledged" | "superseded" | "reverted" | "out_of_scope";
export type Attribution = "client" | "nexus";

export interface MonitoredLink {
  group_id: number;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
}

export interface MonitoredDatabase {
  allowed_actions?: AllowedActions;
  id: number;
  kind: MonitoredKind;
  engine: string;
  identity_key: string;
  display_name: string;
  group_id: number | null;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
  enabled: boolean;
  scan_interval_seconds: number;
  schema_include: string[];
  schema_exclude: string[];
  default_schema_exclude: string[];
  view_definitions_enabled: boolean;
  engine_identity: string | null;
  engine_identity_strength: string | null;
  duplicate_of_id: number | null;
  allow_engine_duplicate: boolean;
  engine_identity_weak: string | null;
  lease_installation_id: string | null;
  lease_installation_name: string | null;
  lease_active: boolean;
  lease_until: string | null;
  scan_requested_at: string | null;
  state: MonitoredState;
  verification_status: VerificationStatus;
  effective_status: EffectiveStatus;
  stale: boolean;
  last_attempt_at: string | null;
  last_verified_at: string | null;
  last_verified_snapshot_id: number | null;
  last_reason_code: string | null;
  baseline_version: number;
  baseline_approved_at: string | null;
  baseline_approved_by: string | null;
  pending_changes: number;
  baseline_objects: number;
  observed_objects: number;
  links: MonitoredLink[];
  created_at: string;
  warning?: string;
}

export interface InventorySnapshot {
  id: number;
  installation_id: string | null;
  installation_name: string | null;
  captured_at: string | null;
  received_at: string;
  status: "complete" | "partial" | "unreliable";
  reason_code: string | null;
  object_count: number;
  schemas_verified: string[];
  schemas_unverifiable: { schema_name: string; reason: string }[];
  agent_version: string;
  server_version: string;
  processing: Record<string, unknown>;
}

export interface MonitoredEvent {
  id: number;
  event_type: string;
  actor: string;
  message: string | null;
  data: Record<string, unknown>;
  created_at: string;
}

export interface MonitoredDatabaseDetail extends MonitoredDatabase {
  snapshots: InventorySnapshot[];
  events: MonitoredEvent[];
  config_current: boolean;
}

export interface ColumnStructure {
  type: string;
  not_null: boolean;
  default: string | null;
  [k: string]: unknown;
}

export interface ObjectStructure {
  columns?: Record<string, ColumnStructure>;
  constraints?: Record<string, { type: string; definition: string }>;
  indexes?: Record<string, { unique: boolean; definition: string }>;
  definition_hash?: string;
  [k: string]: unknown;
}

export interface BaselineItem {
  schema_name: string;
  name: string;
  type: ObjectType;
  fingerprint: string;
  columns: number;
  constraints: number;
  indexes: number;
  has_definition_hash: boolean;
  structure: ObjectStructure;
  nexus_catalog_match: boolean;
}

export interface BaselineResponse {
  view: "approved" | "proposal";
  state: MonitoredState;
  total: number;
  items: BaselineItem[];
  snapshot: { id: number; status: string; received_at: string; captured_at: string | null; schemas_verified: string[]; schemas_unverifiable: { schema_name: string; reason: string }[]; object_count: number } | null;
  baseline_version: number;
}

export interface StructuralDiff {
  kind: string;
  item: string | null;
  before: unknown;
  after: unknown;
}

export interface Evidence {
  type: "nexus_execution" | "nexus_catalog";
  note: string;
  execution_id?: string;
  task_id?: number;
  installation_id?: string | null;
  finished_at?: string | null;
  action?: string;
  columns?: string[];
  object_catalog_id?: number;
  object_name?: string;
  company_id?: number;
}

export interface StructuralChange {
  allowed_actions?: AllowedActions;
  id: number;
  monitored_database_id: number;
  schema_name: string;
  object_name: string;
  object_type: ObjectType;
  change_kind: ChangeKind;
  change_types: string[];
  baseline_fingerprint: string | null;
  observed_fingerprint: string;
  first_detected_at: string;
  last_observed_at: string;
  observation_count: number;
  status: ChangeStatus;
  status_changed_at: string;
  supersedes_id: number | null;
  superseded_by_id: number | null;
  attribution: Attribution | null;
  ack_by: string | null;
  ack_at: string | null;
  ack_comment: string | null;
  ticket_ref: string | null;
  reclassified_at: string | null;
  reclassified_by: string | null;
  row_version: number;
  evidence: Evidence[];
  evidence_count: number;
  baseline_version: number;
  database_kind: MonitoredKind;
  database_name: string;
  group_id: number | null;
  group_name: string | null;
  company_id: number | null;
  company_name: string | null;
}

export interface StructuralChangeDetail extends StructuralChange {
  diffs: StructuralDiff[];
  previous_structure: ObjectStructure | null;
  current_structure: ObjectStructure | null;
  previous_definition_hash: string | null;
  current_definition_hash: string | null;
  definitions_stored: boolean;
  definitions_viewable: boolean;
  events: MonitoredEvent[];
  object_history: { id: number; change_kind: ChangeKind; status: ChangeStatus; first_detected_at: string; attribution: Attribution | null; ack_at: string | null }[];
  database_state: { state: MonitoredState; verification_status: VerificationStatus; last_verified_at: string | null };
  labels: Record<string, string>;
}

export interface StructureBadge {
  pending_changes: number;
  unverifiable_databases: number;
  awaiting_baseline: number;
}

export interface InventorySummary {
  databases: number;
  by_status: Partial<Record<EffectiveStatus, number>>;
  pending_changes: number;
  awaiting_baseline: number;
  unverifiable: number;
  permissions: Record<string, string>;
}
