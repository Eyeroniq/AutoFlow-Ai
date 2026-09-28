# FlowForge AI — Web

Next.js (App Router) + TypeScript + Tailwind CSS v4. See the [root README](../../README.md)
for running the full stack.

## Running on the host

```bash
npm install
npm run dev        # http://localhost:3000, expects the API at http://localhost:8000
```

Point it at a different API by creating `apps/web/.env.local` with
`NEXT_PUBLIC_API_URL=http://localhost:8001` (Next.js only reads env files from this directory).

## Layout

| Path                         | Purpose                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| `src/app/`                   | Routes: `/`, `/login`, `/register`, `/dashboard`               |
| `src/components/auth/`       | Login/register forms (React Hook Form + Zod)                   |
| `src/components/dashboard/`  | Protected dashboard placeholder                                |
| `src/hooks/use-auth.ts`      | `useCurrentUser` (protect a page), `useRedirectIfAuthenticated` |
| `src/lib/api.ts`             | Typed fetch wrapper for the FastAPI backend                    |
| `src/lib/token-storage.ts`   | JWT storage (localStorage for Phase 1)                         |
| `src/lib/validation.ts`      | Zod schemas, kept in sync with the API's Pydantic rules        |

## Scripts

- `npm run dev`: dev server (Turbopack)
- `npm run build`: production build
- `npm run lint`: ESLint
