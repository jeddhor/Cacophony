/**
 * The HTTP client for the Cacophony API (design document section 36).
 *
 * One place that knows how to call the backend, and one place that knows how
 * to read its errors. The API answers a schema mistake with
 * `{"error": "schema", "detail": "..."}` and that detail is the compiler's own
 * message - the most useful sentence available - so it must reach the screen
 * intact rather than becoming "Request failed".
 */

import type {
  CreateRunBody,
  GeneratorInfo,
  JobView,
  LintReport,
  ModelInfo,
  OutputsView,
  PlanView,
  PreviewResult,
  ProjectDetail,
  ProjectSummary,
  ProviderHealth,
  AssetsView,
  ProvidersView,
  QualityReport,
  RejectsView,
  RunView,
  SchemaOperation,
  SchemaTypesView,
  SchemaView,
  PluginsView,
  StoredEvent,
  StreamRecordsView,
  StreamView,
  CreateStreamBody,
  SystemInfo,
} from "./types";

/** An error carrying whatever the API was able to explain about it. */
export class ApiError extends Error {
  readonly status: number;
  readonly kind: string;

  constructor(message: string, status: number, kind = "error") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

const BASE = "/api";

/**
 * The desktop shell's per-launch token (design document section 41).
 *
 * A served Cacophony has none and needs none — a browser tab is an explicit act
 * by a person. A desktop window is not, and its backend listens on loopback
 * where every other process on the machine can reach it, so the shell generates
 * a token, hands it to the page in the query string, and the API requires it.
 *
 * Read once at load and then removed from the address bar, so it does not end
 * up in a screenshot, a bookmark or a shared link.
 */
const TOKEN: string | null = (() => {
  if (typeof window === "undefined") return null;
  const found = new URLSearchParams(window.location.search).get("token");
  if (!found) return null;
  const clean = new URL(window.location.href);
  clean.searchParams.delete("token");
  window.history.replaceState({}, "", clean.toString());
  return found;
})();

/** Whether this page is running inside the desktop shell. */
export const isDesktop = (): boolean => TOKEN !== null;

function withToken(init?: RequestInit): HeadersInit | undefined {
  const headers: Record<string, string> = {};
  if (init?.body) headers["Content-Type"] = "application/json";
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  return Object.keys(headers).length > 0 ? headers : undefined;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { ...withToken(init), ...(init?.headers as Record<string, string>) },
    });
  } catch (cause) {
    // A dead backend is the single most likely failure in local development,
    // so it gets a sentence that says what to do about it.
    throw new ApiError(
      "Could not reach the Cacophony API. Is `cacophony serve` running?",
      0,
      "network",
    );
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    throw new ApiError(explain(payload, response), response.status, kindOf(payload));
  }
  return payload as T;
}

function kindOf(payload: unknown): string {
  if (payload && typeof payload === "object" && "error" in payload) {
    return String((payload as { error: unknown }).error);
  }
  return "error";
}

function explain(payload: unknown, response: Response): string {
  if (payload && typeof payload === "object") {
    const body = payload as Record<string, unknown>;
    if (typeof body.detail === "string") return body.detail;
    // FastAPI's validation errors arrive as a list of locations and messages.
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((item) => {
          const entry = item as { loc?: unknown[]; msg?: string };
          const where = (entry.loc ?? []).slice(1).join(".");
          return where ? `${where}: ${entry.msg}` : String(entry.msg);
        })
        .join("; ");
    }
  }
  return `${response.status} ${response.statusText}`;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  body: JSON.stringify(body),
});

export const api = {
  system: () => request<SystemInfo>("/system"),
  generators: () => request<GeneratorInfo[]>("/generators"),
  schemaTypes: () => request<SchemaTypesView>("/schema/types"),
  outputs: (projectId?: number) =>
    request<OutputsView>(
      `/outputs${projectId !== undefined ? `?project_id=${projectId}` : ""}`,
    ),

  // -- projects ---------------------------------------------------------- //
  projects: () => request<ProjectSummary[]>("/projects"),
  project: (id: number) => request<ProjectDetail>(`/projects/${id}`),
  registerProject: (body: { path?: string; source?: string }) =>
    request<ProjectDetail>("/projects", json(body)),

  plan: (id: number) => request<PlanView>(`/projects/${id}/plan`),
  lint: (id: number) => request<LintReport>(`/projects/${id}/lint`),
  schema: (id: number) => request<SchemaView>(`/projects/${id}/schema`),

  patchSchema: (id: number, operations: SchemaOperation[]) =>
    request<{ revision_id: number; applied: string[]; changed: boolean }>(
      `/projects/${id}/schema`,
      { method: "PATCH", body: JSON.stringify({ operations }) },
    ),

  writeSchema: (id: number, source: string) =>
    request<{ revision_id: number }>(`/projects/${id}/schema`, {
      method: "PUT",
      body: JSON.stringify({ source }),
    }),

  preview: (
    id: number,
    body: { entity?: string; count?: number; offset?: number; seed?: number; isolate?: boolean },
  ) => request<PreviewResult>(`/projects/${id}/preview`, json(body)),

  // -- runs -------------------------------------------------------------- //
  runs: (params: { project_id?: number; state?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        query.set(key, String(value));
      }
    }
    const suffix = query.toString();
    return request<RunView[]>(`/runs${suffix ? `?${suffix}` : ""}`);
  },
  run: (id: string) => request<RunView>(`/runs/${id}`),
  runJobs: (id: string) => request<JobView[]>(`/runs/${id}/jobs`),
  runQuality: (id: string) => request<QualityReport>(`/runs/${id}/quality`),
  runAssets: (id: string, params: { kind?: string | null; entity?: string | null } = {}) => {
    const query = new URLSearchParams();
    if (params.kind) query.set("kind", params.kind);
    if (params.entity) query.set("entity", params.entity);
    const suffix = query.toString();
    return request<AssetsView>(`/runs/${id}/assets${suffix ? `?${suffix}` : ""}`);
  },
  runRejects: (id: string, params: { entity?: string | null; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.entity) query.set("entity", params.entity);
    if (params.limit) query.set("limit", String(params.limit));
    const suffix = query.toString();
    return request<RejectsView>(`/runs/${id}/rejects${suffix ? `?${suffix}` : ""}`);
  },
  runEvents: (id: string, after = 0, limit = 200) =>
    request<StoredEvent[]>(`/runs/${id}/events?after=${after}&limit=${limit}`),

  startRun: (projectId: number, body: CreateRunBody) =>
    request<RunView>(`/projects/${projectId}/runs`, json(body)),
  pauseRun: (id: string) => request<{ state: string }>(`/runs/${id}/pause`, { method: "POST" }),
  resumeRun: (id: string) =>
    request<{ state: string; mode: string }>(`/runs/${id}/resume`, { method: "POST" }),
  cancelRun: (id: string) => request<{ state: string }>(`/runs/${id}/cancel`, { method: "POST" }),
  deleteRun: (id: string) => request<void>(`/runs/${id}`, { method: "DELETE" }),

  // -- plugins ----------------------------------------------------------- //
  plugins: () => request<PluginsView>("/plugins"),

  // -- live streams ------------------------------------------------------ //
  streams: (projectId?: number) =>
    request<StreamView[]>(`/streams${projectId !== undefined ? `?project_id=${projectId}` : ""}`),
  stream: (id: string) => request<StreamView>(`/streams/${id}`),
  streamRecords: (id: string, params: { limit?: number; entity?: string | null } = {}) => {
    const query = new URLSearchParams();
    if (params.limit) query.set("limit", String(params.limit));
    if (params.entity) query.set("entity", params.entity);
    const suffix = query.toString();
    return request<StreamRecordsView>(`/streams/${id}/records${suffix ? `?${suffix}` : ""}`);
  },
  startStream: (projectId: number, body: CreateStreamBody) =>
    request<StreamView>(`/projects/${projectId}/streams`, json(body)),
  retargetStream: (id: string, entity: string, rate: string) =>
    request<{ entity: string; rate: string; per_second: number }>(
      `/streams/${id}/retarget`,
      json({ entity, rate }),
    ),
  pauseStream: (id: string) =>
    request<{ paused: boolean; state: string }>(`/streams/${id}/pause`, { method: "POST" }),
  resumeStream: (id: string) =>
    request<{ resumed: boolean; state: string }>(`/streams/${id}/resume`, { method: "POST" }),
  stopStream: (id: string) => request<StreamView>(`/streams/${id}/stop`, { method: "POST" }),
  forgetStream: (id: string) =>
    request<{ forgotten: boolean }>(`/streams/${id}`, { method: "DELETE" }),

  // -- providers --------------------------------------------------------- //
  providers: (projectId?: number) =>
    request<ProvidersView>(
      `/providers${projectId !== undefined ? `?project_id=${projectId}` : ""}`,
    ),
  providerModels: (providerId: string, projectId: number) =>
    request<ModelInfo[]>(`/providers/${providerId}/models?project_id=${projectId}`),
  testProvider: (providerId: string, projectId: number) =>
    request<ProviderHealth>(`/providers/${providerId}/test?project_id=${projectId}`, {
      method: "POST",
    }),
};

/** The WebSocket URL for a run's live feed (design document section 55). */
export function runStreamUrl(runId: string): string {
  return socketUrl(`/runs/${runId}/stream`);
}

/** The WebSocket URL for a live stream's status feed (section 94). */
export function streamFeedUrl(streamId: string): string {
  return socketUrl(`/streams/${streamId}/feed`);
}

function socketUrl(path: string): string {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  // A WebSocket handshake cannot carry an Authorization header, so the token
  // goes in the query string - which the API accepts for exactly this reason.
  const suffix = TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : "";
  return `${scheme}//${window.location.host}${BASE}${path}${suffix}`;
}
