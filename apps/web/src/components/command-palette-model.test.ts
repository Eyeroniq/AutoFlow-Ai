import { describe, expect, it } from "vitest";

import { fuzzyScore, moveSelection, type PaletteItem, rankItems } from "./command-palette-model";

const items: PaletteItem[] = [
  { id: "create", label: "Create pipeline", group: "Action", keywords: ["new pipeline"] },
  { id: "knowledge", label: "Knowledge bases", group: "Page", keywords: ["documents", "rag"] },
  { id: "integrations", label: "Integrations", group: "Page", keywords: ["keys", "settings"] },
  { id: "p1", label: "Telegram: answer a question", group: "Pipeline" },
  { id: "p2", label: "Demo: Summarize and email", group: "Pipeline" },
  { id: "p3", label: "Meeting Notes", group: "Pipeline" },
];
const ids = (query: string) => rankItems(items, query).map((i) => i.id);

describe("fuzzyScore", () => {
  it("matches characters in order, ignoring case", () => {
    expect(fuzzyScore("mtng", "Meeting Notes")).not.toBeNull();
    expect(fuzzyScore("ntm", "Meeting Notes")).toBeNull();
    expect(fuzzyScore("MEET", "meeting notes")).not.toBeNull();
  });

  it("ranks a substring above a scattered match, and a word start above the middle", () => {
    expect(fuzzyScore("notes", "Meeting Notes")!).toBeGreaterThan(fuzzyScore("mtgns", "Meeting Notes")!);
    expect(fuzzyScore("note", "Notes app")!).toBeGreaterThan(fuzzyScore("note", "Keynote app")!);
  });
});

describe("rankItems", () => {
  it("finds a pipeline by part of its name", () => {
    expect(ids("summ")[0]).toBe("p2");
    expect(ids("telegram q")[0]).toBe("p1");
    expect(ids("meet")[0]).toBe("p3"); // "telegraM: answEr a quEsTion" matches too, ranked below
  });

  it("searches keywords and groups too", () => {
    expect(ids("rag")).toEqual(["knowledge"]);
    expect(ids("settings")).toEqual(["integrations"]);
    expect(ids("new")[0]).toBe("create");
  });

  it("keeps everything in order for an empty query, and nothing for a non-match", () => {
    expect(ids("")).toEqual(items.map((i) => i.id));
    expect(ids("zzzz")).toEqual([]);
  });
});

describe("moveSelection", () => {
  it("wraps around both ways", () => {
    expect(moveSelection(0, 1, 3)).toBe(1);
    expect(moveSelection(2, 1, 3)).toBe(0);
    expect(moveSelection(0, -1, 3)).toBe(2);
    expect(moveSelection(0, 1, 0)).toBe(0);
  });
});
