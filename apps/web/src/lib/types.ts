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
}

export interface NodeType {
  type: string;
  category: string;
  group: string;
  label: string;
  description: string;
  icon: string;
  queue: string;
  interruptible: boolean;
  branches: string[];
  has_input: boolean;
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
export type NodeExecutionStatus = "pending" | "running" | "success" | "failed" | "skipped";

export interface ExecutionSummary {
  id: string;
  workflow_id: string;
  status: ExecutionStatus;
  trigger: string;
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
    }
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

// --- integrations ------------------------------------------------------------------------------

export interface Integration {
  provider: string;
  label: string;
  kind: "llm" | "email";
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
