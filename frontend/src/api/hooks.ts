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

import { api, runStreamUrl, streamFeedUrl } from "./client";
import type {
  CreateRunBody,
  CreateStreamBody,
  RunEvent,
  RunSnapshot,
  RunView,
  SchemaOperation,
  StreamView,
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
  runAssets: (id: string, params: Record<string, unknown>) => ["run-assets", id, params] as const,
  providers: (id?: number) => ["providers", id ?? null] as const,
  outputs: (id?: number) => ["outputs", id ?? null] as const,
};

export const useSystem = () => useQuery({ queryKey: keys.system, queryFn: api.system });

/** Installed plugins (section 44). Refetched on mount so a `pip install` shows. */
export const usePlugins = () =>
  useQuery({ queryKey: ["plugins"], queryFn: api.plugins, staleTime: 5_000 });

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

/** Where a run can write (sections 33, 34). */
export const useOutputs = (projectId?: number) =>
  useQuery({
    queryKey: keys.outputs(projectId),
    queryFn: () => api.outputs(projectId),
    ...EDITABLE,
  });

export function useRegisterProject() {
  const client = useQueryClient();
  const countsChanged = useSystemInvalidation();
  return useMutation({
    mutationFn: (body: { path?: string; source?: string }) => api.registerProject(body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.projects });
      countsChanged();
    },
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
      // Providers and output layouts are part of the schema, so an edit to
      // one of them has to invalidate the pages that show them.
      keys.providers(projectId),
      keys.outputs(projectId),
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
export function useRunQuality(id: string | null, live = false) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: keys.runQuality(id ?? ""),
    queryFn: () => api.runQuality(id as string),
    enabled: Boolean(id),
    refetchInterval: live ? 5_000 : false,
  });

  // One more request when the run stops. Turning the polling off is not the
  // same as taking a final reading: without this the page kept whatever it
  // last saw mid-run, and went on describing a finished run's numbers as
  // "measured so far".
  const wasLive = useRef(live);
  useEffect(() => {
    if (wasLive.current && !live && id) {
      void client.invalidateQueries({ queryKey: keys.runQuality(id) });
    }
    wasLive.current = live;
  }, [client, id, live]);

  return query;
}

/** The files a run produced (design document section 81). */
export const useRunAssets = (
  id: string | null,
  params: { kind?: string | null; entity?: string | null } = {},
) =>
  useQuery({
    queryKey: keys.runAssets(id ?? "", params),
    queryFn: () => api.runAssets(id as string, params),
    enabled: Boolean(id),
  });

/**
 * The store's own counters (design document section 46's footer).
 *
 * Invalidated by anything that changes them, because a footer reading "0 runs
 * recorded" beside a run somebody just watched finish is the interface saying
 * it has not looked.
 */
function useSystemInvalidation() {
  const client = useQueryClient();
  return useCallback(
    () => void client.invalidateQueries({ queryKey: keys.system }),
    [client],
  );
}

export function useStartRun(projectId: number) {
  const client = useQueryClient();
  const countsChanged = useSystemInvalidation();
  return useMutation({
    mutationFn: (body: CreateRunBody) => api.startRun(projectId, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["runs"] });
      countsChanged();
    },
  });
}

export function useRunControl(runId: string) {
  const client = useQueryClient();
  const countsChanged = useSystemInvalidation();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: keys.run(runId) });
    void client.invalidateQueries({ queryKey: ["runs"] });
    countsChanged();
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
  const client = useQueryClient();
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
        // The store now holds one more finished run, and the footer counts
        // those. Nothing else would tell it.
        void client.invalidateQueries({ queryKey: keys.system });
        void client.invalidateQueries({ queryKey: keys.run(runId) });
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
  }, [client, runId, enabled]);

  return { status, snapshot, events, finished };
}

/** States in which a run's numbers are still moving. */
const RUNNING = new Set(["queued", "running", "paused"]);

/**
 * Convenience for views that want the freshest numbers, whatever their source.
 *
 * For a run that has finished, the stored summary is the freshest thing there
 * is: it is what the run ended up doing. A socket snapshot from just before
 * the end and a server-side `live` block are both older news, and the server's
 * measures elapsed time from a clock that does not stop - which is how a
 * 42-millisecond run came to report a minute and counting.
 */
export function liveOrStored(run: RunView | undefined, live: LiveRun): RunSnapshot | null {
  // An empty summary is a run that stored nothing, not a run that did nothing:
  // it must not win over numbers that exist.
  const summary =
    run?.summary && Object.keys(run.summary).length > 0
      ? (run.summary as RunSnapshot)
      : undefined;
  if (run && !RUNNING.has(run.state)) return summary ?? live.snapshot ?? run.live ?? null;
  return live.snapshot ?? run?.live ?? summary ?? null;
}

export type { UseQueryResult };

// --------------------------------------------------------------------------- //
// Live streams (design document sections 35, 94)
// --------------------------------------------------------------------------- //

export const streamKeys = {
  all: (projectId?: number) => ["streams", projectId ?? null] as const,
  one: (id: string) => ["stream", id] as const,
  records: (id: string, params: Record<string, unknown>) => ["stream-records", id, params] as const,
};

/** Every stream this server is running. */
export const useStreams = (projectId?: number) =>
  useQuery({
    queryKey: streamKeys.all(projectId),
    queryFn: () => api.streams(projectId),
    refetchInterval: 5_000,
  });

/**
 * The records a stream has just produced.
 *
 * Polled rather than pushed. The socket carries the numbers, which move
 * continuously; the sample window is a peek at the data, and a browser
 * re-rendering fifty rows twenty times a second would be the most expensive
 * thing on the page for no benefit.
 */
export const useStreamRecords = (
  id: string | null,
  params: { limit?: number; entity?: string | null } = {},
  live = true,
) =>
  useQuery({
    queryKey: streamKeys.records(id ?? "", params),
    queryFn: () => api.streamRecords(id as string, params),
    enabled: Boolean(id),
    refetchInterval: live ? 1_000 : false,
  });

/** Start, steer and stop a stream. */
export function useStreamControls(projectId: number | null) {
  const client = useQueryClient();
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["streams"] });
    void client.invalidateQueries({ queryKey: ["stream"] });
  };

  return {
    start: useMutation({
      mutationFn: (body: CreateStreamBody) => api.startStream(projectId as number, body),
      onSuccess: refresh,
    }),
    retarget: useMutation({
      mutationFn: ({ id, entity, rate }: { id: string; entity: string; rate: string }) =>
        api.retargetStream(id, entity, rate),
      onSuccess: refresh,
    }),
    pause: useMutation({ mutationFn: (id: string) => api.pauseStream(id), onSuccess: refresh }),
    resume: useMutation({ mutationFn: (id: string) => api.resumeStream(id), onSuccess: refresh }),
    stop: useMutation({ mutationFn: (id: string) => api.stopStream(id), onSuccess: refresh }),
    forget: useMutation({ mutationFn: (id: string) => api.forgetStream(id), onSuccess: refresh }),
  };
}

export interface LiveStreamFeed {
  status: StreamStatus;
  view: StreamView | null;
  finished: boolean;
}

const STREAM_RUNNING = new Set(["queued", "running", "paused"]);

/**
 * Follow a stream's status over its WebSocket (section 94).
 *
 * The server pushes a whole status frame twice a second rather than an event
 * per batch: a stream at 50,000 records a second would otherwise spend more
 * time serialising JSON for a dashboard than generating data.
 */
export function useStreamFeed(streamId: string | null, enabled = true): LiveStreamFeed {
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [view, setView] = useState<StreamView | null>(null);
  const [finished, setFinished] = useState(false);

  useEffect(() => {
    if (!streamId || !enabled || typeof WebSocket === "undefined") {
      setStatus("closed");
      return;
    }

    setStatus("connecting");
    setFinished(false);
    let socket: WebSocket;
    try {
      socket = new WebSocket(streamFeedUrl(streamId));
    } catch {
      setStatus("unavailable");
      return;
    }

    socket.onopen = () => setStatus("open");
    socket.onmessage = (message) => {
      let payload: (StreamView & { kind?: string }) | null;
      try {
        payload = JSON.parse(message.data as string) as StreamView & { kind?: string };
      } catch {
        return;
      }
      if (!payload || payload.kind === "error") {
        setStatus("unavailable");
        return;
      }
      setView(payload);
      if (!STREAM_RUNNING.has(payload.state)) setFinished(true);
    };
    socket.onerror = () => setStatus("unavailable");
    socket.onclose = () => setStatus((current) => (current === "open" ? "closed" : current));

    return () => {
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
      if (socket.readyState <= WebSocket.OPEN) socket.close();
    };
  }, [streamId, enabled]);

  return { status, view, finished };
}
