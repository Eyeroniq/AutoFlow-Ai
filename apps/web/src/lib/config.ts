// NEXT_PUBLIC_* values are inlined at compile time, so this must be read via the
// literal `process.env.NEXT_PUBLIC_API_URL` expression.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/+$/,
  "",
);
