// Mirrors the Pydantic schemas in apps/api/app/schemas (and flowforge_engine's graph models).

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface User {
  id: string;
  email: string;
  full_name: string;
  created_at: string;
  updated_at: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

export interface RegisterPayload {
  email: string;
  password: string;
  full_name: string;
}

// --- node catalog (GET /api/nodes) ---------------------------------------------------------

/** A JSON Schema object as produced by Pydantic's model_json_schema(). */
export interface JsonSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  default?: unknown;
  enum?: unknown[];
  const?: unknown;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  additionalProperties?: boolean | JsonSchema;
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  maxItems?: number;
  pattern?: string;
  /** "file-ref": a file reference; the editor adds an upload/choose-file picker. */
  format?: string;
}

export interface NodeType {
  type: string;
  category: string;
  group: string;
  label: string;
  description: string;
  icon: string;
  /** The Celery queue whose workers run it: "default", "llm", or "ocr". */
  queue: string;
  /** Runs wherever the run is (Input, Output, Text, Condition): never a queue hand-off. */
  portable: boolean;
  interruptible: boolean;
  branches: string[];
  has_input: boolean;
  /** Per-item template fields (For Each's prompt, Join's template): {{item}} and {{index}} work there. */
  item_fields: string[];
  produces_final_output: boolean;
  config_schema: JsonSchema;
  output_schema: JsonSchema | null;
  output_keys: string[] | null;
}

// --- workflows --------------------------------------------------------------------------------

export type WorkflowStatus = "draft" | "active" | "archived";
export type VariableType = "workflow" | "environment" | "node_output" | "user_input" | "system";

export interface ApiGraphNode {
  id: string;
  type: string;
  label?: string | null;
  position: { x: number; y: number };
  config: Record<string, unknown>;
  // Editor-only fields; the API keeps unknown node keys as-is.
  description?: string | null;
  collapsed?: boolean;
}

export interface ApiGraphEdge {
  id?: string | null;
  source: string;
  target: string;
  source_handle?: string | null;
  target_handle?: string | null;
}

export interface WorkflowVariable {
  key: string;
  value: string | null;
  type: VariableType;
}

export interface WorkflowGraph {
  nodes: ApiGraphNode[];
  edges: ApiGraphEdge[];
  variables: WorkflowVariable[];
}

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string | null;
  status: WorkflowStatus;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowListItem extends WorkflowSummary {
  node_count: number;
  last_execution: {
    id: string;
    status: ExecutionStatus;
    created_at: string;
    started_at: string | null;
    finished_at: string | null;
  } | null;
  /** Saved triggers: which are on, and which the failure limit switched off. */
  triggers: { type: TriggerType; enabled: boolean; auto_disabled: boolean }[];
}

export interface Workflow extends WorkflowSummary {
  graph: WorkflowGraph;
}

export interface WorkflowUpdate {
  name?: string;
  description?: string | null;
  status?: WorkflowStatus;
  graph?: WorkflowGraph;
}

export interface ValidationIssue {
  code: string;
  message: string;
  node_id: string | null;
  edge_id: string | null;
  field: string | null;
}

export interface WorkflowValidation {
  valid: boolean;
  errors: ValidationIssue[];
}

export interface NodeTestRequest {
  config?: Record<string, unknown>;
  upstream_outputs?: Record<string, Record<string, unknown>>;
  variables?: Record<string, unknown>;
  inputs?: Record<string, unknown>;
}

export interface NodeTestResult {
  node_key: string;
  node_type: string;
  label: string;
  status: "success" | "failed" | "skipped";
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
}

// --- executions -------------------------------------------------------------------------------

export type ExecutionStatus = "pending" | "running" | "success" | "failed" | "stopped";
/** What started a run. "api" is the old name for webhook runs (before triggers existed). */
export type ExecutionTrigger = "manual" | "schedule" | "email" | "webhook" | "event" | "api";
export type NodeExecutionStatus = "pending" | "running" | "success" | "failed" | "skipped";

export interface ExecutionSummary {
  id: string;
  workflow_id: string;
  status: ExecutionStatus;
  trigger: ExecutionTrigger;
  /** The schedule, email, or webhook trigger that started it; null for manual runs. */
  trigger_id: string | null;
  triggered_by_user_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  queue: string | null;
  worker_hostname: string | null;
  heartbeat_at: string | null;
  stop_requested_at: string | null;
  duration_ms: number | null;
  /** Times the run moved to another queue's workers. */
  segment: number;
  /** Set while the run waits for a worker of `queue` after a hand-off. */
  handoff_at: string | null;
  /** The deployment whose endpoint started the run (trigger "webhook"). */
  deployment_id: string | null;
}

export interface ExecutionListItem extends ExecutionSummary {
  workflow_name: string;
}

export interface NodeExecution {
  id: string;
  node_id: string | null;
  node_key: string;
  position: number | null;
  node_type: string;
  node_label: string;
  status: NodeExecutionStatus;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  /** Where the node ran (null for skipped nodes, and the queue for in-request runs). */
  queue: string | null;
  worker_hostname: string | null;
}

export interface ExecutionDetail extends ExecutionSummary {
  inputs: Record<string, unknown> | null;
  final_output: Record<string, unknown> | null;
  node_executions: NodeExecution[];
}

export interface ExecutionAccepted {
  execution_id: string;
  workflow_id: string;
  status: ExecutionStatus;
  queue: string;
  links: { execution: string; events: string; stop: string };
}

// --- live events (WS /ws/executions/{id}) --------------------------------------------------

export type ExecutionEvent =
  | { type: "snapshot"; seq: number; resync: boolean; execution: ExecutionDetail }
  | { type: "execution.started"; seq: number; status: "running"; started_at: string | null; worker: string }
  | {
      type: "node.started";
      seq: number;
      node_key: string;
      started_at: string | null;
      worker?: string | null;
      queue?: string | null;
    }
  | {
      type: "execution.handoff";
      seq: number;
      from_queue: string | null;
      to_queue: string;
      segment: number;
      worker: string;
    }
  | { type: "execution.resumed"; seq: number; segment: number; worker: string; queue: string | null }
  | { type: "node.token"; seq: number; node_key: string; text: string; provider: string }
  | {
      type: "node.succeeded" | "node.failed" | "node.skipped";
      seq: number;
      node_key: string;
      status: NodeExecutionStatus;
      input?: Record<string, unknown> | null;
      output?: Record<string, unknown> | null;
      error?: string | null;
      reason?: string | null;
      started_at: string | null;
      finished_at: string | null;
      duration_ms: number | null;
    }
  | {
      type: "execution.finished";
      seq: number | null;
      status: ExecutionStatus;
      final_output: Record<string, unknown> | null;
      error: string | null;
      started_at: string | null;
      finished_at: string | null;
      duration_ms: number | null;
      replayed?: boolean;
    }
  | { type: "heartbeat" | "pong"; timestamp: string }
  | { type: "error"; code: number; message: string };

// --- files ---------------------------------------------------------------------------------------

export interface UploadedFile {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  created_at: string;
}

/** What an Input node of type file outputs (and document nodes accept as `file`). */
export interface FileDescription {
  file_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
}

// --- deployments ---------------------------------------------------------------------------------

export interface DeploymentInput {
  node_id: string;
  label: string;
  /** The key callers send in `inputs`. */
  name: string;
  type: "text" | "number" | "json" | "file";
  /** The caller must send it (it has no default). */
  required: boolean;
  default: unknown;
}

export interface DeploymentOutput {
  node_id: string;
  label: string;
  name: string;
}

export interface Deployment {
  id: string;
  workflow_id: string;
  name: string;
  version: number;
  workflow_version: number;
  /** Relative to the API's base URL: /api/v1/deployments/{id}/run */
  endpoint: string;
  api_key_prefix: string;
  inputs: DeploymentInput[];
  outputs: DeploymentOutput[];
  key_created_at: string;
  deployed_at: string;
  created_at: string;
  updated_at: string;
}

export interface DeploymentWithKey extends Deployment {
  /** Only in the response that issued it; never retrievable again. */
  api_key: string | null;
}

// --- integrations ------------------------------------------------------------------------------

export interface Integration {
  provider: string;
  label: string;
  kind: "llm" | "email" | "messaging" | "search";
  connected: boolean;
  source: "user" | "server" | "none";
  status: "connected" | "disconnected" | "error";
  masked: Record<string, unknown> | null;
  connected_at: string | null;
  last_test: { success: boolean; latency_ms: number; error: string | null; tested_at: string } | null;
  default_model: string | null;
  get_key_url: string | null;
}

export interface IntegrationConnect {
  api_key?: string;
  base_url?: string;
  model?: string;
  email?: string;
  app_password?: string;
  from_name?: string;
  smtp_host?: string;
  smtp_port?: number;
  smtp_security?: "auto" | "starttls" | "ssl";
  imap_host?: string;
  imap_port?: number;
  bot_token?: string;
  chat_id?: string;
  webhook_url?: string;
}

export interface IntegrationTestResult {
  provider: string;
  success: boolean;
  source: "user" | "server" | "none";
  latency_ms: number;
  error: string | null;
  details: Record<string, unknown>;
  tested_at: string;
}

// --- triggers ------------------------------------------------------------------------------------

export type TriggerType = "schedule" | "email" | "webhook";

export interface ScheduleConfig {
  cron: string;
  timezone: string;
  inputs?: Record<string, unknown>;
}

export interface EmailTriggerConfig {
  folder: string;
  from_address: string | null;
  subject: string | null;
  unread_only: boolean;
  poll_minutes: number;
  input_name: string;
  max_per_poll: number;
  mark_as_read: boolean;
  max_body_chars: number;
}

export interface TriggerLastRun {
  execution_id: string;
  status: ExecutionStatus;
  created_at: string;
  finished_at: string | null;
  error_message: string | null;
}

export interface Trigger {
  type: TriggerType;
  id: string | null;
  configured: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
  /** Schedule: the next fire time. Email: the next mailbox check. */
  next_run_at: string | null;
  upcoming: string[];
  last_fired_at: string | null;
  last_run: TriggerLastRun | null;
  consecutive_failures: number;
  /** Set when the consecutive-failure limit switched the trigger off. */
  auto_disabled_at: string | null;
  disabled_reason: string | null;
  last_error: string | null;
  last_error_at: string | null;
  warnings: string[];
  webhook: { deployment_id: string; endpoint: string; api_key_prefix: string; deployed_version: number; behind: boolean } | null;
  mailbox: { folder: string | null; last_uid: number | null; last_poll_at: string | null } | null;
}

export interface TriggerSettings {
  max_runs_per_hour: number;
  max_consecutive_failures: number;
}

export interface TriggersResponse {
  workflow_id: string;
  settings: TriggerSettings;
  runs_last_hour: number;
  triggers: Trigger[];
}

export interface SchedulePreview {
  valid: boolean;
  error: string | null;
  cron: string | null;
  interval: boolean | null;
  next: string[];
}

export interface EmailCheckResult {
  trigger_id: string;
  polled: boolean;
  found: number;
  reset: boolean;
  error: string | null;
  runs: { outcome: string; execution_id: string | null; event_key: string; detail: string | null }[];
}

// --- templates -----------------------------------------------------------------------------------

export interface TemplateRequirement {
  providers: string[];
  label: string;
  why: string | null;
  satisfied: boolean;
  using: string | null;
}

export interface Template {
  slug: string;
  name: string;
  description: string | null;
  category: string;
  node_types: string[];
  requirements: TemplateRequirement[];
  /** Every requirement has a credential (yours or the server's). */
  ready: boolean;
  triggers: { type: TriggerType; config: Record<string, unknown> }[];
}
