/**
 * The command palette's search: fuzzy matching and ranking, kept free of React so it can be
 * tested on its own.
 *
 * A query matches a text when its characters appear in order (case-insensitive), as in most
 * editors' "go to file". Matches score higher when the characters are consecutive, start a
 * word, or start the text, and when the text is shorter; an exact substring always beats a
 * scattered match.
 */

export interface PaletteItem {
  id: string;
  label: string;
  /** "Pipeline", "Page", "Action": shown on the right, and searched too. */
  group: string;
  hint?: string;
  /** Extra words that match without being shown (e.g. "settings" for Integrations). */
  keywords?: string[];
}

/** A score (higher is better), or null when `query` doesn't match `text`. */
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.trim().toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 0;
  const exact = t.indexOf(q);
  if (exact >= 0) {
    const wordStart = exact === 0 || /[\s\-_:/.(]/.test(t[exact - 1]);
    return 1000 - exact * 2 + (wordStart ? 200 : 0) - t.length * 0.5;
  }
  let score = 0;
  let last = -2;
  let from = 0;
  for (const ch of q) {
    if (ch === " ") continue;
    const index = t.indexOf(ch, from);
    if (index < 0) return null;
    score += 10;
    if (index === last + 1) score += 15; // consecutive
    if (index === 0 || /[\s\-_:/.(]/.test(t[index - 1])) score += 20; // start of a word
    score -= Math.min(index - from, 10); // gaps cost a little
    last = index;
    from = index + 1;
  }
  return score - t.length * 0.5;
}

/** Matching items, best first (ties keep their order: actions, then pages, then pipelines). */
export function rankItems<T extends PaletteItem>(items: T[], query: string, limit = 50): T[] {
  if (!query.trim()) return items.slice(0, limit);
  const scored: { item: T; score: number; index: number }[] = [];
  items.forEach((item, index) => {
    const candidates = [item.label, ...(item.keywords ?? []), `${item.group} ${item.label}`];
    const scores = candidates.map((c, i) => {
      const s = fuzzyScore(query, c);
      return s === null ? null : i === 0 ? s : s - 50; // the label itself counts most
    });
    const best = Math.max(...scores.map((s) => (s === null ? -Infinity : s)));
    if (best > -Infinity) scored.push({ item, score: best, index });
  });
  return scored
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, limit)
    .map((s) => s.item);
}

/** The next highlighted index for ArrowUp/ArrowDown, wrapping around. */
export function moveSelection(current: number, delta: 1 | -1, count: number): number {
  if (count <= 0) return 0;
  return (current + delta + count) % count;
}
