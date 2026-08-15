/**
 * Client state (design document section 40).
 *
 * Zustand holds only what the server does not: which project the user is
 * working on, what they have selected in the Studio, and the generate-screen
 * form. Everything that comes from the backend lives in TanStack Query, so
 * there is exactly one copy of it and no chance of the two disagreeing.
 *
 * The selected project persists across reloads, because losing it on every
 * refresh is the difference between a tool and a demo.
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";

export interface GenerateForm {
  outputDir: string;
  outputFormat: string;
  records: string;
  seed: string;
  entities: string[];
  provenance: string;
  failurePolicy: string;
  cacheMode: string;
  batchSize: number;
  workers: number;
  llmBatchSize: number;
  checkpointEvery: number;
  validate: boolean;
  dropInvalid: boolean;
}

export const DEFAULT_GENERATE_FORM: GenerateForm = {
  outputDir: "out",
  outputFormat: "jsonl",
  records: "",
  seed: "",
  entities: [],
  provenance: "none",
  failurePolicy: "abort",
  cacheMode: "disabled",
  batchSize: 1000,
  workers: 4,
  llmBatchSize: 20,
  checkpointEvery: 10000,
  validate: true,
  dropInvalid: false,
};

interface StudioState {
  projectId: number | null;
  entity: string | null;
  field: string | null;
  generate: GenerateForm;

  selectProject: (id: number | null) => void;
  selectEntity: (entity: string | null) => void;
  selectField: (field: string | null) => void;
  updateGenerate: (patch: Partial<GenerateForm>) => void;
  resetGenerate: () => void;
}

export const useStudio = create<StudioState>()(
  persist(
    (set) => ({
      projectId: null,
      entity: null,
      field: null,
      generate: DEFAULT_GENERATE_FORM,

      // Changing project invalidates the selections beneath it; leaving a
      // stale entity name selected produces an empty editor with no
      // explanation.
      selectProject: (id) => set({ projectId: id, entity: null, field: null }),
      selectEntity: (entity) => set({ entity, field: null }),
      selectField: (field) => set({ field }),
      updateGenerate: (patch) =>
        set((state) => ({ generate: { ...state.generate, ...patch } })),
      resetGenerate: () => set({ generate: DEFAULT_GENERATE_FORM }),
    }),
    {
      name: "cacophony.studio",
      // The generate form is deliberately not persisted: a record count or an
      // output directory silently carried over from last week is a way to
      // overwrite a dataset by accident.
      partialize: (state) => ({ projectId: state.projectId }),
    },
  ),
);
