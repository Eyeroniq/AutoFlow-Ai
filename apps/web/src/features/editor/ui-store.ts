import { create } from "zustand";

export type RightPanel = "variables" | "validation" | null;
export type ConnectionStatus = "idle" | "connecting" | "open" | "reconnecting" | "closed" | "error";

interface EditorUi {
  libraryOpen: boolean;
  /** Variables/validation panels; with neither open, a selected node shows its config. */
  rightPanel: RightPanel;
  runPanelOpen: boolean;
  runInputsOpen: boolean;
  connection: { status: ConnectionStatus; detail: string | null };
  /** Bumped to ask the config panel to open the Test section for the selected node. */
  testRequest: number;
  set: (patch: Partial<Omit<EditorUi, "set">>) => void;
}

/** View state only: not part of the graph, history, or saves. */
export const useEditorUi = create<EditorUi>((set) => ({
  libraryOpen: true,
  rightPanel: null,
  runPanelOpen: false,
  runInputsOpen: false,
  connection: { status: "idle", detail: null },
  testRequest: 0,
  set: (patch) => set(patch),
}));
