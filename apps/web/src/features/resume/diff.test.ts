import { describe, expect, it } from "vitest";

import { diffWords } from "./diff";

const text = (parts: { text: string; kind: string }[], kind: string) =>
  parts
    .filter((p) => p.kind === kind)
    .map((p) => p.text)
    .join(" | ");

describe("diffWords", () => {
  it("marks the words a rewrite removed and the words it added", () => {
    const { before, after } = diffWords(
      "Responsible for the backend of the onboarding platform.",
      "Architected the backend of the onboarding platform serving [N] merchants.",
    );
    expect(text(before, "del")).toBe("Responsible for");
    expect(text(after, "add")).toBe("Architected | serving [N] merchants.");
    expect(text(before, "same")).toBe("the backend of the onboarding platform.");
  });

  it("treats identical text as unchanged", () => {
    const { before, after } = diffWords("Led a team of 4.", "Led a team of 4.");
    expect(before).toEqual([{ text: "Led a team of 4.", kind: "same" }]);
    expect(after).toEqual([{ text: "Led a team of 4.", kind: "same" }]);
  });

  it("ignores case and trailing punctuation when matching words", () => {
    const { before, after } = diffWords("Built APIs, quickly", "built apis quickly now");
    expect(text(before, "del")).toBe("");
    expect(text(after, "add")).toBe("now");
  });

  it("shows a total rewrite as a deletion and an addition", () => {
    const { before, after } = diffWords("Helped the team", "Mentored four engineers");
    expect(text(before, "same")).toBe("");
    expect(text(before, "del")).toBe("Helped the team");
    expect(text(after, "add")).toBe("Mentored four engineers");
  });

  it("copes with empty text and very long text", () => {
    expect(diffWords("", "New bullet")).toEqual({ before: [], after: [{ text: "New bullet", kind: "add" }] });
    const long = Array.from({ length: 700 }, (_, i) => `w${i}`).join(" ");
    expect(diffWords(long, `${long} extra`).after).toEqual([{ text: `${long} extra`, kind: "add" }]);
  });
});
