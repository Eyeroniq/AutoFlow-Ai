# FlowForge AI — Web

Next.js 16 (App Router) + TypeScript + Tailwind CSS v4, with React Flow for the canvas,
Zustand for editor state, TanStack Query for server data, and React Hook Form + Zod for
forms. See the [root README](../../README.md) for running the full stack, the editor's
features, and the frontend architecture.

## Running on the host

```bash
npm install
npm run dev        # http://localhost:3000, expects the API at http://localhost:8000
```

Point it at a different API by creating `apps/web/.env.local` with
`NEXT_PUBLIC_API_URL=http://localhost:8001` (Next.js only reads env files from this directory).

## Layout

| Path                              | Purpose                                                                 |
| --------------------------------- | ----------------------------------------------------------------------- |
| `src/app/`                        | Routes: `/`, `/login`, `/register`, `/dashboard`, `/pipelines/[id]`, `/executions`, `/executions/[id]`, `/integrations` |
| `src/app/providers.tsx`           | TanStack Query client (errors become toasts unless `meta.silent`)       |
| `src/features/editor/`            | The editor: `store.ts` (graph, undo/redo, save state machine), `canvas.tsx`, `flow-node.tsx`, `node-library.tsx`, `config-panel.tsx` + `config-form.tsx` (schema forms), `schema-form.ts` (JSON Schema → fields + Zod), `references.ts` + `reference-field.tsx` (`{{` autocomplete), `hooks.ts` (autosave, live validation, shortcuts), `run-controller.tsx` + `run-panel.tsx` |
| `src/features/runs/`              | `execution-socket.ts` (WebSocket client), `run-state.ts` (event reducer), `use-live-execution.ts`, `node-timeline.tsx` |
| `src/features/dashboard/`, `executions/`, `integrations/` | The other pages                               |
| `src/components/`                 | App shell (auth guard, header, user menu), node icons, UI primitives (button, dialog, menu, toast, status) |
| `src/lib/api.ts`                  | Typed fetch client for the FastAPI backend, with token refresh          |
| `src/lib/types.ts`                | API types                                                               |
| `e2e/`                            | Playwright end-to-end tests against the running stack                  |

## Scripts

- `npm run dev`: dev server (Turbopack on the host; the Docker service uses webpack for polling)
- `npm run build`: production build
- `npm run lint`: ESLint
- `npm test`: Vitest unit tests (`src/**/*.test.ts`)
- `npx playwright test`: end-to-end tests. Needs the Docker stack running and seeded, and
  `npx playwright install chromium` once. The run test calls Gemini and sends one real email.
