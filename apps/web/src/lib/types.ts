import type { Schemas } from "@flowforge/shared";

// Mirrors the Pydantic schemas in apps/api/app/schemas (and flowforge_engine's graph models).

export type TokenResponse = Schemas["TokenResponse"];

export type User = Schemas["UserRead"];

export type LoginPayload = Schemas["LoginRequest"];

export type RegisterPayload = Schemas["RegisterRequest"];

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

export type WorkflowStatus = Schemas["WorkflowStatus"];
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

export type WorkflowListItem = Schemas["WorkflowListItem"];

/** The graph is typed strictly here: the API's own schema lists every graph field as optional because the same model is also accepted as a request body. */
export type Workflow = Omit<Schemas["WorkflowRead"], "graph"> & { graph: WorkflowGraph };

export type WorkflowUpdate = Omit<Schemas["WorkflowUpdate"], "graph"> & { graph?: WorkflowGraph };

/** Plus "request_failed", which the editor raises itself when validation could not be requested. */
export type ValidationIssue = Omit<Schemas["ValidationIssue"], "code"> & { code: Schemas["ValidationIssue"]["code"] | "request_failed" };

export type WorkflowValidation = Schemas["WorkflowValidation"];

export type NodeTestRequest = Schemas["NodeTestRequest"];

export type NodeTestResult = Omit<Schemas["NodeTestResult"], "status"> & { status: NodeExecutionStatus };

// --- executions -------------------------------------------------------------------------------

export type ExecutionStatus = Schemas["ExecutionStatus"];
/** What started a run. "api" is the old name for webhook runs (before triggers existed). */
export type ExecutionTrigger = Schemas["ExecutionTrigger"];
export type NodeExecutionStatus = Schemas["NodeExecutionStatus"];

export type ExecutionSummary = Schemas["ExecutionSummary"];

export type ExecutionListItem = Schemas["ExecutionListItem"];

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

/** Hand-written: the API model is also a request body, so its schema lists every field as optional. */
export interface PrivacySettings {
  mask_stored_io: boolean;
  detect_personal_data: boolean;
  allowlist: string[];
}

export interface PrivacyGuard {
  mode: string;
  action: "clean" | "warned" | "redacted" | "blocked" | "off";
  total: number;
  by_type: Record<string, number>;
  by_category: Record<string, number>;
  fields: string[];
}

export interface PrivacyReport {
  total: number;
  by_type: Record<string, number>;
  by_category: Record<string, number>;
  guard_actions: Record<string, number>;
  masked: boolean;
  nodes: {
    node_key: string;
    label: string;
    node_type: string;
    total: number;
    by_type: Record<string, number>;
    by_category: Record<string, number>;
    guard: PrivacyGuard | null;
  }[];
}

/** The API describes `graph` and `privacy_report` as free-form objects; their shapes are typed here. */
export type ExecutionDetail = Omit<Schemas["ExecutionDetail"], "graph" | "privacy_report"> & {
  /** The graph exactly as it ran (what Replay draws). */
  graph?: { nodes: unknown[]; edges: unknown[] } | null;
  privacy_report?: PrivacyReport | null;
};

export type ExecutionAccepted = Schemas["ExecutionAccepted"];

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

export type UploadedFile = Schemas["FileRead"];

export type EmbeddingProvider = "gemini" | "openai" | "ollama" | "mock";

export type KnowledgeBase = Schemas["KnowledgeBaseRead"];

export type KnowledgeBaseCreate = Schemas["KnowledgeBaseCreate"];

export type DocumentStatus = Schemas["DocumentStatus"];

export type KnowledgeDocument = Schemas["DocumentRead"];

export type KnowledgeSearchHit = Schemas["SearchHit"];

/** What an Input node of type file outputs (and document nodes accept as `file`). */
export interface FileDescription {
  file_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
}

// --- deployments ---------------------------------------------------------------------------------

export type DeploymentInput = Schemas["DeploymentInput"];

export type DeploymentOutput = Schemas["DeploymentOutput"];

export type Deployment = Schemas["DeploymentRead"];

export type DeploymentWithKey = Schemas["DeploymentWithKey"];

// --- integrations ------------------------------------------------------------------------------

export type Integration = Omit<Schemas["IntegrationRead"], "last_test"> & {
  last_test: { success: boolean; latency_ms: number; error: string | null; tested_at: string } | null;
};

export type IntegrationConnect = Schemas["ConnectRequest"];

export type IntegrationTestResult = Schemas["IntegrationTestResult"];

// --- triggers ------------------------------------------------------------------------------------

export type SystemInfo = Schemas["SystemInfo"];
export type AutomationsState = Schemas["AutomationsState"];

export type TriggerType = Schemas["TriggerType"];

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

export type TriggerLastRun = Schemas["TriggerLastRun"];

export type Trigger = Omit<Schemas["TriggerRead"], "mailbox"> & {
  mailbox: { folder: string | null; last_uid: number | null; last_poll_at: string | null } | null;
};

export type TriggerSettings = Schemas["TriggerSettings"];

export type TriggersResponse = Omit<Schemas["TriggersRead"], "triggers"> & { triggers: Trigger[] };

export type SchedulePreview = Schemas["SchedulePreview"];

export interface EmailCheckResult {
  trigger_id: string;
  polled: boolean;
  found: number;
  reset: boolean;
  error: string | null;
  runs: { outcome: string; execution_id: string | null; event_key: string; detail: string | null }[];
}

// --- templates -----------------------------------------------------------------------------------

export type TemplateRequirement = Schemas["TemplateRequirement"];

export type Template = Schemas["TemplateRead"];
