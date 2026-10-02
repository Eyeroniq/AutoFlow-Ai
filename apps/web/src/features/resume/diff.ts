// Word-level before/after diff for a rewritten bullet.

export type DiffKind = "same" | "add" | "del";

export interface DiffPart {
  text: string;
  kind: DiffKind;
}

export interface BulletDiff {
  /** The original, with the words that were removed or replaced marked "del". */
  before: DiffPart[];
  /** The rewrite, with the words that are new marked "add". */
  after: DiffPart[];
}

// Past this many words a longest-common-subsequence table gets large; show the pair as replaced.
const MAX_WORDS = 600;

function tokens(text: string): string[] {
  return text.split(/\s+/).filter(Boolean);
}

function merge(parts: DiffPart[]): DiffPart[] {
  const merged: DiffPart[] = [];
  for (const part of parts) {
    const last = merged[merged.length - 1];
    if (last && last.kind === part.kind) last.text += ` ${part.text}`;
    else merged.push({ ...part });
  }
  return merged;
}

/**
 * Which words of `before` survive in `after` (a longest common subsequence of words, compared
 * without trailing punctuation or case), so the reader sees exactly what the rewrite changed.
 */
export function diffWords(before: string, after: string): BulletDiff {
  const a = tokens(before);
  const b = tokens(after);
  if (before.trim() === after.trim()) {
    return { before: a.length ? [{ text: a.join(" "), kind: "same" }] : [], after: b.length ? [{ text: b.join(" "), kind: "same" }] : [] };
  }
  if (a.length > MAX_WORDS || b.length > MAX_WORDS) {
    return { before: [{ text: a.join(" "), kind: "del" }], after: [{ text: b.join(" "), kind: "add" }] };
  }
  const key = (word: string) => word.toLowerCase().replace(/[.,;:!?]+$/, "");
  const table: number[][] = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = key(a[i]) === key(b[j]) ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const left: DiffPart[] = [];
  const right: DiffPart[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (key(a[i]) === key(b[j])) {
      left.push({ text: a[i], kind: "same" });
      right.push({ text: b[j], kind: "same" });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      left.push({ text: a[i], kind: "del" });
      i += 1;
    } else {
      right.push({ text: b[j], kind: "add" });
      j += 1;
    }
  }
  for (; i < a.length; i += 1) left.push({ text: a[i], kind: "del" });
  for (; j < b.length; j += 1) right.push({ text: b[j], kind: "add" });
  return { before: merge(left), after: merge(right) };
}
