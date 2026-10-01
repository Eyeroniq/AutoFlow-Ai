import { API_URL } from "./config";
import { isTokenExpired, tokenStorage } from "./token-storage";
import type {
  Deployment,
  DeploymentWithKey,
  EmailCheckResult,
  ExecutionAccepted,
  ExecutionDetail,
  ExecutionListItem,
  ExecutionStatus,
  ExecutionTrigger,
  Integration,
  IntegrationConnect,
  IntegrationTestResult,
  KnowledgeBase,
  KnowledgeBaseCreate,
  KnowledgeDocument,
  KnowledgeSearchHit,
  LoginPayload,
  NodeTestRequest,
  NodeTestResult,
  NodeType,
  RegisterPayload,
  SchedulePreview,
  Template,
  TokenResponse,
  TriggerSettings,
  TriggersResponse,
  TriggerType,
  UploadedFile,
  User,
  ValidationIssue,
  Workflow,
  WorkflowGraph,
  WorkflowListItem,
  WorkflowUpdate,
  WorkflowValidation,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** The graph problems a refused run (422 with `detail.errors`) reports; [] for any other error. */
export function runIssues(error: unknown): ValidationIssue[] {
  if (!(error instanceof ApiError) || error.status !== 422) return [];
  const detail = (error.body as { detail?: unknown } | undefined)?.detail;
  const errors = detail && typeof detail === "object" ? (detail as { errors?: unknown }).errors : undefined;
  return Array.isArray(errors) ? (errors as ValidationIssue[]) : [];
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Attach the stored access token, refreshing it when it has expired or is rejected. */
  auth?: boolean;
  signal?: AbortSignal;
}

interface PydanticIssue {
  loc?: (string | number)[];
  msg?: string;
}

/** Turn FastAPI's `detail` (a string, or a list of validation issues) into one message. */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = (detail as PydanticIssue[])
      .map((issue) => {
        const field = issue.loc?.filter((part) => part !== "body").join(".");
        const msg = issue.msg?.replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  return null;
}

async function send(path: string, options: RequestOptions, token?: string | null): Promise<Response> {
  const { method = "GET", body, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    return await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, `Can't reach the API at ${API_URL}. Is the backend running?`);
  }
}

async function parse<T>(response: Response): Promise<T> {
  const data: unknown = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(response.status, describeDetail(detail) ?? `Request failed (${response.status})`, data);
  }
  return data as T;
}

let refreshInFlight: Promise<boolean> | null = null;

/**
 * Exchange the stored refresh token for a new token pair. Concurrent callers share one
 * request. Resolves false (and clears the session if the API rejected the token) when
 * no new access token could be obtained.
 */
export function refreshSession(): Promise<boolean> {
  refreshInFlight ??= doRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function doRefresh(): Promise<boolean> {
  const refreshToken = tokenStorage.getRefreshToken();
  if (!refreshToken || isTokenExpired(refreshToken, 0)) {
    tokenStorage.clear();
    return false;
  }

  let response: Response;
  try {
    response = await send("/api/auth/refresh", { method: "POST", body: { refresh_token: refreshToken } });
  } catch {
    // Network trouble isn't proof the session is dead; keep the tokens.
    return false;
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 422) tokenStorage.clear();
    return false;
  }
  tokenStorage.setTokens((await response.json()) as TokenResponse);
  return true;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!options.auth) return parse<T>(await send(path, options));

  // Skip a guaranteed 401 when the stored access token has visibly expired.
  const current = tokenStorage.getAccessToken();
  if ((!current || isTokenExpired(current)) && tokenStorage.getRefreshToken()) {
    await refreshSession();
  }

  const token = tokenStorage.getAccessToken();
  let response = await send(path, options, token);
  if (response.status === 401 && tokenStorage.getRefreshToken()) {
    // Another request may already have refreshed while this one was in flight.
    const refreshed = tokenStorage.getAccessToken() !== token || (await refreshSession());
    if (refreshed) response = await send(path, options, tokenStorage.getAccessToken());
  }
  return parse<T>(response);
}

/**
 * A current access token for places that can't go through request() (the WebSocket's
 * first-message auth): refreshed first when it has expired. Null when signed out.
 */
export async function getFreshAccessToken(): Promise<string | null> {
  const current = tokenStorage.getAccessToken();
  if ((!current || isTokenExpired(current)) && tokenStorage.getRefreshToken()) {
    await refreshSession();
  }
  return tokenStorage.getAccessToken();
}

const authed = <T>(path: string, options: Omit<RequestOptions, "auth"> = {}) =>
  request<T>(path, { ...options, auth: true });

/**
 * GET a file the API serves only to signed-in users (so a plain link can't carry the
 * token) and hand it to the browser as a download.
 */
export async function downloadAuthed(path: string, fallbackName: string): Promise<void> {
  let token = await getFreshAccessToken();
  let response = await send(path, {}, token);
  if (response.status === 401 && tokenStorage.getRefreshToken() && (await refreshSession())) {
    token = tokenStorage.getAccessToken();
    response = await send(path, {}, token);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(response.status, describeDetail((data as { detail?: unknown } | null)?.detail) ?? `Download failed (${response.status})`, data);
  }
  const name = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface UploadOptions {
  /** Called with 0..1 as the bytes go out. */
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

/**
 * POST /api/files with upload progress (fetch can't report it, so this uses XHR). Same
 * auth handling as request(): a fresh token first, and one refresh + retry on 401.
 */
export function uploadFile(file: File, options: UploadOptions = {}): Promise<UploadedFile> {
  return uploadTo<UploadedFile>("/api/files", file, options);
}

/** POST `file` as the multipart field `file` to `path`, with progress and one token refresh. */
async function uploadTo<T>(path: string, file: File, { onProgress, signal }: UploadOptions = {}): Promise<T> {
  const attempt = (token: string | null) =>
    new Promise<{ status: number; data: unknown }>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_URL}${path}`);
      xhr.setRequestHeader("Accept", "application/json");
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress?.(event.loaded / event.total);
      };
      xhr.onload = () => {
        let data: unknown = null;
        try {
          data = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch {
          // not JSON
        }
        resolve({ status: xhr.status, data });
      };
      xhr.onerror = () => reject(new ApiError(0, `Can't reach the API at ${API_URL}. Is the backend running?`));
      xhr.onabort = () => reject(new DOMException("The upload was cancelled", "AbortError"));
      signal?.addEventListener("abort", () => xhr.abort(), { once: true });
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });

  let result = await attempt(await getFreshAccessToken());
  if (result.status === 401 && tokenStorage.getRefreshToken() && (await refreshSession())) {
    onProgress?.(0);
    result = await attempt(tokenStorage.getAccessToken());
  }
  if (result.status < 200 || result.status >= 300) {
    const detail = (result.data as { detail?: unknown } | null)?.detail;
    throw new ApiError(result.status, describeDetail(detail) ?? `Upload failed (${result.status})`, result.data);
  }
  return result.data as T;
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const enc = encodeURIComponent;

export const api = {
  auth: {
    login: (payload: LoginPayload) =>
      request<TokenResponse>("/api/auth/login", { method: "POST", body: payload }),
    register: (payload: RegisterPayload) =>
      request<TokenResponse>("/api/auth/register", { method: "POST", body: payload }),
    me: (signal?: AbortSignal) => request<User>("/api/auth/me", { auth: true, signal }),
  },
  nodes: {
    list: () => authed<NodeType[]>("/api/nodes"),
  },
  workflows: {
    list: () => authed<WorkflowListItem[]>("/api/workflows"),
    get: (id: string, signal?: AbortSignal) => authed<Workflow>(`/api/workflows/${enc(id)}`, { signal }),
    create: (body: { name: string; description?: string | null }) =>
      authed<Workflow>("/api/workflows", { method: "POST", body }),
    update: (id: string, body: WorkflowUpdate) =>
      authed<Workflow>(`/api/workflows/${enc(id)}`, { method: "PUT", body }),
    remove: (id: string) => authed<null>(`/api/workflows/${enc(id)}`, { method: "DELETE" }),
    duplicate: (id: string) => authed<Workflow>(`/api/workflows/${enc(id)}/duplicate`, { method: "POST" }),
    /** Validate `graph` (unsaved edits) or, without it, the saved graph. */
    validate: (id: string, graph?: WorkflowGraph) =>
      authed<WorkflowValidation>(`/api/workflows/${enc(id)}/validate`, {
        method: "POST",
        body: graph ? { graph } : undefined,
      }),
    /** Queue a run: resolves with the 202 body. */
    run: (id: string, inputs: Record<string, unknown> = {}) =>
      authed<ExecutionAccepted>(`/api/workflows/${enc(id)}/run`, { method: "POST", body: { inputs } }),
    executions: (id: string, params: { limit?: number; offset?: number } = {}) =>
      authed<ExecutionListItem[]>(`/api/workflows/${enc(id)}/executions${query(params)}`),
    triggers: (id: string) => authed<TriggersResponse>(`/api/workflows/${enc(id)}/triggers`),
    /** Save a trigger's config and switch it on or off; resolves with every trigger. */
    saveTrigger: (id: string, type: TriggerType, body: { enabled: boolean; config?: Record<string, unknown> }) =>
      authed<TriggersResponse>(`/api/workflows/${enc(id)}/triggers/${type}`, { method: "PUT", body }),
    saveTriggerSettings: (id: string, body: TriggerSettings) =>
      authed<TriggersResponse>(`/api/workflows/${enc(id)}/trigger-settings`, { method: "PUT", body }),
    checkEmail: (id: string) => authed<EmailCheckResult>(`/api/workflows/${enc(id)}/triggers/email/check`, { method: "POST" }),
    testNode: (id: string, nodeKey: string, body: NodeTestRequest) =>
      authed<NodeTestResult>(`/api/workflows/${enc(id)}/nodes/${enc(nodeKey)}/test`, { method: "POST", body }),
  },
  triggers: {
    previewSchedule: (body: { cron: string; timezone: string; count?: number }) =>
      authed<SchedulePreview>("/api/triggers/schedule-preview", { method: "POST", body }),
  },
  templates: {
    list: () => authed<Template[]>("/api/templates"),
    use: (slug: string, timezone?: string) =>
      authed<Workflow>(`/api/templates/${enc(slug)}/use`, { method: "POST", body: { timezone } }),
  },
  executions: {
    list: (
      params: { limit?: number; offset?: number; status?: ExecutionStatus; workflow_id?: string; trigger?: ExecutionTrigger } = {},
    ) => authed<ExecutionListItem[]>(`/api/executions${query(params)}`),
    /** Download the final output: JSON as is, or CSV (one row per record, e.g. per entity). */
    downloadOutput: (id: string, format: "json" | "csv") =>
      downloadAuthed(`/api/executions/${enc(id)}/output?format=${format}`, `execution-${id.slice(0, 8)}-output.${format}`),
    get: (id: string, signal?: AbortSignal) => authed<ExecutionDetail>(`/api/executions/${enc(id)}`, { signal }),
    stop: (id: string) => authed<ExecutionDetail>(`/api/executions/${enc(id)}/stop`, { method: "POST" }),
  },
  deployments: {
    list: (params: { workflow_id?: string } = {}) => authed<Deployment[]>(`/api/deployments${query(params)}`),
    /** Deploy or redeploy the saved graph; `api_key` is set only on the first deploy. */
    deploy: (workflowId: string) =>
      authed<DeploymentWithKey>("/api/deployments", { method: "POST", body: { workflow_id: workflowId } }),
    /** A new API key (in `api_key`); the old one is revoked. The deployed graph doesn't change. */
    rotateKey: (deploymentId: string) =>
      authed<DeploymentWithKey>(`/api/deployments/${enc(deploymentId)}/rotate-key`, { method: "POST" }),
    /** Take the endpoint down (404 from now on); the deployment is kept for history. */
    undeploy: (deploymentId: string) => authed<Deployment>(`/api/deployments/${enc(deploymentId)}`, { method: "DELETE" }),
  },
  files: {
    list: () => authed<UploadedFile[]>("/api/files"),
    get: (id: string) => authed<UploadedFile>(`/api/files/${enc(id)}`),
    remove: (id: string) => authed<null>(`/api/files/${enc(id)}`, { method: "DELETE" }),
    upload: uploadFile,
  },
  knowledgeBases: {
    list: () => authed<KnowledgeBase[]>("/api/knowledge-bases"),
    get: (id: string) => authed<KnowledgeBase>(`/api/knowledge-bases/${enc(id)}`),
    create: (body: KnowledgeBaseCreate) => authed<KnowledgeBase>("/api/knowledge-bases", { method: "POST", body }),
    remove: (id: string) => authed<null>(`/api/knowledge-bases/${enc(id)}`, { method: "DELETE" }),
    documents: (id: string) => authed<KnowledgeDocument[]>(`/api/knowledge-bases/${enc(id)}/documents`),
    /** Upload a file; it's read, chunked, and embedded in the background (poll `documents`). */
    upload: (id: string, file: File, options?: UploadOptions) =>
      uploadTo<KnowledgeDocument>(`/api/knowledge-bases/${enc(id)}/documents`, file, options),
    retry: (id: string, documentId: string) =>
      authed<KnowledgeDocument>(`/api/knowledge-bases/${enc(id)}/documents/${enc(documentId)}/retry`, { method: "POST" }),
    removeDocument: (id: string, documentId: string) =>
      authed<null>(`/api/knowledge-bases/${enc(id)}/documents/${enc(documentId)}`, { method: "DELETE" }),
    search: (id: string, query: string, topK = 5) =>
      authed<{ query: string; results: KnowledgeSearchHit[] }>(`/api/knowledge-bases/${enc(id)}/search`, {
        method: "POST",
        body: { query, top_k: topK },
      }),
  },
  integrations: {
    list: () => authed<Integration[]>("/api/integrations"),
    connect: (provider: string, body: IntegrationConnect) =>
      authed<Integration>(`/api/integrations/${enc(provider)}/connect`, { method: "POST", body }),
    disconnect: (provider: string) => authed<null>(`/api/integrations/${enc(provider)}`, { method: "DELETE" }),
    test: (provider: string) =>
      authed<IntegrationTestResult>(`/api/integrations/${enc(provider)}/test`, { method: "POST" }),
  },
};
