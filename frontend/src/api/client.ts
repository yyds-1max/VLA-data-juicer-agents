import type {
  AgentEvent,
  InteractionResponsePayload,
  InteractionResponseResult,
  NavigationDatasetSummary,
  NavigationDatasetRelease,
  NavigationDateSummary,
  NavigationSyncImageListing,
  SessionDetail,
  SessionEntrypoint,
  SessionRecord,
  SessionRequestContext,
  TrainingCapabilities,
  TrainingEvent,
  TrainingModel,
  TrainingRun,
  TrainingRunLog,
  TrainingRunPreview,
  TrainingServer,
  TrainingServerResources,
  TrainingMetricSample,
  TrainingNode,
  TrainingNodeDeploymentResult,
  TrainingNodeRemovalResult,
  TrainingNodeHostKey,
  TrainingNodePreflightResult,
  TrainingNodeResourceSnapshot,
  TrainingParameterDefinition,
  TrainingLaunchTemplate,
} from "./types";

function sessionPath(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

function navigationDatasetPath(date: string): string {
  return `/api/navigation/datasets/${encodeURIComponent(date)}`;
}

function navigationClipPath(date: string, clip: string): string {
  return `${navigationDatasetPath(date)}/clips/${encodeURIComponent(clip)}`;
}

export class ApiResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
    this.name = "ApiResponseError";
  }
}

async function responseError(response: Response): Promise<ApiResponseError> {
  const fallback = `${response.status} ${response.statusText}`;
  const text = await response.text();
  if (!text) return new ApiResponseError(fallback, response.status, null);
  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      const message = typeof detail === "string" ? detail : JSON.stringify(detail);
      return new ApiResponseError(message, response.status, parsed);
    }
    return new ApiResponseError(text, response.status, parsed);
  } catch {
    return new ApiResponseError(text, response.status, text);
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as T;
}

export async function createSession(
  message: string,
  entrypoint?: SessionEntrypoint,
  requestContext?: SessionRequestContext,
): Promise<SessionRecord> {
  const data = await requestJson<{ session: SessionRecord }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      message,
      ...(entrypoint ? { entrypoint } : {}),
      ...(requestContext ? { request_context: requestContext } : {}),
    }),
  });
  return data.session;
}

export async function listSessions(): Promise<SessionRecord[]> {
  const data = await requestJson<{ sessions: SessionRecord[] }>("/api/sessions");
  return data.sessions;
}

export async function getSession(sessionId: string): Promise<SessionDetail> {
  const data = await requestJson<{ session: SessionDetail }>(sessionPath(sessionId));
  return data.session;
}

export async function submitTurn(
  sessionId: string,
  message: string,
  invocationId?: string,
): Promise<string> {
  const data = await requestJson<{ turn_id: string }>(`${sessionPath(sessionId)}/turns`, {
    method: "POST",
    body: JSON.stringify({
      message,
      ...(invocationId ? { invocation_id: invocationId } : {}),
    }),
  });
  return data.turn_id;
}

export async function interruptTurn(sessionId: string): Promise<boolean> {
  const data = await requestJson<{ interrupted: boolean }>(`${sessionPath(sessionId)}/interrupt`, {
    method: "POST",
  });
  return data.interrupted;
}

export async function submitInteractionResponse(
  sessionId: string,
  interactionId: string,
  payload: InteractionResponsePayload,
): Promise<InteractionResponseResult> {
  return requestJson<InteractionResponseResult>(
    `${sessionPath(sessionId)}/interactions/${encodeURIComponent(interactionId)}/responses`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function openSessionEvents(
  sessionId: string,
  onEvent: (event: AgentEvent) => void,
  afterSeq = 0,
): WebSocket {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(
    `${protocol}//${window.location.host}${sessionPath(sessionId)}/events?after_seq=${afterSeq}`,
  );
  socket.addEventListener("message", (message) => {
    try {
      onEvent(JSON.parse(message.data) as AgentEvent);
    } catch (error) {
      console.error("Failed to parse DataPilot event", error);
    }
  });
  return socket;
}

export async function getNavigationDatasetSummary(): Promise<NavigationDatasetSummary> {
  return requestJson<NavigationDatasetSummary>("/api/navigation/datasets/summary");
}

export async function getNavigationDatasetDate(date: string): Promise<NavigationDateSummary> {
  return requestJson<NavigationDateSummary>(navigationDatasetPath(date));
}

export async function getNavigationDatasetReleases(): Promise<NavigationDatasetRelease[]> {
  const data = await requestJson<{ releases: NavigationDatasetRelease[] }>(
    "/api/navigation/datasets/releases",
  );
  return data.releases;
}

export async function createNavigationDatasetRelease(
  date: string,
  expectedScopeManifestSha256: string,
  note: string | null,
  idempotencyKey: string,
): Promise<NavigationDatasetRelease> {
  return requestJson<NavigationDatasetRelease>(
    `/api/navigation/datasets/releases/${encodeURIComponent(date)}`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        expected_scope_manifest_sha256: expectedScopeManifestSha256,
        note,
      }),
    },
  );
}

export async function getSyncImages(
  date: string,
  clip: string,
): Promise<NavigationSyncImageListing> {
  return requestJson<NavigationSyncImageListing>(`${navigationClipPath(date, clip)}/sync-images`);
}

export function getSyncImageUrl(
  date: string,
  clip: string,
  sequence: string,
  filename: string,
): string {
  return `${navigationClipPath(date, clip)}/sync-images/${encodeURIComponent(sequence)}/${encodeURIComponent(filename)}`;
}

const trainingPath = "/api/training";
const requestIdempotencyKey = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;

export async function getTrainingCapabilities(): Promise<TrainingCapabilities> {
  return requestJson<TrainingCapabilities>(`${trainingPath}/capabilities`);
}

export async function listTrainingServers(): Promise<TrainingServer[]> {
  const data = await requestJson<{ servers: TrainingServer[] }>(`${trainingPath}/servers`);
  return data.servers;
}

export async function getTrainingServerResources(serverRef: string): Promise<TrainingServerResources> {
  return requestJson<TrainingServerResources>(`${trainingPath}/servers/${encodeURIComponent(serverRef)}/resources`);
}

export async function listTrainingNodes(): Promise<TrainingNode[]> {
  const data = await requestJson<{ nodes: TrainingNode[] }>(`${trainingPath}/nodes`);
  return data.nodes;
}

export async function createTrainingNode(payload: { name: string; description?: string; address: string; ssh_port: number; ssh_username: string }): Promise<TrainingNode> {
  const data = await requestJson<{ node: TrainingNode }>(`${trainingPath}/nodes`, { method: "POST", body: JSON.stringify(payload) });
  return data.node;
}

export async function createTrainingNodeEnrollmentToken(nodeRef: string, expectedRevision: number): Promise<{ enrollment_token: string; expires_at: string; node: TrainingNode }> {
  return requestJson<{ enrollment_token: string; expires_at: string; node: TrainingNode }>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/enrollment-tokens`, {
    method: "POST",
    body: JSON.stringify({ expected_revision: expectedRevision, expires_in_seconds: 600 }),
  });
}

export async function discoverTrainingNodeHostKey(nodeRef: string): Promise<TrainingNodeHostKey> {
  const data = await requestJson<{ host_key: TrainingNodeHostKey }>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/host-key`, { method: "POST" });
  return data.host_key;
}

export async function deployTrainingNodeWorker(nodeRef: string, payload: {
  expected_revision: number;
  confirmed_host_key: TrainingNodeHostKey;
  host_key_confirmed: true;
  ssh_password: string;
  sudo_password_mode: "same_as_ssh" | "separate" | "not_required";
  sudo_password?: string;
}): Promise<TrainingNodeDeploymentResult> {
  return requestJson<TrainingNodeDeploymentResult>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/deploy-worker`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function preflightTrainingNodeWorker(nodeRef: string, payload: {
  expected_revision: number;
  confirmed_host_key: TrainingNodeHostKey;
  host_key_confirmed: true;
  ssh_password: string;
  sudo_password_mode: "same_as_ssh" | "separate" | "not_required";
  sudo_password?: string;
}): Promise<TrainingNodePreflightResult> {
  return requestJson<TrainingNodePreflightResult>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/preflight-worker`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function removeTrainingNodeWorker(nodeRef: string, payload: {
  expected_revision: number;
  ssh_password: string;
  sudo_password_mode: "same_as_ssh" | "separate" | "not_required";
  sudo_password?: string;
}): Promise<TrainingNodeRemovalResult> {
  return requestJson<TrainingNodeRemovalResult>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/remove-worker`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getTrainingNodeResources(nodeRef: string): Promise<TrainingNodeResourceSnapshot> {
  return requestJson<TrainingNodeResourceSnapshot>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/resources`);
}

export async function listTrainingModels(): Promise<TrainingModel[]> {
  const data = await requestJson<{ models: TrainingModel[] }>(`${trainingPath}/models`);
  return data.models;
}

export async function getTrainingModel(modelRef: string): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models/${encodeURIComponent(modelRef)}`);
  return data.model;
}

export type TrainingModelDraftInput = {
  name: string;
  description?: string;
  parameter_definitions: TrainingParameterDefinition[];
  launch_template: TrainingLaunchTemplate;
};

export async function createTrainingModel(payload: TrainingModelDraftInput): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models`, { method: "POST", body: JSON.stringify(payload) });
  return data.model;
}

export async function updateTrainingModel(modelRef: string, payload: { expected_revision: number; name?: string; description?: string; parameter_definitions: TrainingParameterDefinition[]; launch_template: TrainingLaunchTemplate }): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models/${encodeURIComponent(modelRef)}`, { method: "PUT", body: JSON.stringify(payload) });
  return data.model;
}

type TrainingRunRequest = { model_ref: string; model_revision?: number; server_ref: string; gpu_uuids: string[]; parameters: Record<string, string | number | boolean>; execution_mode: "simulation" };

export async function previewTrainingRun(payload: TrainingRunRequest): Promise<TrainingRunPreview> {
  return requestJson<TrainingRunPreview>(`${trainingPath}/runs/preview`, { method: "POST", body: JSON.stringify(payload) });
}

export async function createTrainingRun(payload: TrainingRunRequest): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs`, { method: "POST", headers: { "Idempotency-Key": requestIdempotencyKey() }, body: JSON.stringify(payload) });
  return data.run;
}

export async function listTrainingRuns(): Promise<TrainingRun[]> {
  const data = await requestJson<{ runs: TrainingRun[] }>(`${trainingPath}/runs`);
  return data.runs;
}

export async function getTrainingRun(runRef: string): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}`);
  return data.run;
}

export async function stopTrainingRun(runRef: string, expectedRevision: number): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/stop`, { method: "POST", headers: { "Idempotency-Key": requestIdempotencyKey() }, body: JSON.stringify({ expected_revision: expectedRevision }) });
  return data.run;
}

export async function getTrainingRunLogs(runRef: string, afterSeq = 0): Promise<TrainingRunLog[]> {
  const data = await requestJson<{ logs: TrainingRunLog[] }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/logs?after_seq=${afterSeq}`);
  return data.logs;
}

export async function getTrainingRunMetrics(runRef: string, afterSeq = 0): Promise<TrainingMetricSample[]> {
  const data = await requestJson<{ metrics: TrainingMetricSample[] }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/metrics?after_seq=${afterSeq}`);
  return data.metrics;
}

export function openTrainingEvents(
  onEvent: (event: TrainingEvent) => void,
  afterSeq = 0,
  onError?: () => void,
): EventSource {
  const source = new EventSource(`${trainingPath}/events?after_seq=${afterSeq}`);
  const handleEvent = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as TrainingEvent);
    } catch (error) {
      console.error("Failed to parse training event", error);
    }
  };
  for (const eventName of ["run.updated", "run.log.appended", "run.metric.appended"]) {
    source.addEventListener(eventName, handleEvent as EventListener);
  }
  if (onError) source.addEventListener("error", onError);
  return source;
}
