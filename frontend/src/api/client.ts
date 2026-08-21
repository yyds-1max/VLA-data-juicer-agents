import type {
  AgentEvent,
  InteractionResponsePayload,
  InteractionResponseResult,
  NavigationDatasetSummary,
  NavigationDatasetRelease,
  NavigationDatasetResetResult,
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
  TrainingStageInputSource,
  TrainingServer,
  TrainingServerResources,
  TrainingMetricSample,
  TrainingNode,
  TrainingNodeDeploymentResult,
  TrainingNodeRemovalResult,
  TrainingNodeHostKey,
  TrainingNodePreflightResult,
  TrainingNodeResourceSnapshot,
  TrainingDatasetRelease,
  TrainingDatasetReplica,
  TrainingDirectoryListing,
  TrainingDatasetTransfer,
  TrainingDatasetSelection,
  TrainingParameterDefinition,
  TrainingLaunchTemplate,
  TrainingArtifactInspection,
  TrainingModelVersionDetail,
  TrainingModelVersionFamily,
  TrainingModelVersionSummary,
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
      const message = typeof detail === "string"
        ? detail
        : detail && typeof detail === "object" && "message" in detail && typeof detail.message === "string"
          ? detail.message
          : JSON.stringify(detail);
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
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
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

export async function resetNavigationDataset(
  date: string,
  confirmation: string,
  idempotencyKey: string,
): Promise<NavigationDatasetResetResult> {
  return requestJson<NavigationDatasetResetResult>(
    `${navigationDatasetPath(date)}/reset`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ confirmation, reason: "manual" }),
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

export async function createTrainingNode(payload: { name: string; description?: string; address: string; ssh_port: number }): Promise<TrainingNode> {
  const data = await requestJson<{ node: TrainingNode }>(`${trainingPath}/nodes`, { method: "POST", body: JSON.stringify(payload) });
  return data.node;
}

export async function deleteTrainingNode(nodeRef: string, expectedRevision: number): Promise<void> {
  await requestJson<void>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}?expected_revision=${expectedRevision}`, { method: "DELETE" });
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
  ssh_username: string;
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
  ssh_username: string;
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
  ssh_username: string;
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

export async function getTrainingModel(familyRef: string): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models/${encodeURIComponent(familyRef)}`);
  return data.model;
}

export interface TrainingModelVersionFamilyPage {
  families: TrainingModelVersionFamily[];
  next_after: string | null;
}

export async function listTrainingModelVersionFamilies(options: { query?: string; after?: string; limit?: number } = {}): Promise<TrainingModelVersionFamilyPage> {
  const query = new URLSearchParams();
  if (options.query?.trim()) query.set("query", options.query.trim());
  if (options.after) query.set("after", options.after);
  query.set("limit", String(options.limit ?? 20));
  return requestJson<TrainingModelVersionFamilyPage>(`${trainingPath}/model-version-families?${query}`);
}

export interface TrainingModelVersionPage {
  versions: TrainingModelVersionSummary[];
  next_after: string | null;
}

export async function listTrainingModelVersions(familyRef: string, options: { after?: string; limit?: number } = {}): Promise<TrainingModelVersionPage> {
  const query = new URLSearchParams();
  if (options.after) query.set("after", options.after);
  query.set("limit", String(options.limit ?? 6));
  return requestJson<TrainingModelVersionPage>(`${trainingPath}/model-version-families/${encodeURIComponent(familyRef)}/versions?${query}`);
}

export async function getTrainingModelVersion(versionRef: string): Promise<TrainingModelVersionDetail> {
  const data = await requestJson<{ version: TrainingModelVersionDetail }>(`${trainingPath}/model-versions/${encodeURIComponent(versionRef)}`);
  return data.version;
}

export async function inspectTrainingModelVersionArtifact(versionRef: string): Promise<{ inspection: TrainingArtifactInspection; version?: TrainingModelVersionDetail }> {
  return requestJson<{ inspection: TrainingArtifactInspection; version?: TrainingModelVersionDetail }>(`${trainingPath}/model-versions/${encodeURIComponent(versionRef)}/artifact-checks`, {
    method: "POST",
    headers: { "Idempotency-Key": requestIdempotencyKey() },
  });
}

export type TrainingModelConfigurationInput = {
  data_access_mode: "datapilot_managed" | "self_managed";
  parameter_definitions: TrainingParameterDefinition[];
  launch_template: TrainingLaunchTemplate;
};

export type TrainingModelDraftInput = {
  family_name: string;
  configuration: TrainingModelConfigurationInput;
};

export async function createTrainingModel(payload: TrainingModelDraftInput): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models`, { method: "POST", body: JSON.stringify(payload) });
  return data.model;
}

export async function updateTrainingModel(familyRef: string, payload: { expected_revision: number; configuration: TrainingModelConfigurationInput }): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models/${encodeURIComponent(familyRef)}`, { method: "PUT", body: JSON.stringify(payload) });
  return data.model;
}

export async function verifyTrainingModel(familyRef: string, expectedRevision: number): Promise<TrainingModel> {
  const data = await requestJson<{ model: TrainingModel }>(`${trainingPath}/models/${encodeURIComponent(familyRef)}/verify`, { method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }) });
  return data.model;
}

type TrainingRunPayload = {
  family_ref: string;
  server_ref: string;
  gpu_uuids: string[];
  version_description?: string;
  dataset_selection?: TrainingDatasetSelection;
  stages: Array<{ parameters: Record<string, string | number | boolean>; stage_input_source: TrainingStageInputSource }>;
};

export type TrainingRunPreviewRequest = TrainingRunPayload & {
  execution_mode: "simulation" | "real";
};

export type TrainingRunRequest = TrainingRunPayload & {
  execution_mode: "simulation" | "real";
};

export async function previewTrainingRun(payload: TrainingRunPreviewRequest): Promise<TrainingRunPreview> {
  return requestJson<TrainingRunPreview>(`${trainingPath}/runs/preview`, { method: "POST", body: JSON.stringify(payload) });
}

export async function createTrainingRun(payload: TrainingRunRequest): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs`, { method: "POST", headers: { "Idempotency-Key": requestIdempotencyKey() }, body: JSON.stringify(payload) });
  return data.run;
}

export type TrainingRunList = TrainingRun[] & { next_after?: string | null };

export async function listTrainingRuns(options: { status?: string; query?: string; after?: string; limit?: number } = {}): Promise<TrainingRunList> {
  const query = new URLSearchParams();
  if (options.status && options.status !== "all") query.set("status", options.status);
  if (options.query?.trim()) query.set("query", options.query.trim());
  if (options.after) query.set("after", options.after);
  query.set("limit", String(options.limit ?? 20));
  const data = await requestJson<{ runs: TrainingRun[]; next_after?: string | null }>(`${trainingPath}/runs?${query}`);
  const runs = data.runs as TrainingRunList;
  runs.next_after = data.next_after ?? null;
  return runs;
}

export async function getTrainingRun(runRef: string): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}`);
  return data.run;
}

export async function stopTrainingRun(runRef: string, expectedRevision: number): Promise<TrainingRun> {
  const data = await requestJson<{ run: TrainingRun }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/stop`, { method: "POST", headers: { "Idempotency-Key": requestIdempotencyKey() }, body: JSON.stringify({ expected_revision: expectedRevision }) });
  return data.run;
}

export type TrainingRunLogList = TrainingRunLog[] & { next_before?: number | null };

export async function getTrainingRunLogs(
  runRef: string,
  afterSeq = 0,
  stageRef?: string,
  options: { beforeSeq?: number; tail?: boolean; limit?: number; levels?: Array<TrainingRunLog["level"]>; query?: string } = {},
): Promise<TrainingRunLogList> {
  const query = new URLSearchParams({ after_seq: String(afterSeq) });
  if (stageRef) query.set("stage_ref", stageRef);
  if (options.beforeSeq != null) { query.delete("after_seq"); query.set("before_seq", String(options.beforeSeq)); }
  if (options.tail) query.set("tail", "true");
  if (options.limit) query.set("limit", String(options.limit));
  options.levels?.forEach((level) => query.append("levels", level));
  if (options.query?.trim()) query.set("query", options.query.trim());
  const data = await requestJson<{ logs: TrainingRunLog[]; next_before?: number | null }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/logs?${query}`);
  const logs = data.logs as TrainingRunLogList;
  logs.next_before = data.next_before ?? null;
  return logs;
}

export async function getTrainingRunMetrics(
  runRef: string,
  afterSeq = 0,
  stageRef?: string,
  options: { tail?: boolean; limit?: number; since?: string } = {},
): Promise<TrainingMetricSample[]> {
  const query = new URLSearchParams({ after_seq: String(afterSeq) });
  if (stageRef) query.set("stage_ref", stageRef);
  if (options.tail) query.set("tail", "true");
  if (options.limit) query.set("limit", String(options.limit));
  if (options.since) query.set("since", options.since);
  const data = await requestJson<{ metrics: TrainingMetricSample[] }>(`${trainingPath}/runs/${encodeURIComponent(runRef)}/metrics?${query}`);
  return data.metrics;
}

export async function listTrainingDatasetReleases(): Promise<TrainingDatasetRelease[]> {
  const data = await requestJson<{ releases: TrainingDatasetRelease[] }>(`${trainingPath}/dataset-releases`);
  return data.releases;
}

export async function listTrainingDatasetReplicas(nodeRef: string): Promise<TrainingDatasetReplica[]> {
  const data = await requestJson<{ replicas: TrainingDatasetReplica[] }>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/dataset-replicas`);
  return data.replicas;
}

export async function requestTrainingDirectoryListing(nodeRef: string, path: string): Promise<TrainingDirectoryListing> {
  const data = await requestJson<{ listing: TrainingDirectoryListing }>(`${trainingPath}/nodes/${encodeURIComponent(nodeRef)}/directory-listings`, {
    method: "POST",
    body: JSON.stringify({ path }),
  });
  return data.listing;
}

export async function getTrainingDirectoryListing(listingRef: string): Promise<TrainingDirectoryListing> {
  const data = await requestJson<{ listing: TrainingDirectoryListing }>(`${trainingPath}/directory-listings/${encodeURIComponent(listingRef)}`);
  return data.listing;
}

export async function createTrainingDatasetTransfers(payload: { node_ref: string; release_refs: string[]; target_parent_directory: string }): Promise<TrainingDatasetTransfer[]> {
  const data = await requestJson<{ transfers: TrainingDatasetTransfer[] }>(`${trainingPath}/dataset-transfers`, {
    method: "POST",
    headers: { "Idempotency-Key": requestIdempotencyKey() },
    body: JSON.stringify(payload),
  });
  return data.transfers;
}

export async function listTrainingDatasetTransfers(nodeRef?: string): Promise<TrainingDatasetTransfer[]> {
  const query = nodeRef ? `?node_ref=${encodeURIComponent(nodeRef)}` : "";
  const data = await requestJson<{ transfers: TrainingDatasetTransfer[] }>(`${trainingPath}/dataset-transfers${query}`);
  return data.transfers;
}

export async function getTrainingDatasetTransfer(transferRef: string): Promise<TrainingDatasetTransfer> {
  const data = await requestJson<{ transfer: TrainingDatasetTransfer }>(`${trainingPath}/dataset-transfers/${encodeURIComponent(transferRef)}`);
  return data.transfer;
}

export async function cancelTrainingDatasetTransfer(transferRef: string): Promise<TrainingDatasetTransfer> {
  const data = await requestJson<{ transfer: TrainingDatasetTransfer }>(`${trainingPath}/dataset-transfers/${encodeURIComponent(transferRef)}/cancel`, { method: "POST", body: "{}" });
  return data.transfer;
}

export async function pauseTrainingDatasetTransfer(transferRef: string): Promise<TrainingDatasetTransfer> {
  const data = await requestJson<{ transfer: TrainingDatasetTransfer }>(`${trainingPath}/dataset-transfers/${encodeURIComponent(transferRef)}/pause`, { method: "POST", body: "{}" });
  return data.transfer;
}

export async function retryTrainingDatasetTransfer(transferRef: string): Promise<TrainingDatasetTransfer> {
  const data = await requestJson<{ transfer: TrainingDatasetTransfer }>(`${trainingPath}/dataset-transfers/${encodeURIComponent(transferRef)}/retry`, { method: "POST", headers: { "Idempotency-Key": requestIdempotencyKey() }, body: "{}" });
  return data.transfer;
}

export async function removeTrainingDatasetReplica(replicaRef: string): Promise<TrainingDatasetReplica> {
  const data = await requestJson<{ replica: TrainingDatasetReplica }>(`${trainingPath}/dataset-replicas/${encodeURIComponent(replicaRef)}/remove`, {
    method: "POST",
    headers: { "Idempotency-Key": requestIdempotencyKey() },
    body: "{}",
  });
  return data.replica;
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
  for (const eventName of ["run.updated", "run.log.appended", "run.metric.appended", "dataset.transfer.updated", "dataset.replica.ready", "dataset.replica.removed"]) {
    source.addEventListener(eventName, handleEvent as EventListener);
  }
  if (onError) source.addEventListener("error", onError);
  return source;
}
