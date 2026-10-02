import type { Schemas } from "@flowforge/shared";

// Types of the resume refinement API (apps/api/app/schemas/resume.py and the agents' outputs in
// apps/api/app/services/resume_agents.py).

export type RefinementStatus = "pending" | "running" | "success" | "failed";
export type StageStatus = "pending" | "running" | "success" | "failed" | "skipped";
export type StageKey = "extract" | "parse" | "ats" | "content" | "job_match" | "rewrite";

export interface StageAttempt {
  raw: string;
  problems: string[];
}

export interface Stage {
  key: StageKey;
  title: string;
  role: string;
  status: StageStatus;
  note?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  duration_ms?: number | null;
  provider_used?: string | null;
  model?: string | null;
  mock?: boolean | null;
  attempts: StageAttempt[];
  fallback_errors: { provider: string; model?: string; error: string }[];
  warnings?: string[];
  prompt?: string | null;
  output?: Record<string, unknown> | null;
  error?: string | null;
}

export interface Contact {
  name: string;
  email: string;
  phone: string;
  location: string;
  links: string[];
}

export interface ParsedJob {
  company: string;
  title: string;
  dates: string;
  bullets: string[];
}

export interface ParsedResume {
  contact_info: Contact;
  summary: string;
  experience: ParsedJob[];
  education: { institution: string; degree: string; dates: string; details: string }[];
  skills: string[];
}

export type AtsSeverity = "blocker" | "warning";

export interface AtsIssue {
  category: string;
  severity: AtsSeverity;
  title: string;
  explanation: string;
  evidence?: string;
  fix: string;
  /** "agent" (the model reported it) or "layout" (measured in the PDF; the model had missed it). */
  source?: "agent" | "layout";
}

export interface AtsOutput {
  score: number;
  summary: string;
  issues: AtsIssue[];
}

export type BulletFlag = "weak_opening_verb" | "no_metric" | "vague_claim" | "passive_voice";

export interface ContentOutput {
  overall: string;
  summary_review: string;
  bullets: { id: string; flags: BulletFlag[]; explanation: string; rewrite: string }[];
}

export interface MatchOutput {
  match_score: number;
  summary: string;
  matched_keywords: { keyword: string; evidence: string }[];
  missing_keywords: { keyword: string; importance: "high" | "medium" | "low"; suggestion: string }[];
  requirement_matches: { requirement: string; strength: "strong" | "partial" | "weak" | "none"; bullet_ids: string[]; note: string }[];
}

export interface BulletChange {
  source_id: string;
  job: number;
  company: string;
  title: string;
  before: string;
  after: string;
  changed: boolean;
  moved: boolean;
  flags: BulletFlag[];
  explanation: string;
  job_requirements: string[];
}

export interface RefinementStats {
  bullets_total: number;
  bullets_rewritten: number;
  bullets_reordered: number;
  bullets_flagged: number;
  ats_score: number;
  ats_blockers: number;
  ats_warnings: number;
  ats_fixed_by_reformat: number;
  ats_remaining: string[];
  placeholders: number;
  corrections: number;
  jd: boolean;
  keywords_matched?: number;
  keywords_total?: number;
  match_score?: number;
  missing_high?: string[];
}

export interface RefinementResult {
  resume: ParsedResume;
  changes: BulletChange[];
  stats: RefinementStats;
  notes: string;
  corrections: { source_id: string; reason: string }[];
  summary_review: string;
  original_summary: string;
}

export type ResumeEmail = Schemas["EmailRead"];

export type RefinementSummary = Omit<Schemas["RefinementSummary"], "status"> & { status: RefinementStatus };

/** The stage outputs and the result are free-form objects in the API schema; their shapes are typed above. */
export type Refinement = Omit<Schemas["RefinementRead"], "stages" | "result" | "status"> & {
  status: RefinementStatus;
  stages: Stage[];
  result: RefinementResult | null;
};
