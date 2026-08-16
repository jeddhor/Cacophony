/**
 * Query hooks over the API (design document sections 36, 40, 55).
 *
 * Caching policy is per resource rather than global, because these resources
 * change at wildly different rates: the generator registry never changes
 * within a session, a schema changes only when someone edits it, and a running
 * run changes several times a second. One `staleTime` for all three would
 * either hammer the backend or show stale progress.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, runStreamUrl } from "./client";
import type {
  CreateRunBody,
  RunEvent,
  RunSnapshot,
  RunView,
  SchemaOperation,
} from "./types";

/** Never changes while the page is open. */
const STATIC = { staleTime: Infinity, gcTime: Infinity };
/** Changes when someone edits something. */
const EDITABLE = { staleTime: 5_000 };

export const keys = {
  system: ["system"] as const,
  types: ["schema-types"] as const,
  projects: ["projects"] as const,
  project: (id: number) => ["project", id] as const,
  schema: (id: number) => ["schema", id] as const,
  plan: (id: number) => ["plan", id] as const,
  lint: (id: number) => ["lint", id] as const,
  runs: (params: Record<string, unknown>) => ["runs", params] as const,
  run: (id: string) => ["run", id] as const,
  runEvents: (id: string) => ["run-events", id] as const,
  runQuality: (id: string) => ["run-quality", id] as const,
  providers: (id?: number) => ["providers", id ?? null] as const,
};

export const useSystem = () => useQuery({ queryKey: keys.system, queryFn: api.system });

export const useSchemaTypes = () =>
  useQuery({ queryKey: keys.types, queryFn: api.schemaTypes, ...STATIC });

export const useProjects = () =>
  useQuery({ queryKey: keys.projects, queryFn: api.projects, ...EDITABLE });

export const useProject = (id: number | null) =>
  useQuery({
    queryKey: keys.project(id ?? -1),
    queryFn: () => api.project(id as number),
    enabled: id !== null,
    ...EDITABLE,
  });

export const useSchema = (id: number | null) =>
  useQuery({
    queryKey: keys.schema(id ?? -1),
    queryFn: () => api.schema(id as number),
    enabled: id !== null,
    ...EDITABLE,
  });

export const usePlan = (id: number | null) =>
  useQuery({
    queryKey: keys.plan(id ?? -1),
    queryFn: () => api.plan(id as number),
    enabled: id !== null,
    ...EDITABLE,
  });

export const useLint = (id: number | null) =>
  useQuery({
    queryKey: keys.lint(id ?? -1),
    queryFn: () => api.lint(id as number),
    enabled: id !== null,
    ...EDITABLE,
  });

export const useProviders = (projectId?: number) =>
  useQuery({
    queryKey: keys.providers(projectId),
    queryFn: () => api.providers(projectId),
    ...EDITABLE,
  });

export function useRegisterProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { path?: string; source?: string }) => api.registerProject(body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.projects }),
  });
}

/** Editing a schema invalidates everything derived from it. */
function useSchemaInvalidation(projectId: number) {
  const client = useQueryClient();
  return useCallback(() => {
    for (const key of [
      keys.schema(projectId),
      keys.plan(projectId),
      keys.lint(projectId),
      keys.project(projectId),
    ]) {
      void client.invalidateQueries({ queryKey: key });
    }
  }, [client, projectId]);
}

export function usePatchSchema(projectId: number) {
  const invalidate = useSchemaInvalidation(projectId);
  return useMutation({
    mutationFn: (operations: SchemaOperation[]) => api.patchSchema(projectId, operations),
    onSuccess: invalidate,
  });
}

export function useWriteSchema(projectId: number) {
  const invalidate = useSchemaInvalidation(projectId);
  return useMutation({
    mutationFn: (source: string) => api.writeSchema(projectId, source),
    onSuccess: invalidate,
  });
}

export function usePreview(projectId: number) {
  return useMutation({
    mutationFn: (body: {
      entity?: string;
      count?: number;
      offset?: number;
      seed?: number;
      isolate?: boolean;
    }) => api.preview(projectId, body),
  });
}

export const useRuns = (params: { project_id?: number; state?: string; limit?: number } = {}) =>
  useQuery({
    queryKey: keys.runs(params),
    queryFn: () => api.runs(params),
    // A list of runs is worth refreshing while something is executing.
    refetchInterval: (query) =>
      (query.state.data ?? []).some((run) => run.state === "running" || run.state === "paused")
        ? 2_000
        : false,
  });

/**
 * A single run. Polls only while it is live, and only as a safety net: the
 * WebSocket carries the fast-moving numbers.
 */
export const useRun = (id: string | null) =>
  useQuery({
    queryKey: keys.run(id ?? ""),
    queryFn: () => api.run(id as string),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state === "running" || state === "paused" || state === "queued" ? 3_000 : false;
    },
  });

/**
 * The quality report for a run (design document section 58).
 *
 * Polled while the run is executing, because the numbers are meaningful long
 * before the run finishes: referential integrity measured over four million
 * records is already the answer.
 */
export const useRunQuality = (id: string | null, live = false) =>
  useQuery({
    queryKey: keys.runQuality(id ?? ""),
    queryFn: () => api.runQuality(id as string),
    enabled: Boolean(id),
    refetchInterval: live ? 5_000 : false,
  });

export function useStartRun(projectId: number) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateRunBody) => api.startRun(projectId, body),
    onSuccess: () => client.invalidateQueries({ queryKey: ["runs"] }),
  });
}

export function useRunControl(runId: string) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: keys.run(runId) });
    void client.invalidateQueries({ queryKey: ["runs"] });
  };
  return {
    pause: useMutation({ mutationFn: () => api.pauseRun(runId), onSuccess: refresh }),
    resume: useMutation({ mutationFn: () => api.resumeRun(runId), onSuccess: refresh }),
    cancel: useMutation({ mutationFn: () => api.cancelRun(runId), onSuccess: refresh }),
  };
}

export type StreamStatus = "connecting" | "open" | "closed" | "unavailable";

export interface LiveRun {
  status: StreamStatus;
  snapshot: RunSnapshot | null;
  events: RunEvent[];
  finished: boolean;
}

const TERMINAL = new Set(["run.completed", "run.failed", "run.cancelled"]);

/**
 * Follow a run over the WebSocket (design document section 55).
 *
 * The socket is the source of the numbers that move; the query above remains
 * the source of the run's record. A run started in another process has no
 * socket to attach to, so the hook reports `unavailable` and the view falls
 * back to polling rather than showing nothing.
 */
export function useRunStream(runId: string | null, enabled = true): LiveRun {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [snapshot, setSnapshot] = useState<RunSnapshot | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [finished, setFinished] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!runId || !enabled || typeof WebSocket === "undefined") {
      setStatus("closed");
      return;
    }

    setStatus("connecting");
    setFinished(false);
    let socket: WebSocket;
    try {
      socket = new WebSocket(runStreamUrl(runId));
    } catch {
      setStatus("unavailable");
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => setStatus("open");

    socket.onmessage = (message) => {
      let event: RunEvent;
      try {
        event = JSON.parse(message.data as string) as RunEvent;
      } catch {
        return;
      }

      if (event.kind === "error") {
        setStatus("unavailable");
        return;
      }

      // Progress events arrive many times a second and carry the whole
      // snapshot; keeping them all would grow without bound for no benefit.
      if (event.data && typeof event.data.records_written === "number") {
        setSnapshot(event.data as RunSnapshot);
      }
      if (event.kind !== "job.progress") {
        setEvents((previous) => [...previous.slice(-199), event]);
      }
      if (TERMINAL.has(event.kind)) {
        setFinished(true);
      }
    };

    socket.onerror = () => setStatus("unavailable");
    socket.onclose = () => setStatus((current) => (current === "open" ? "closed" : current));

    return () => {
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      if (socket.readyState <= WebSocket.OPEN) socket.close();
      socketRef.current = null;
    };
  }, [runId, enabled]);

  return { status, snapshot, events, finished };
}

/** Convenience for views that want the freshest numbers, whatever their source. */
export function liveOrStored(run: RunView | undefined, live: LiveRun): RunSnapshot | null {
  return live.snapshot ?? run?.live ?? (run?.summary as RunSnapshot | undefined) ?? null;
}

export type { UseQueryResult };
