import path from "node:path";

import { type APIRequestContext, expect, type Page } from "@playwright/test";

import type { TokenResponse, Workflow, WorkflowGraph, WorkflowListItem } from "../src/lib/types";

export const API_URL = process.env.E2E_API_URL ?? "http://localhost:8000";
// The seed account (apps/api/app/db/seed.py).
export const DEMO_EMAIL = process.env.E2E_EMAIL ?? "demo@flowforge.ai";
export const DEMO_PASSWORD = process.env.E2E_PASSWORD ?? "demo1234";
export const DEMO_PIPELINE = "Demo: Summarize and email";

const SCREENSHOTS = path.resolve(__dirname, "../../../docs/screenshots");

export function screenshotPath(name: string) {
  return path.join(SCREENSHOTS, `${name}.png`);
}

const sessions = new Map<string, { tokens: TokenResponse; at: number }>();

/** A thin client for the real API, used for setup, cleanup, and checking results. */
export class Api {
  private constructor(
    private readonly request: APIRequestContext,
    readonly tokens: TokenResponse,
  ) {}

  /**
   * Logs in once per test worker and reuses the tokens (valid 30 minutes): the API allows
   * only AUTH_RATE_LIMIT logins a minute per client.
   */
  static async login(request: APIRequestContext, email = DEMO_EMAIL, password = DEMO_PASSWORD) {
    const cached = sessions.get(email);
    if (cached && Date.now() - cached.at < 20 * 60_000) return new Api(request, cached.tokens);
    const response = await request.post(`${API_URL}/api/auth/login`, { data: { email, password } });
    expect(response.status(), "demo login (seed it first: docker compose exec api python -m app.db.seed)").toBe(200);
    const tokens = (await response.json()) as TokenResponse;
    sessions.set(email, { tokens, at: Date.now() });
    return new Api(request, tokens);
  }

  private get headers() {
    return { Authorization: `Bearer ${this.tokens.access_token}` };
  }

  async call<T>(method: "GET" | "POST" | "PUT" | "DELETE", url: string, data?: unknown): Promise<{ status: number; body: T }> {
    const response = await this.request.fetch(`${API_URL}${url}`, { method, headers: this.headers, data });
    const text = await response.text();
    return { status: response.status(), body: (text ? JSON.parse(text) : null) as T };
  }

  async workflows() {
    return (await this.call<WorkflowListItem[]>("GET", "/api/workflows")).body;
  }

  async workflow(id: string) {
    return (await this.call<Workflow>("GET", `/api/workflows/${id}`)).body;
  }

  async demoWorkflow() {
    const demo = (await this.workflows()).find((w) => w.name === DEMO_PIPELINE);
    expect(demo, `"${DEMO_PIPELINE}" is seeded for ${DEMO_EMAIL}`).toBeTruthy();
    return this.workflow(demo!.id);
  }

  async createWorkflow(name: string, graph: WorkflowGraph) {
    const { status, body } = await this.call<Workflow>("POST", "/api/workflows", { name });
    expect(status).toBe(201);
    await this.putGraph(body.id, graph);
    return this.workflow(body.id);
  }

  async duplicate(id: string) {
    const { status, body } = await this.call<Workflow>("POST", `/api/workflows/${id}/duplicate`);
    expect(status).toBe(201);
    return body;
  }

  async putGraph(id: string, graph: WorkflowGraph) {
    const { status } = await this.call("PUT", `/api/workflows/${id}`, { graph });
    expect(status).toBe(200);
  }

  async remove(id: string) {
    await this.call("DELETE", `/api/workflows/${id}`);
  }
}

/** Signs the page in as `api`'s user by seeding the tokens the web app keeps in localStorage. */
export async function signIn(page: Page, api: Api) {
  await page.addInitScript((tokens) => {
    window.localStorage.setItem("flowforge.access_token", tokens.access_token);
    window.localStorage.setItem("flowforge.refresh_token", tokens.refresh_token);
  }, api.tokens);
}

export async function openEditor(page: Page, workflowId: string) {
  await page.goto(`/pipelines/${workflowId}`);
  await expect(page.getByTestId("editor")).toBeVisible();
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
}

export function node(page: Page, id: string) {
  return page.getByTestId(`node-${id}`);
}

export async function expectSaved(page: Page) {
  await expect(page.getByTestId("save-state")).toHaveText("Saved", { timeout: 15_000 });
}

/** Drags from a node's output handle to another node's input handle. */
export async function connect(page: Page, source: string, target: string, sourceHandle?: string) {
  const handleSelector = sourceHandle
    ? `.react-flow__handle.source[data-nodeid="${source}"][data-handleid="${sourceHandle}"]`
    : `.react-flow__handle.source[data-nodeid="${source}"]`;
  const from = page.locator(handleSelector);
  const to = page.locator(`.react-flow__handle.target[data-nodeid="${target}"]`);
  const a = (await from.boundingBox())!;
  const b = (await to.boundingBox())!;
  await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
  await page.mouse.down();
  await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 5 });
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 5 });
  await page.mouse.up();
}

/** An edge by its id. Assert with toBeAttached(): a straight edge's box has no height, so it never counts as "visible". */
export function edge(page: Page, id: string) {
  return page.locator(`.react-flow__edge[data-id="${id}"]`);
}

/** Clicks a straight edge halfway between its handles (it has no box to click). */
export async function clickEdge(page: Page, source: string, target: string) {
  const a = (await page.locator(`.react-flow__handle.source[data-nodeid="${source}"]`).first().boundingBox())!;
  const b = (await page.locator(`.react-flow__handle.target[data-nodeid="${target}"]`).boundingBox())!;
  await page.mouse.click((a.x + a.width / 2 + b.x + b.width / 2) / 2, (a.y + a.height / 2 + b.y + b.height / 2) / 2);
}

/** Hides the Next.js dev-mode badge so it doesn't cover the UI in screenshots. */
export async function hideDevOverlay(page: Page) {
  await page.addStyleTag({ content: "nextjs-portal { display: none !important; }" });
}

interface ReadEmail {
  subject?: string;
  from?: string;
  message_id?: string;
}

/**
 * Waits for an email whose subject contains `subject` to reach the SMTP_USER inbox,
 * reading it over IMAP with the real Gmail Read node (tested in isolation).
 */
export async function waitForEmail(api: Api, subject: string, timeoutMs = 90_000): Promise<ReadEmail> {
  const reader = await api.createWorkflow(`E2E inbox check ${subject}`.slice(0, 200), {
    nodes: [
      {
        id: "inbox",
        type: "gmail_read",
        label: "Inbox",
        position: { x: 0, y: 0 },
        config: { subject, unread_only: false, since_days: 1, max_results: 5, include_body: false },
      },
    ],
    edges: [],
    variables: [],
  });
  try {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const { status, body } = await api.call<{ status: string; error: string | null; output: { count: number; emails: ReadEmail[] } | null }>(
        "POST",
        `/api/workflows/${reader.id}/nodes/inbox/test`,
        {},
      );
      expect(status).toBe(200);
      expect(body.error, "reading the inbox over IMAP").toBeNull();
      if (body.output && body.output.count > 0) return body.output.emails[0];
      if (Date.now() > deadline) throw new Error(`No email with subject containing "${subject}" arrived within ${timeoutMs / 1000}s`);
      await new Promise((resolve) => setTimeout(resolve, 5_000));
    }
  } finally {
    await api.remove(reader.id);
  }
}
