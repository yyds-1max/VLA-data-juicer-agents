import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as trainingApi from "../../api/client";
import type { TrainingCapabilities, TrainingModel, TrainingNode, TrainingRun, TrainingServer, TrainingServerResources } from "../../api/types";
import { validateParameterDefinitions } from "./ParameterDefinitionEditor";
import { TrainingPlatform } from "./TrainingPlatform";
import { navilaTrajectoryParameters } from "./navilaTemplate";

vi.mock("../../api/client", () => ({
  ApiResponseError: class ApiResponseError extends Error {
    constructor(message: string, readonly status: number, readonly body: unknown) {
      super(message);
    }
  },
  getTrainingCapabilities: vi.fn(), listTrainingModels: vi.fn(), listTrainingServers: vi.fn(),
  getTrainingServerResources: vi.fn(), listTrainingNodes: vi.fn(), getTrainingNodeResources: vi.fn(), listTrainingRuns: vi.fn(), createTrainingModel: vi.fn(),
  createTrainingNode: vi.fn(), deleteTrainingNode: vi.fn(), discoverTrainingNodeHostKey: vi.fn(), preflightTrainingNodeWorker: vi.fn(), deployTrainingNodeWorker: vi.fn(), removeTrainingNodeWorker: vi.fn(),
  updateTrainingModel: vi.fn(), verifyTrainingModel: vi.fn(),
  previewTrainingRun: vi.fn(), createTrainingRun: vi.fn(), getTrainingRun: vi.fn(),
  getTrainingRunLogs: vi.fn(), getTrainingRunMetrics: vi.fn(), stopTrainingRun: vi.fn(),
  openTrainingEvents: vi.fn(),
}));

const readonlyCapabilities: TrainingCapabilities = {
  permissions: ["training:view"], authentication_mode: "read_only", simulation_enabled: true,
  real_execution_enabled: false, real_execution_disabled_reason: "真实执行未配置",
};
const adminCapabilities: TrainingCapabilities = { ...readonlyCapabilities, authentication_mode: "development_admin", permissions: ["training:view", "training:manage_models", "training:manage_nodes", "training:create_runs", "training:stop_runs"], node_deployment_enabled: true };
const server: TrainingServer = { server_ref: "fake-local", name: "Fake A100 Server", kind: "simulation", gpu_count: 8 };
const resources: TrainingServerResources = { server, sampled_at: "2026-08-06T00:00:00Z", gpus: [{ gpu_uuid: "GPU-0", index: 0, name: "A100", total_memory_mib: 81920, used_memory_mib: 1024, utilization_percent: 2, temperature_c: 45, externally_occupied: false }] };
const secondaryServer: TrainingServer = { server_ref: "fake-west", name: "Fake L40S Server", kind: "simulation", gpu_count: 4 };
const secondaryResources: TrainingServerResources = { server: secondaryServer, sampled_at: "2026-08-06T00:01:00Z", gpus: [{ gpu_uuid: "GPU-WEST-1", index: 1, name: "L40S", total_memory_mib: 49152, used_memory_mib: 12288, utilization_percent: 38, temperature_c: 52, externally_occupied: false }] };
const launchTemplate = { domain: "vla", server_ref: "fake-local", working_directory: "/workspace/project", launcher_kind: "direct" as const, executable: "python", entrypoint: "train.py", fixed_argv: ["--deepspeed", "configs/zero3.json"], output_root: "/workspace/outputs", output_flag: "--output_dir", runtime_environment: { kind: "system" as const }, monitoring: { source: "stdout" as const, format: "plain" as const } };
const model: TrainingModel = { family_ref: "navila-family", family_name: "NaVILA", status: "draft", edit_revision: 1, trained_version_count: 0, created_at: "2026-08-06T00:00:00Z", updated_at: "2026-08-06T00:00:00Z", configuration: { fixed_argv: launchTemplate.fixed_argv, launch_template: launchTemplate, parameter_definitions: [{ key: "num_video_frames", label: "视频帧数", type: "integer", default: 4, minimum: 1, maximum: 64, editable: true, description: "控制每个训练样本使用的视频帧数。" }] } };
const runSpec = { contract_version: 1 as const, execution_mode: "simulation" as const, launcher_kind: "direct" as const, server_ref: "fake-local", gpu_uuids: ["GPU-0"], nnodes: 1 as const, master_addr: null, master_port: null, node_rank: null, nproc_per_node: 1, environment: { CUDA_VISIBLE_DEVICES: "0" }, parameters: { num_video_frames: 8, learning_rate: 0.0001 }, argv: ["python", "train.py"] };
const runningStage = { stage_ref: "stage-1", stage_number: 1, stage_name: "第一阶段", stage_input_source: "manual" as const, status: "running" as const, progress_percent: 40, current_step: 8, total_steps: 20, current_epoch: 1, total_epochs: 3, parameters: { num_video_frames: 8, learning_rate: 0.0001 }, run_spec: runSpec, output_directory: "/workspace/outputs/navila-family/v1-20260806/stage-01" };
const runningRun: TrainingRun = { run_ref: "run-running", family_ref: "navila-family", family_name: "NaVILA", version_ref: "version-1", version_number: 1, version_date: "20260806", version_label: "v1-20260806", status: "running", state_revision: 3, server_ref: "fake-local", gpu_uuids: ["GPU-0"], progress_percent: 40, current_step: 8, total_steps: 20, current_epoch: 1, total_epochs: 3, stage_count: 1, current_stage_number: 1, stages: [runningStage], created_at: "2026-08-06T00:00:00Z", parameters: { num_video_frames: 8, learning_rate: 0.0001 }, audit_events: [{ created_at: "2026-08-06T00:01:00Z", action: "run.started", summary: "模拟训练已启动" }] };
const succeededRun: TrainingRun = { ...runningRun, run_ref: "run-succeeded", version_ref: "version-2", version_number: 2, version_label: "v2-20260806", status: "succeeded", state_revision: 5, progress_percent: 100, current_step: 20, stages: [{ ...runningStage, status: "succeeded", progress_percent: 100, current_step: 20 }] };
const pendingNode: TrainingNode = { node_ref: "node-test", name: "测试训练节点", description: "", address: "10.0.0.12", ssh_port: 2222, ssh_username: null, status: "pending_enrollment", state_revision: 1, heartbeat_revision: 0, created_at: "2026-08-12T00:00:00Z", updated_at: "2026-08-12T00:00:00Z" };

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function renderPlatform(path = "/model") {
  function LocationProbe() { return <span data-testid="location-path">{useLocation().pathname}</span>; }
  return render(<MemoryRouter initialEntries={[path]}><TrainingPlatform /><LocationProbe /></MemoryRouter>);
}

async function openNewTraining() {
  fireEvent.click(await screen.findByRole("tab", { name: "训练任务" }));
  const buttons = await screen.findAllByRole("button", { name: "新建训练任务" });
  fireEvent.click(buttons[0]);
}

function mockApi(capabilities = readonlyCapabilities, models: TrainingModel[] = []) {
  vi.mocked(trainingApi.getTrainingCapabilities).mockResolvedValue(capabilities);
  vi.mocked(trainingApi.listTrainingModels).mockResolvedValue(models);
  vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([]);
  vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([server]);
  vi.mocked(trainingApi.getTrainingServerResources).mockResolvedValue(resources);
  vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([]);
}

describe("TrainingPlatform", () => {
  beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal("ResizeObserver", TestResizeObserver); mockApi(); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("shows a real-execution disabled notice and keeps write flows disabled for a read-only principal", async () => {
    renderPlatform();
    expect(await screen.findByText("真实训练未启用")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));
    expect(await screen.findByRole("button", { name: "登记模型" })).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: "训练任务" }));
    expect((await screen.findAllByRole("button", { name: "新建训练任务" }))[0]).toBeDisabled();
  });

  it("opens new training from the task page instead of exposing a peer navigation tab", async () => {
    mockApi(adminCapabilities, [model]);
    renderPlatform();
    expect(await screen.findByRole("tab", { name: "训练任务" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "新建训练" })).not.toBeInTheDocument();
    fireEvent.click((await screen.findAllByRole("button", { name: "新建训练任务" }))[0]);
    expect(await screen.findByRole("heading", { name: "新建训练任务" })).toBeVisible();
    expect(screen.getByRole("tab", { name: "训练任务" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "← 返回训练任务" }));
    expect(await screen.findByRole("heading", { name: "训练任务" })).toBeVisible();
  });

  it("summarizes the selected node resources beside the GPU picker", async () => {
    const gib = 1024 ** 3;
    mockApi(adminCapabilities, [model]);
    vi.mocked(trainingApi.getTrainingServerResources).mockResolvedValue({
      ...resources,
      cpu: { logical_cores: 64, load_1m: 8.25 },
      memory: { available_bytes: 96 * gib, total_bytes: 128 * gib },
      disks: [{ mount: "/data", available_bytes: 640 * gib, total_bytes: 1024 * gib }],
    });
    renderPlatform();
    await openNewTraining();

    const resourcePanel = await screen.findByRole("complementary", { name: "训练资源概览" });
    expect(resourcePanel).toHaveTextContent("64 核");
    expect(resourcePanel).toHaveTextContent("75%");
    expect(resourcePanel).toHaveTextContent("640 GiB");
    expect(resourcePanel).toHaveTextContent("0/1");
    fireEvent.click(screen.getByRole("checkbox", { name: "选择 GPU 0" }));
    expect(resourcePanel).toHaveTextContent("1/1");
    expect(resourcePanel).toHaveTextContent("80 GiB 显存");
    const frameSlider = screen.getByRole("slider", { name: "视频帧数 快速调整" });
    expect(frameSlider).toHaveValue("4");
    fireEvent.change(frameSlider, { target: { value: "8" } });
    expect(screen.getByLabelText("视频帧数")).toHaveValue(8);
  });

  it("registers a node then automatically deploys a Worker with one-time SSH credentials", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([]);
    vi.mocked(trainingApi.createTrainingNode).mockResolvedValue(pendingNode);
    const hostKey = { algorithm: "ssh-ed25519", public_key: "A".repeat(40), sha256_fingerprint: `SHA256:${"B".repeat(43)}` };
    vi.mocked(trainingApi.discoverTrainingNodeHostKey).mockResolvedValue(hostKey);
    vi.mocked(trainingApi.preflightTrainingNodeWorker).mockResolvedValue({ ready: true, checked_at: "2026-08-13T08:00:00Z", checks: [{ code: "runtime_identity", label: "Worker 与训练运行身份", status: "passed", detail: "将以 SSH 登录账号 trainer 运行 Worker 和训练任务。" }, { code: "deployment_privilege", label: "部署账号权限", status: "passed", detail: "已验证 sudo 权限。" }] });
    vi.mocked(trainingApi.deployTrainingNodeWorker).mockResolvedValue({ node: { ...pendingNode, state_revision: 2, deployment_status: "succeeded", installed_worker_version: "0.1.0" }, deployment: { status: "succeeded", worker_version: "0.1.0", message: "Worker deployed." } });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));
    fireEvent.change(screen.getByLabelText("节点名称"), { target: { value: "测试训练节点" } });
    fireEvent.change(screen.getByLabelText("主机地址"), { target: { value: "10.0.0.12" } });
    fireEvent.change(screen.getByLabelText("SSH 端口"), { target: { value: "2222" } });
    fireEvent.change(screen.getByLabelText("SSH 登录用户名"), { target: { value: "trainer" } });
    fireEvent.change(screen.getByLabelText("SSH 登录密码"), { target: { value: "one-time-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登记并部署 Worker" }));
    expect((await screen.findAllByText("待部署 Worker"))[0]).toBeVisible();
    expect(trainingApi.createTrainingNode).toHaveBeenCalledWith({ name: "测试训练节点", address: "10.0.0.12", ssh_port: 2222, description: undefined });
    expect(await screen.findByText(hostKey.sha256_fingerprint)).toBeVisible();
    const sshPasswordInput = screen.getByLabelText("SSH 登录密码");
    expect(sshPasswordInput).toHaveAttribute("type", "password");
    expect(sshPasswordInput).toHaveClass("training-password-input");
    expect(screen.getAllByRole("button", { name: "显示 SSH 登录密码" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "显示 SSH 登录密码" }));
    expect(sshPasswordInput).toHaveAttribute("type", "text");
    fireEvent.click(document.body);
    fireEvent.focus(sshPasswordInput);
    expect(screen.getByRole("button", { name: "隐藏 SSH 登录密码" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "隐藏 SSH 登录密码" }));
    expect(sshPasswordInput).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByLabelText("我已确认该主机指纹正确"));
    expect(screen.queryByLabelText("Worker 部署提权方式")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "自动部署 Worker" })).toBeEnabled();
    expect(screen.getByText(/系统会先只读确认 SSH 登录/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "自动部署 Worker" }));
    await waitFor(() => expect(trainingApi.preflightTrainingNodeWorker).toHaveBeenCalledWith("node-test", expect.objectContaining({ ssh_username: "trainer", ssh_password: "one-time-password" })));
    await waitFor(() => expect(trainingApi.deployTrainingNodeWorker).toHaveBeenCalledWith("node-test", expect.objectContaining({ expected_revision: 1, ssh_username: "trainer", confirmed_host_key: hostKey, host_key_confirmed: true, ssh_password: "one-time-password", sudo_password_mode: "same_as_ssh" })));
    expect(await screen.findByRole("status")).toHaveTextContent("节点登记与 Worker 部署均已完成。");
    expect(screen.queryByLabelText("SSH 登录密码")).not.toBeInTheDocument();
  }, 20_000);

  it("keeps low-frequency registration collapsed after nodes already exist", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([pendingNode]);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    expect(screen.queryByLabelText("节点名称")).not.toBeInTheDocument();
    expect(screen.queryByText(/最近一次成功部署的 SSH 账号/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "登记新节点" }));
    expect(screen.getByLabelText("节点名称")).toBeVisible();
    expect(screen.getByRole("button", { name: "取消" })).toBeVisible();
  });

  it("uses node status to present one recommended primary action", async () => {
    const onlineNode: TrainingNode = { ...pendingNode, ssh_username: "trainer", status: "online", deployment_status: "succeeded", installed_worker_version: "0.1.0", worker_version: "0.1.0", enrolled_at: "2026-08-12T00:00:00Z" };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([onlineNode]);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    expect(screen.getByText("Worker 已连接中心服务，节点可以用于创建训练。")).toBeVisible();
    expect(screen.getByRole("button", { name: "查看服务器资源" })).toBeVisible();
    expect(screen.getByRole("button", { name: "更新 Worker" })).toBeVisible();
    expect(screen.getByRole("button", { name: "卸载 Worker（保留节点）" })).toHaveClass("ml-auto");
    expect(screen.getByRole("button", { name: "删除训练节点 测试训练节点" })).toBeVisible();
    expect(screen.queryByText("危险操作")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "修复 Worker" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看服务器资源" }));
    expect(screen.getByRole("tab", { name: "服务器资源" })).toHaveAttribute("aria-selected", "true");
  });

  it("shows available memory percentage and reported disk capacity for a real node", async () => {
    const gib = 1024 ** 3;
    const onlineNode: TrainingNode = { ...pendingNode, status: "online", enrolled_at: "2026-08-12T00:00:00Z", last_heartbeat_at: "2026-08-12T00:01:00Z" };
    const realServer: TrainingServer = { server_ref: onlineNode.node_ref, name: onlineNode.name, kind: "training_node", gpu_count: 0, status: "online", online: true, available: true, stale: false };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([onlineNode]);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([realServer]);
    vi.mocked(trainingApi.getTrainingServerResources).mockResolvedValue({
      server: realServer,
      sampled_at: "2026-08-12T00:01:00Z",
      stale: false,
      cpu: { logical_cores: 112, load_1m: 10.88 },
      memory: { available_bytes: 88 * gib, total_bytes: 100 * gib },
      disks: [
        { mount: "/", available_bytes: 400 * gib, total_bytes: 1000 * gib },
        { mount: "/data", available_bytes: 1200 * gib, total_bytes: 2000 * gib },
      ],
      gpus: [],
    });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));
    expect(screen.getByText("内存可用 / 总容量")).not.toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "服务器资源" }));

    expect(await screen.findByText("内存可用 / 总容量")).toBeVisible();
    expect(screen.getByLabelText("内存可用百分比 88%")).toBeVisible();
    expect(screen.getByText("88 GiB / 100 GiB")).toBeVisible();
    expect(screen.getByRole("heading", { name: "磁盘空间" })).toBeVisible();
    expect(screen.getByText("Worker 自动发现 2 个存储挂载点")).toBeVisible();
    expect(screen.getByText("400 GiB / 1000 GiB")).toBeVisible();
    expect(screen.getByText("1200 GiB / 2000 GiB")).toBeVisible();
    expect(screen.getByLabelText("/ 可用 40%")).toBeVisible();
    expect(screen.getByLabelText("/data 可用 60%")).toBeVisible();
  });

  it("shows an honest empty state when no training node is registered", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([]);
    renderPlatform();

    fireEvent.click(await screen.findByRole("tab", { name: "服务器资源" }));
    expect(await screen.findByText("尚未发现训练服务器")).toBeVisible();
    expect(screen.getByText("服务器接入后可在此切换查看资源。")).toBeVisible();
    expect(trainingApi.getTrainingServerResources).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));
    expect(await screen.findByRole("combobox", { name: "训练节点 · Server" })).toHaveTextContent("尚未登记训练节点");
  });

  it("requires two confirmations and temporary SSH credentials before removing a Worker", async () => {
    const installedNode: TrainingNode = {
      ...pendingNode,
      ssh_username: "trainer",
      status: "online",
      state_revision: 7,
      deployment_status: "succeeded",
      installed_worker_version: "0.1.0",
      worker_version: "0.1.0",
      enrolled_at: "2026-08-12T00:00:00Z",
      host_key_algorithm: "ssh-ed25519",
      host_public_key: "A".repeat(40),
      host_key_fingerprint: `SHA256:${"B".repeat(43)}`,
    };
    const removedNode: TrainingNode = {
      ...installedNode,
      status: "pending_enrollment",
      state_revision: 9,
      deployment_status: "not_started",
      installed_worker_version: null,
      worker_version: null,
      enrolled_at: null,
    };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([installedNode]);
    vi.mocked(trainingApi.getTrainingNodeResources).mockResolvedValue({ node_ref: installedNode.node_ref, captured_at: null, stale: true, resources: null });
    vi.mocked(trainingApi.removeTrainingNodeWorker).mockResolvedValue({ node: removedNode, removal: { status: "succeeded", message: "Worker removed." } });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    fireEvent.click(await screen.findByRole("button", { name: "卸载 Worker（保留节点）" }));
    expect(await screen.findByRole("heading", { name: "卸载 测试训练节点 的 Worker" })).toBeVisible();
    expect(screen.getByText(/系统只卸载 DataPilot Worker 服务和自身文件/)).toBeVisible();
    fireEvent.change(screen.getByLabelText("SSH 登录密码"), { target: { value: "one-time-removal-password" } });
    fireEvent.click(screen.getByRole("button", { name: "继续卸载" }));

    expect(await screen.findByRole("heading", { name: "再次确认卸载 Worker" })).toBeVisible();
    expect(trainingApi.removeTrainingNodeWorker).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认卸载 Worker" }));
    await waitFor(() => expect(trainingApi.removeTrainingNodeWorker).toHaveBeenCalledWith("node-test", {
      expected_revision: 7,
      ssh_username: "trainer",
      ssh_password: "one-time-removal-password",
      sudo_password_mode: "same_as_ssh",
    }));
    expect(await screen.findByText(/重新部署 Worker 前不能用于训练/)).toBeVisible();
    expect(screen.getByRole("button", { name: "部署 Worker" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "卸载 Worker（保留节点）" })).not.toBeInTheDocument();
  });

  it("separates Worker repair from changing the Worker and training account", async () => {
    const installedNode: TrainingNode = { ...pendingNode, ssh_username: "trainer", status: "offline", deployment_status: "succeeded", installed_worker_version: "0.1.0", enrolled_at: "2026-08-12T00:00:00Z" };
    const hostKey = { algorithm: "ssh-ed25519", public_key: "A".repeat(40), sha256_fingerprint: `SHA256:${"B".repeat(43)}` };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([installedNode]);
    vi.mocked(trainingApi.discoverTrainingNodeHostKey).mockResolvedValue(hostKey);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    expect(screen.getByRole("button", { name: "修复 Worker" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "更多操作" }));
    fireEvent.click(screen.getByRole("button", { name: "更换Worker和训练所属账号" }));
    expect(await screen.findByText("更换 Worker 和训练所属账号")).toBeVisible();
    const username = screen.getByLabelText("SSH 登录用户名");
    expect(username).toHaveValue("trainer");
    expect(username).not.toBeDisabled();
    fireEvent.change(username, { target: { value: "root" } });
    expect(username).toHaveValue("root");
  });

  it("updates an online Worker with its current runtime account", async () => {
    const installedNode: TrainingNode = { ...pendingNode, ssh_username: "trainer", status: "online", state_revision: 7, deployment_status: "succeeded", installed_worker_version: "0.1.0", worker_version: "0.1.0", enrolled_at: "2026-08-12T00:00:00Z" };
    const updatedNode: TrainingNode = { ...installedNode, state_revision: 8, installed_worker_version: "0.2.0", worker_version: "0.2.0" };
    const hostKey = { algorithm: "ssh-ed25519", public_key: "A".repeat(40), sha256_fingerprint: `SHA256:${"B".repeat(43)}` };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([installedNode]);
    vi.mocked(trainingApi.discoverTrainingNodeHostKey).mockResolvedValue(hostKey);
    vi.mocked(trainingApi.preflightTrainingNodeWorker).mockResolvedValue({ ready: true, checked_at: "2026-08-13T08:00:00Z", checks: [{ code: "deployment_privilege", label: "部署账号权限", status: "passed", detail: "已验证部署权限。" }] });
    vi.mocked(trainingApi.deployTrainingNodeWorker).mockResolvedValue({ node: updatedNode, deployment: { status: "succeeded", worker_version: "0.2.0", message: "Worker updated." } });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    fireEvent.click(screen.getByRole("button", { name: "更新 Worker" }));
    expect(await screen.findByText(hostKey.sha256_fingerprint)).toBeVisible();
    expect(screen.getByText("更新 Worker", { selector: "p" })).toBeVisible();
    expect(screen.getByLabelText("SSH 登录用户名")).toHaveValue("trainer");
    expect(screen.getByLabelText("SSH 登录用户名")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("我已确认该主机指纹正确"));
    fireEvent.change(screen.getByLabelText("SSH 登录密码"), { target: { value: "one-time-update-password" } });
    expect(screen.getByText(/点击“确认更新 Worker”后，系统会先只读确认 SSH 登录/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "确认更新 Worker" }));

    await waitFor(() => expect(trainingApi.preflightTrainingNodeWorker).toHaveBeenCalledWith("node-test", expect.objectContaining({ ssh_password: "one-time-update-password" })));
    await waitFor(() => expect(trainingApi.deployTrainingNodeWorker).toHaveBeenCalledWith("node-test", expect.objectContaining({
      expected_revision: 7,
      ssh_username: "trainer",
      ssh_password: "one-time-update-password",
    })));
    expect(await screen.findByText("Worker 已更新并重新连接中心服务。")).toBeVisible();
  });

  it("deletes a never-deployed training node after confirmation without asking for SSH credentials", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([pendingNode]);
    vi.mocked(trainingApi.deleteTrainingNode).mockResolvedValue(undefined);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    fireEvent.click(screen.getByRole("button", { name: "删除训练节点 测试训练节点" }));
    expect(await screen.findByRole("heading", { name: "再次确认删除训练节点" })).toBeVisible();
    expect(screen.queryByLabelText("删除操作 SSH 登录用户名")).not.toBeInTheDocument();
    expect(trainingApi.deleteTrainingNode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认删除训练节点" }));
    await waitFor(() => expect(trainingApi.deleteTrainingNode).toHaveBeenCalledWith("node-test", 1));
    expect(await screen.findByText("尚未登记训练节点。")).toBeVisible();
  });

  it("lets the user retry deleting a node when Worker removal succeeded but record deletion failed", async () => {
    const installedNode: TrainingNode = {
      ...pendingNode,
      ssh_username: "trainer",
      status: "online",
      state_revision: 7,
      deployment_status: "succeeded",
      installed_worker_version: "0.1.0",
      worker_version: "0.1.0",
      enrolled_at: "2026-08-12T00:00:00Z",
    };
    const removedNode: TrainingNode = {
      ...installedNode,
      status: "pending_enrollment",
      state_revision: 9,
      deployment_status: "not_started",
      installed_worker_version: null,
      worker_version: null,
      enrolled_at: null,
    };
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([installedNode]);
    vi.mocked(trainingApi.removeTrainingNodeWorker).mockResolvedValue({ node: removedNode, removal: { status: "succeeded", message: "Worker removed." } });
    vi.mocked(trainingApi.deleteTrainingNode)
      .mockRejectedValueOnce(new Error("temporary failure"))
      .mockResolvedValueOnce(undefined);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));

    fireEvent.click(screen.getByRole("button", { name: "删除训练节点 测试训练节点" }));
    fireEvent.change(await screen.findByLabelText("SSH 登录密码"), { target: { value: "one-time-removal-password" } });
    fireEvent.click(screen.getByRole("button", { name: "继续删除" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除训练节点" }));

    expect(await screen.findByText(/Worker 已卸载，但节点记录删除失败/)).toBeVisible();
    expect(trainingApi.removeTrainingNodeWorker).toHaveBeenCalledTimes(1);
    expect(trainingApi.deleteTrainingNode).toHaveBeenCalledWith("node-test", 9);
    fireEvent.click(screen.getByRole("button", { name: "删除训练节点 测试训练节点" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认删除训练节点" }));

    await waitFor(() => expect(trainingApi.deleteTrainingNode).toHaveBeenCalledTimes(2));
    expect(trainingApi.removeTrainingNodeWorker).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("尚未登记训练节点。")).toBeVisible();
  });

  it("clearly reports an insufficient deployment account without suggesting manual setup", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.listTrainingNodes).mockResolvedValue([pendingNode]);
    const hostKey = { algorithm: "ssh-ed25519", public_key: "A".repeat(40), sha256_fingerprint: `SHA256:${"B".repeat(43)}` };
    vi.mocked(trainingApi.discoverTrainingNodeHostKey).mockResolvedValue(hostKey);
    vi.mocked(trainingApi.preflightTrainingNodeWorker)
      .mockResolvedValueOnce({ ready: false, checked_at: "2026-08-13T08:00:00Z", checks: [{ code: "deployment_privilege", label: "部署账号权限", status: "failed", detail: "部署账号权限不足" }] })
      .mockResolvedValueOnce({ ready: true, checked_at: "2026-08-13T08:01:00Z", checks: [{ code: "deployment_privilege", label: "部署账号权限", status: "passed", detail: "独立 sudo 密码有效。" }] });
    vi.mocked(trainingApi.deployTrainingNodeWorker).mockResolvedValue({ node: { ...pendingNode, state_revision: 2, deployment_status: "succeeded", installed_worker_version: "0.1.0" }, deployment: { status: "succeeded", worker_version: "0.1.0", message: "Worker deployed." } });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "训练节点" }));
    fireEvent.click(screen.getByRole("button", { name: "部署 Worker" }));
    await screen.findByText(hostKey.sha256_fingerprint);
    fireEvent.change(screen.getByLabelText("SSH 登录用户名"), { target: { value: "trainer" } });
    fireEvent.click(screen.getByLabelText("我已确认该主机指纹正确"));
    fireEvent.change(screen.getByLabelText("SSH 登录密码"), { target: { value: "one-time-password" } });
    fireEvent.click(screen.getByRole("button", { name: "自动部署 Worker" }));

    expect(await screen.findByText("存在阻止部署的问题")).toBeVisible();
    expect(screen.getAllByText(/部署账号权限不足/).length).toBeGreaterThan(0);
    expect(screen.getByText("部分条件未满足，请根据检查结果调整后重试。")).toBeVisible();
    expect(screen.getByRole("button", { name: "自动部署 Worker" })).toBeEnabled();
    expect(trainingApi.deployTrainingNodeWorker).not.toHaveBeenCalled();
    expect(screen.queryByText(/手工创建账号/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "sudo 密码与登录密码不同？" }));
    fireEvent.change(screen.getByLabelText("独立 sudo 密码"), { target: { value: "separate-sudo-password" } });
    fireEvent.click(screen.getByRole("button", { name: "自动部署 Worker" }));
    await waitFor(() => expect(trainingApi.preflightTrainingNodeWorker).toHaveBeenLastCalledWith("node-test", expect.objectContaining({ sudo_password_mode: "separate", sudo_password: "separate-sudo-password" })));
    await waitFor(() => expect(trainingApi.deployTrainingNodeWorker).toHaveBeenCalledTimes(1));
  });

  it("surfaces a registered dataset parameter separately from hyperparameters", async () => {
    const datasetModel: TrainingModel = { ...model, configuration: { ...model.configuration!, parameter_definitions: [
      { key: "data_mixture", label: "数据混合配置", type: "string", semantic_role: "dataset", default: "rxr", editable: true },
      ...model.configuration!.parameter_definitions,
    ] } };
    mockApi(adminCapabilities, [datasetModel]);
    renderPlatform();
    await openNewTraining();
    expect(await screen.findByRole("region", { name: "训练数据集" })).toBeVisible();
    expect(within(screen.getByRole("region", { name: "训练数据集" })).getByLabelText("数据混合配置")).toHaveValue("rxr");
  });

  it("sends an admin model registration payload with the structured launch template", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "登记新模型" })).toHaveFocus());
    expect(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" })).not.toHaveFocus();
    expect(screen.getByLabelText("领域 · Domain")).toBeVisible();
    expect(screen.getByLabelText("训练节点 · Server")).toBeVisible();
    expect(screen.getByLabelText("工作目录 · Working directory")).toBeVisible();
    expect(screen.getByLabelText("启动方式 · Launcher")).toBeVisible();
    expect(screen.getByLabelText("启动程序 · Executable")).toBeVisible();
    expect(screen.getByLabelText("训练入口 · Entrypoint")).toBeVisible();
    expect(screen.getByLabelText("输出根目录 · Output root")).toBeVisible();
    expect(screen.getByLabelText(/产物输出参数 · Output flag/)).toBeVisible();
    expect(screen.getByLabelText("运行环境 · Runtime environment")).toBeVisible();
    expect(screen.getByLabelText("指标日志格式 · Metrics format")).toBeVisible();
    expect(screen.getByLabelText("领域 · Domain")).toHaveValue("");
    expect(screen.getByLabelText("领域 · Domain")).toHaveAttribute("placeholder", "例如 vla");
    expect(screen.getByLabelText("工作目录 · Working directory")).toHaveValue("");
    expect(screen.getByLabelText("工作目录 · Working directory")).toHaveAttribute("placeholder", "例如 /data/project/NaVILA");
    expect(screen.getByLabelText("训练入口 · Entrypoint")).toHaveAttribute("placeholder", "例如 llava/train/train_mem.py");
    expect(screen.getByLabelText("输出根目录 · Output root")).toHaveAttribute("placeholder", "例如 /data/training_outputs");
    expect(screen.getByText("绝对路径。填写训练节点上的模型工程目录。")).toBeVisible();
    expect(screen.getByText("相对路径。以工作目录为起点，且必须位于工作目录内。")).toBeVisible();
    expect(screen.getByText("绝对路径。平台会在此目录下生成模型版本和训练阶段目录。")).toBeVisible();
    expect(screen.getByRole("option", { name: "单进程启动（不使用 Torchrun）" })).toBeVisible();
    expect(screen.getByRole("button", { name: "登记模型" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    expect(screen.getByLabelText("启动方式 · Launcher")).toHaveValue("torchrun");
    expect(screen.getByText(/--master_port=<自动分配>/)).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "登记模型" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(trainingApi.verifyTrainingModel).toHaveBeenCalledWith(model.family_ref, model.edit_revision));
    expect(trainingApi.createTrainingModel).toHaveBeenCalledWith(expect.objectContaining({
      family_name: "NaVILA 轨迹训练",
      configuration: expect.objectContaining({ launch_template: expect.objectContaining({ server_ref: "fake-local", launcher_kind: "torchrun", executable: "torchrun", entrypoint: "llava/train/train_mem.py", fixed_argv: [], output_flag: "--output_dir", runtime_environment: { kind: "system" }, monitoring: { source: "stdout", format: "transformers" } }),
      parameter_definitions: expect.arrayContaining([expect.objectContaining({ key: "num_video_frames", default: 4 }), expect.objectContaining({ key: "tune_vision_tower", argument_style: "explicit_boolean" }), expect.objectContaining({ key: "longvila_sampler", editable: true }), expect.objectContaining({ key: "save_steps", visible_when: { parameter_key: "save_strategy", equals: "steps" } })]) }),
    }));
  });

  it("builds a direct-launch summary without Torchrun arguments and sends the launcher type", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.change(screen.getByLabelText("启动方式 · Launcher"), { target: { value: "direct" } });
    fireEvent.change(screen.getByLabelText("启动程序 · Executable"), { target: { value: "python" } });

    const commandSummaryHeading = screen.getByRole("heading", { name: "实时结构化命令摘要（默认值）" });
    const commandSummary = commandSummaryHeading.closest("section")!;
    expect(commandSummary).toBeVisible();
    expect(commandSummary.closest("aside")).toHaveClass("xl:sticky", "xl:top-4");
    expect(commandSummary.querySelector("pre")).toHaveClass("whitespace-pre", "overflow-auto");
    expect(commandSummary).toHaveTextContent("python");
    expect(commandSummary).toHaveTextContent("llava/train/train_mem.py");
    expect(commandSummary).not.toHaveTextContent("--master_port");
    expect(commandSummary).not.toHaveTextContent("--nproc_per_node");
    expect(screen.getByText("GPU 和产物输出目录由平台管理；单进程启动不会注入 Torchrun 分布式参数。")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    expect(trainingApi.createTrainingModel).toHaveBeenCalledWith(expect.objectContaining({
      configuration: expect.objectContaining({ launch_template: expect.objectContaining({ launcher_kind: "direct", executable: "python", entrypoint: "llava/train/train_mem.py" }) }),
    }));
  });

  it("rejects ambiguous model paths before registration", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));

    fireEvent.change(screen.getByLabelText("工作目录 · Working directory"), { target: { value: "relative/project" } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("工作目录必须填写训练节点上的绝对路径");

    fireEvent.change(screen.getByLabelText("工作目录 · Working directory"), { target: { value: "/data/project" } });
    fireEvent.change(screen.getByLabelText("训练入口 · Entrypoint"), { target: { value: "/data/project/train.py" } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("训练入口必须填写工作目录内的相对路径");

    fireEvent.change(screen.getByLabelText("训练入口 · Entrypoint"), { target: { value: "train.py" } });
    fireEvent.change(screen.getByLabelText("输出根目录 · Output root"), { target: { value: "relative/outputs" } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("输出根目录必须填写训练节点上的绝对路径");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("shows one current configuration per family and keeps it editable after training", async () => {
    const trainedFamily: TrainingModel = { ...model, trained_version_count: 2 };
    const revisedFamily: TrainingModel = { ...trainedFamily, edit_revision: 2 };
    mockApi(adminCapabilities, [trainedFamily]);
    vi.mocked(trainingApi.updateTrainingModel).mockResolvedValue(revisedFamily);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));

    expect(screen.getByText("已训练 2 个模型版本 · 当前训练定义")).toBeVisible();
    expect(screen.queryByRole("button", { name: "登记新版本" })).not.toBeInTheDocument();
    expect(screen.queryByText("配置已冻结")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编辑模型配置" }));
    expect(screen.getByText("修改只影响之后创建的训练，历史模型版本保留原配置快照。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));
    await waitFor(() => expect(trainingApi.updateTrainingModel).toHaveBeenCalledWith("navila-family", expect.objectContaining({ expected_revision: 1 })));
  });

  it("selects real GPUs and generates a non-persistent preview without enabling execution", async () => {
    const realServer: TrainingServer = { server_ref: "node-real", name: "NaVILA 训练节点", kind: "training_node", gpu_count: 1, status: "online", online: true, available: true, stale: false };
    const realResources: TrainingServerResources = { server: realServer, sampled_at: "2026-08-13T08:00:00Z", stale: false, gpus: [{ gpu_uuid: "GPU-real-0", index: 0, name: "A100", total_memory_mib: 81920, used_memory_mib: 2048, utilization_percent: 3, temperature_c: 42, externally_occupied: false }] };
    const realModel: TrainingModel = { ...model, family_ref: "navila-real", configuration: { ...model.configuration!, launch_template: { ...launchTemplate, server_ref: realServer.server_ref } } };
    mockApi(adminCapabilities, [realModel]);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([realServer]);
    vi.mocked(trainingApi.getTrainingServerResources).mockImplementation(async (serverRef) => serverRef === realServer.server_ref ? realResources : resources);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [{ stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py --num_video_frames 4 --output_dir /workspace/outputs/navila-real/preview/stage-01", output_directory: "/workspace/outputs/navila-real/preview/stage-01", run_spec: { ...runSpec, execution_mode: "real", server_ref: realServer.server_ref, gpu_uuids: ["GPU-real-0"], parameters: { num_video_frames: 4 } }, preflight: [{ ok: true, code: "real_preview_ready", message: "真实节点、GPU 和参数已通过预览校验；未创建任务、租约或进程。" }] }] });
    renderPlatform();

    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑模型配置" }));
    const registrationServer = screen.getByLabelText("训练节点 · Server");
    expect(registrationServer).toHaveTextContent("NaVILA 训练节点在线");
    expect(within(registrationServer).getByText("在线")).toHaveClass("text-emerald-600");
    await openNewTraining();
    expect(screen.getByRole("combobox", { name: "训练节点" })).toHaveTextContent("NaVILA 训练节点在线");
    expect(screen.getByText(/开发预览模式/)).toBeVisible();
    expect(screen.getByText(/当前不会占用或租用 GPU/)).toBeVisible();
    const gpu = screen.getByRole("checkbox", { name: "选择 GPU 0" });
    expect(gpu).toBeEnabled();
    fireEvent.click(gpu);
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({
      execution_mode: "real",
      family_ref: realModel.family_ref,
      server_ref: realServer.server_ref,
      gpu_uuids: ["GPU-real-0"],
    })));
    expect(await screen.findByText(/python train.py --num_video_frames 4/)).toBeVisible();
    expect(screen.getByRole("button", { name: "真实训练未启用" })).toBeDisabled();
    expect(trainingApi.createTrainingRun).not.toHaveBeenCalled();
  });

  it("requests Worker verification for a real model family and shows its checks", async () => {
    const realServer: TrainingServer = { server_ref: "node-real", name: "NaVILA 训练节点", kind: "training_node", gpu_count: 1, status: "online", online: true, available: true, stale: false };
    const realModel: TrainingModel = { ...model, family_ref: "navila-real", configuration: { ...model.configuration!, launch_template: { ...launchTemplate, server_ref: realServer.server_ref } } };
    const queued: TrainingModel = { ...realModel, verification: { verification_ref: "verify-1", status: "queued", requested_at: "2026-08-13T08:00:00Z" } };
    mockApi(adminCapabilities, [realModel]);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([realServer]);
    vi.mocked(trainingApi.verifyTrainingModel).mockResolvedValue(queued);
    renderPlatform();

    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(await screen.findByRole("button", { name: "验证配置" }));
    await waitFor(() => expect(trainingApi.verifyTrainingModel).toHaveBeenCalledWith(realModel.family_ref, realModel.edit_revision));
    expect(await screen.findByText("等待 Worker")).toBeVisible();

    vi.mocked(trainingApi.listTrainingModels).mockResolvedValue([{ ...queued, status: "verified", verification: { ...queued.verification!, status: "succeeded", finished_at: "2026-08-13T08:00:02Z", checks: [{ code: "entrypoint", label: "训练入口", status: "passed", detail: "训练入口存在且 Worker 可以读取。" }] } }]);
    await waitFor(() => expect(screen.getByText("验证通过")).toBeVisible(), { timeout: 3_000 });
    expect(screen.getByText("训练入口：")).toBeVisible();
    expect(screen.getByText("训练入口存在且 Worker 可以读取。")).toBeVisible();
  });

  it("infers direct launch when editing a legacy model without launcher_kind", async () => {
    const legacyLaunchTemplate: Partial<typeof launchTemplate> = { ...launchTemplate };
    delete legacyLaunchTemplate.launcher_kind;
    const legacyModel = {
      ...model,
      configuration: { ...model.configuration!, launch_template: legacyLaunchTemplate },
    } as unknown as TrainingModel;
    mockApi(adminCapabilities, [legacyModel]);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑模型配置" }));
    expect(screen.getByLabelText("启动方式 · Launcher")).toHaveValue("direct");
  });

  it("lets an administrator design a typed parameter availability rule", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.click(screen.getByRole("button", { name: "设计依赖关系" }));

    const targetSelect = await screen.findByLabelText("目标参数") as HTMLSelectElement;
    fireEvent.change(targetSelect, { target: { value: "tune_vision_tower" } });
    const controllerSelect = screen.getByLabelText("条件参数") as HTMLSelectElement;
    expect(Array.from(controllerSelect.options, (option) => option.value)).not.toContain("tune_vision_tower");
    fireEvent.change(controllerSelect, { target: { value: "bf16" } });
    fireEvent.change(screen.getByLabelText("条件值"), { target: { value: "true" } });
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));

    expect(screen.getByText("仅当「启用 BF16」等于 True 时，「训练视觉网络」才可设置。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.configuration.parameter_definitions).toContainEqual(expect.objectContaining({
      key: "tune_vision_tower",
      visible_when: { parameter_key: "bf16", equals: true },
    }));
  }, 10_000);

  it("rejects a dependency rule whose target is the stage input parameter", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByLabelText("parameter_1 参数用途"), { target: { value: "stage_input" } });
    fireEvent.click(screen.getByRole("button", { name: "设计依赖关系" }));
    fireEvent.change(await screen.findByLabelText("目标参数"), { target: { value: "parameter_1" } });
    fireEvent.change(screen.getByLabelText("条件参数"), { target: { value: "parameter_2" } });
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("阶段输入参数 parameter_1 必须始终可用，不能设置参数依赖条件");
    expect(() => validateParameterDefinitions([
      { key: "model_path", label: "加载路径", type: "string", default: "", editable: true, semantic_role: "stage_input", visible_when: { parameter_key: "mode", equals: "train" } },
      { key: "mode", label: "训练模式", type: "string", default: "train", editable: true },
    ])).toThrow("阶段输入参数 model_path 必须始终可用，不能设置参数依赖条件");
  });

  it("confirms and removes an existing dependency when marking its target as stage input", async () => {
    mockApi(adminCapabilities);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.click(screen.getByRole("button", { name: "设计依赖关系" }));
    fireEvent.change(await screen.findByLabelText("目标参数"), { target: { value: "parameter_1" } });
    fireEvent.change(screen.getByLabelText("条件参数"), { target: { value: "parameter_2" } });
    fireEvent.click(screen.getByRole("button", { name: "保存规则" }));
    expect(screen.getByText(/仅当「新参数」等于/)).toBeVisible();

    fireEvent.change(screen.getByLabelText("parameter_1 参数用途"), { target: { value: "stage_input" } });
    expect(confirm).toHaveBeenCalledWith("阶段输入参数必须始终可用。设为阶段输入参数会移除该参数已有的依赖条件，确定继续吗？");
    expect(screen.getByLabelText("parameter_1 参数用途")).toHaveValue("stage_input");
    expect(screen.queryByText(/仅当「新参数」等于/)).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("creates, manages, and safely deletes a custom parameter group", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));

    const groupSelect = screen.getByLabelText("longvila_sampler 展示分组");
    const explanationInput = screen.getByLabelText("longvila_sampler 参数解释");
    expect(explanationInput.compareDocumentPosition(groupSelect) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(groupSelect, { target: { value: "__new_group__" } });
    expect(await screen.findByRole("heading", { name: "新建参数分组" })).toBeVisible();
    fireEvent.change(screen.getByLabelText("新分组名称"), { target: { value: "采样高级配置" } });
    fireEvent.click(screen.getByRole("button", { name: "创建并移入" }));
    expect(screen.getByLabelText("longvila_sampler 展示分组")).toHaveValue("custom_group_1");

    fireEvent.click(screen.getByRole("button", { name: "管理参数分组" }));
    const groupName = await screen.findByLabelText("采样高级配置 分组名称");
    fireEvent.change(groupName, { target: { value: "采样与解码" } });
    expect(screen.getByLabelText("采样与解码 分组名称")).toHaveValue("采样与解码");
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "删除分组 采样与解码" }));
    expect(await screen.findByRole("heading", { name: "确认删除参数分组" })).toBeVisible();
    expect(screen.getByText(/该分组内的 1 个参数将自动移入系统保留的“其他参数”/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "删除分组并迁移参数" }));
    expect(screen.getByLabelText("longvila_sampler 展示分组")).toHaveValue("other");
  }, 20_000);

  it("keeps common and other reserved while allowing recommended groups to be renamed and deleted", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.click(screen.getByRole("button", { name: "管理参数分组" }));

    expect(screen.queryByLabelText("常用参数 分组名称")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除分组 常用参数" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("其他参数 分组名称")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除分组 其他参数" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("优化器与正则 分组名称")).toBeVisible();
    expect(screen.getByRole("button", { name: "删除分组 优化器与正则" })).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "删除分组 优化器与正则" }));
    expect(await screen.findByText(/该分组内的 2 个参数将自动移入系统保留的“其他参数”/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "删除分组并迁移参数" }));
    expect(screen.getByLabelText("weight_decay 展示分组")).toHaveValue("other");
    expect(screen.getByLabelText("warmup_ratio 展示分组")).toHaveValue("other");
  });

  it("uses num_video_frames from the editable parameter and starts only after preview", async () => {
    mockApi(adminCapabilities, [model]);
    const createdRun: TrainingRun = { ...runningRun, run_ref: "run-1", status: "queued", state_revision: 1, progress_percent: 0, current_step: 0, current_epoch: 0, stages: [{ ...runningStage, status: "pending", progress_percent: 0, current_step: 0, current_epoch: 0 }] };
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [{ stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py --num_video_frames 8", output_directory: "/workspace/outputs/preview/stage-01", run_spec: { ...runSpec, parameters: { num_video_frames: 8 } }, preflight: [{ ok: true, message: "资源可用" }] }] });
    vi.mocked(trainingApi.createTrainingRun).mockResolvedValue(createdRun);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(createdRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([]);
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));
    const frames = screen.getByLabelText("视频帧数");
    const parameterCard = frames.closest<HTMLElement>('[data-parameter-field="num_video_frames"]');
    expect(parameterCard).not.toBeNull();
    expect(within(parameterCard!).getByText("num_video_frames")).toBeVisible();
    expect(within(parameterCard!).queryByText("--num_video_frames")).not.toBeInTheDocument();
    const parameterHelp = within(parameterCard!).getByLabelText("视频帧数 参数说明");
    fireEvent.focus(parameterHelp);
    const parameterTooltip = await screen.findByRole("tooltip");
    expect(parameterHelp).toHaveAttribute("data-state", "instant-open");
    expect(parameterTooltip).toHaveAttribute("data-side");
    expect(parameterTooltip).toHaveTextContent("控制每个训练样本使用的视频帧数");
    fireEvent.keyDown(parameterHelp, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("tooltip")).not.toBeInTheDocument());
    fireEvent.change(frames, { target: { value: "8" } });
    expect(screen.getByRole("button", { name: "启动模拟训练" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ family_ref: "navila-family", stages: [{ stage_input_source: "manual", parameters: { num_video_frames: 8 } }], gpu_uuids: ["GPU-0"] })));
    expect(vi.mocked(trainingApi.previewTrainingRun).mock.calls[0][0]).not.toHaveProperty("model_revision");
    expect(await screen.findByText("不需要")).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: "启动模拟训练" }));
    await waitFor(() => expect(trainingApi.createTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ stages: [{ stage_input_source: "manual", parameters: { num_video_frames: 8 } }], execution_mode: "simulation" })));
    expect(vi.mocked(trainingApi.createTrainingRun).mock.calls[0][0]).not.toHaveProperty("model_revision");
    await screen.findByText("任务 run-1");
    const modelsTab = screen.getByRole("tab", { name: "模型注册" });
    fireEvent.click(modelsTab);
    await waitFor(() => expect(modelsTab).toHaveAttribute("aria-selected", "true"));
    expect(screen.getByText("已训练 1 个模型版本 · 当前训练定义")).toBeVisible();
    expect(screen.getByRole("button", { name: "编辑模型配置" })).toBeVisible();
  });

  it("adds sequential stages, copies values, and references the previous output", async () => {
    const stagedModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          { key: "model_name_or_path", label: "预训练参数加载地址", type: "string", semantic_role: "stage_input", default: "/models/base", editable: true },
          { key: "learning_rate", label: "学习率", type: "number", default: 0.0001, editable: true },
        ],
      },
    };
    mockApi(adminCapabilities, [stagedModel]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [
      { stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py --output_dir /workspace/outputs/preview/stage-01", output_directory: "/workspace/outputs/preview/stage-01", run_spec: { ...runSpec, parameters: { model_name_or_path: "/models/base", learning_rate: 0.0002 } }, preflight: [{ ok: true, message: "资源可用" }] },
      { stage_number: 2, stage_name: "第二阶段", command_preview: "python train.py --model_name_or_path /workspace/outputs/preview/stage-01", output_directory: "/workspace/outputs/preview/stage-02", run_spec: { ...runSpec, parameters: { model_name_or_path: "/workspace/outputs/preview/stage-01", learning_rate: 0.0002 } }, preflight: [{ ok: true, message: "资源可用" }] },
    ] });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));
    fireEvent.change(screen.getByLabelText("学习率"), { target: { value: "0.0002" } });
    fireEvent.click(screen.getByRole("button", { name: "添加训练阶段" }));

    expect(screen.getByRole("tab", { name: "第二阶段" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByLabelText("学习率")).toHaveValue(0.0002);
    expect(screen.getByLabelText("使用上一阶段输出目录")).toBeChecked();
    expect(screen.getByLabelText("上一阶段输出目录")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("手动填写"));
    expect(screen.getByLabelText("预训练参数加载地址")).toHaveValue("/models/base");
    fireEvent.change(screen.getByLabelText("预训练参数加载地址"), { target: { value: "/models/manual-stage-2" } });
    fireEvent.click(screen.getByLabelText("使用上一阶段输出目录"));
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({
      family_ref: "navila-family",
      stages: [
        expect.objectContaining({ stage_input_source: "manual", parameters: expect.objectContaining({ model_name_or_path: "/models/base", learning_rate: 0.0002 }) }),
        expect.objectContaining({ stage_input_source: "previous_stage_output", parameters: expect.objectContaining({ learning_rate: 0.0002 }) }),
      ],
    })));
    expect(await screen.findByDisplayValue("/workspace/outputs/preview/stage-01")).toBeDisabled();
    fireEvent.click(screen.getByText("第二阶段 · /workspace/outputs/preview/stage-02"));
    expect(screen.getByText(/--model_name_or_path \/workspace\/outputs\/preview\/stage-01/)).toBeVisible();

    fireEvent.change(screen.getByLabelText("学习率"), { target: { value: "0.0003" } });
    expect(screen.queryByText(/--model_name_or_path \/workspace\/outputs\/preview\/stage-01/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "添加训练阶段" }));
    expect(screen.getByRole("tab", { name: "第三阶段" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "删除第二阶段" }));
    expect(confirm).toHaveBeenCalledWith("确定删除第二阶段吗？后续阶段将自动重新编号，已生成的预览会失效。");
    expect(screen.getByRole("tab", { name: "第二阶段" })).toBeVisible();
    expect(screen.queryByRole("tab", { name: "第三阶段" })).not.toBeInTheDocument();
    confirm.mockRestore();
  });

  it("keeps every stage manual when the family has no stage input parameter", async () => {
    mockApi(adminCapabilities, [model]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [
      { stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py", output_directory: "/workspace/outputs/navila-family/preview/stage-01", run_spec: runSpec, preflight: [{ ok: true, message: "资源可用" }] },
      { stage_number: 2, stage_name: "第二阶段", command_preview: "python train.py", output_directory: "/workspace/outputs/navila-family/preview/stage-02", run_spec: runSpec, preflight: [{ ok: true, message: "资源可用" }] },
    ] });
    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));
    fireEvent.click(screen.getByRole("button", { name: "添加训练阶段" }));
    expect(screen.getByText("模型族未登记“阶段输入参数”，各阶段的加载路径需要手动填写。")).toBeVisible();
    expect(screen.queryByLabelText("使用上一阶段输出目录")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ stages: [
      expect.objectContaining({ stage_input_source: "manual" }),
      expect.objectContaining({ stage_input_source: "manual" }),
    ] })));
  });

  it("preserves edited parameters and the generated preview across background polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const pollingModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          { key: "test_argv", label: "测试参数", type: "integer", default: 0, minimum: 0, maximum: 10, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
        ],
      },
    };
    mockApi(adminCapabilities, [pollingModel]);
    vi.mocked(trainingApi.listTrainingModels).mockImplementation(async () => [structuredClone(pollingModel)]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [{ stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py --test_argv 6", output_directory: "/workspace/outputs/preview/stage-01", run_spec: { ...runSpec, parameters: { test_argv: 6 }, argv: ["python", "train.py", "--test_argv", "6"] }, preflight: [{ ok: true, message: "资源可用" }] }] });

    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));
    const testArgument = screen.getByLabelText("测试参数");
    fireEvent.change(testArgument, { target: { value: "6" } });
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    expect(await screen.findByText("python train.py --test_argv 6")).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });

    expect(trainingApi.listTrainingModels).toHaveBeenCalledTimes(2);
    expect(testArgument).toHaveValue(6);
    expect(screen.getByText("python train.py --test_argv 6")).toBeVisible();
  });

  it("disables a dependent parameter until its availability condition matches", async () => {
    const conditionalModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          { key: "bf16", label: "启用 BF16", type: "boolean", default: false, editable: true, argument_style: "explicit_boolean" },
          { key: "learning_rate", label: "学习率", type: "number", default: 0.00001, editable: true, visible_when: { parameter_key: "bf16", equals: true } },
        ],
      },
    };
    mockApi(adminCapabilities, [conditionalModel]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [{ stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py --bf16 True --learning_rate 0.00002", output_directory: "/workspace/outputs/preview/stage-01", run_spec: { ...runSpec, parameters: { bf16: true, learning_rate: 0.00002 } }, preflight: [{ ok: true, message: "资源可用" }] }] });
    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));

    expect(screen.getByLabelText("启用 BF16")).not.toBeChecked();
    const learningRate = screen.getByLabelText("学习率");
    expect(learningRate).toBeVisible();
    expect(learningRate).toBeDisabled();
    expect(learningRate).toHaveClass("disabled:cursor-not-allowed");
    expect(learningRate.closest('[data-parameter-field="learning_rate"]')).toHaveClass("opacity-50");
    expect(learningRate).toHaveAttribute("aria-describedby", "training-parameter-condition-learning_rate");
    expect(screen.getByText("仅当「启用 BF16」等于 True 时，「学习率」才可设置。")).toBeVisible();
    fireEvent.click(screen.getByLabelText("启用 BF16"));
    expect(learningRate).toBeEnabled();
    expect(learningRate.closest('[data-parameter-field="learning_rate"]')).not.toHaveClass("opacity-50");
    fireEvent.change(screen.getByLabelText("学习率"), { target: { value: "0.00002" } });
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ stages: [{ stage_input_source: "manual", parameters: { bf16: true, learning_rate: 0.00002 } }] })));

    fireEvent.click(screen.getByLabelText("启用 BF16"));
    expect(learningRate).toBeVisible();
    expect(learningRate).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenLastCalledWith(expect.objectContaining({ stages: [{ stage_input_source: "manual", parameters: { bf16: false } }] })));
  });

  it("validates run values inline and masks sensitive inputs", async () => {
    const constrainedModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          { key: "seed", label: "随机种子", type: "integer", default: 10, minimum: 0, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
          { key: "run_name", label: "运行名称", type: "string", default: "demo", string_min_length: 3, string_max_length: 8, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
          { key: "hub_token", label: "访问令牌", type: "string", default: "local-token", sensitive: true, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
        ],
      },
    };
    mockApi(adminCapabilities, [constrainedModel]);
    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));

    expect(screen.getByLabelText("访问令牌")).toHaveAttribute("type", "password");
    fireEvent.change(screen.getByLabelText("随机种子"), { target: { value: "-1" } });
    expect(screen.getByText("不能小于 0")).toBeVisible();
    expect(screen.getByRole("button", { name: "生成预览" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("随机种子"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("运行名称"), { target: { value: "x" } });
    expect(screen.getByText("至少输入 3 个字符")).toBeVisible();
    expect(screen.getByRole("button", { name: "生成预览" })).toBeDisabled();
  });

  it("keeps a masked sensitive default out of run requests until the user replaces it", async () => {
    const maskedModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          { key: "seed", label: "随机种子", type: "integer", default: 10, editable: true },
          { key: "hub_token", label: "访问令牌", type: "string", default: "********", sensitive: true, string_min_length: 12, editable: true },
        ],
      },
    };
    mockApi(adminCapabilities, [maskedModel]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ stages: [{ stage_number: 1, stage_name: "第一阶段", command_preview: "python train.py", output_directory: "/workspace/outputs/preview/stage-01", run_spec: runSpec, preflight: [{ ok: true, message: "资源可用" }] }] });
    renderPlatform();
    await openNewTraining();
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));

    const tokenInput = screen.getByLabelText("访问令牌");
    expect(tokenInput).toHaveAttribute("type", "password");
    expect(tokenInput).toHaveValue("");
    expect(tokenInput).toHaveAttribute("placeholder", "已保存敏感默认值，留空沿用");
    expect(screen.queryByText("至少输入 12 个字符")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledTimes(1));
    expect(vi.mocked(trainingApi.previewTrainingRun).mock.calls[0][0].stages[0].parameters).toEqual({ seed: 10 });

    fireEvent.change(tokenInput, { target: { value: "replacement-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(trainingApi.previewTrainingRun).mock.calls[1][0].stages[0].parameters).toEqual({ seed: 10, hub_token: "replacement-secret" });
  });

  it("rejects a fixed argv flag already registered as a parameter", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.change(screen.getByLabelText("额外固定 argv（每行一个 token）"), { target: { value: "--seed=10" } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("额外固定 argv 与训练参数重复声明了 --seed");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("keeps common parameters visible and folds advanced and custom parameters by category", async () => {
    const groupedModel: TrainingModel = {
      ...model,
      configuration: {
        ...model.configuration!,
        parameter_definitions: [
          ...navilaTrajectoryParameters,
          { key: "custom_temperature", label: "自定义温度", type: "number", default: 0.5, editable: true, cli_flag: "--custom_temperature", description: "自定义模型参数。" },
        ],
      },
    };
    mockApi(adminCapabilities, [groupedModel]);
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    await openNewTraining();

    expect(await screen.findByRole("heading", { name: "常用参数 (17)" })).toBeVisible();
    expect(screen.getByRole("region", { name: "训练数据集" })).toBeVisible();
    expect(screen.getByLabelText("学习率")).toBeVisible();
    expect(screen.getByLabelText("随机种子")).toBeVisible();
    expect(screen.getByLabelText("保存 step 间隔")).toBeVisible();
    fireEvent.change(screen.getByLabelText("保存策略"), { target: { value: "epoch" } });
    expect(screen.getByLabelText("保存 step 间隔")).toBeVisible();
    expect(screen.getByLabelText("保存 step 间隔")).toBeDisabled();
    expect(screen.getByLabelText("保存 step 间隔").closest('[data-parameter-field="save_steps"]')).toHaveClass("opacity-50");
    fireEvent.change(screen.getByLabelText("保存策略"), { target: { value: "steps" } });
    expect(screen.getByLabelText("保存 step 间隔")).toBeEnabled();
    expect(screen.getByLabelText("权重衰减")).not.toBeVisible();
    expect(screen.getByLabelText("视觉编码器")).not.toBeVisible();
    expect(screen.getByLabelText("自定义温度")).not.toBeVisible();

    const visibleSummary = (name: RegExp) => screen.getAllByText(name).map((element) => element.closest("summary")).find((summary) => summary && !summary.closest("[hidden]"))!;
    fireEvent.click(visibleSummary(/优化器与正则/));
    expect(screen.getByLabelText("权重衰减")).toBeVisible();
    fireEvent.click(visibleSummary(/模型与多模态/));
    expect(screen.getByLabelText("视觉编码器")).toBeVisible();
    expect(screen.getByLabelText("视觉编码器")).toBeEnabled();
    fireEvent.click(visibleSummary(/其他参数/));
    expect(screen.getByLabelText("自定义温度")).toBeVisible();
  });

  it("filters runs by status", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun, succeededRun]);
    renderPlatform();
    expect(await screen.findByText("NaVILA v1-20260806")).toBeVisible();
    expect(screen.getByText("NaVILA v2-20260806")).toBeVisible();
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "succeeded" } });
    expect(screen.queryByText("NaVILA v1-20260806")).not.toBeInTheDocument();
    expect(screen.getByText("NaVILA v2-20260806")).toBeVisible();
  });

  it("presents training runs as a searchable operations table", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun, succeededRun]);
    renderPlatform();

    expect(await screen.findByRole("table")).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "任务 / 模型" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "训练进度" })).toBeVisible();
    expect(screen.getByRole("region", { name: "训练概览" })).toBeVisible();

    fireEvent.change(screen.getByLabelText("搜索训练任务"), { target: { value: "run-succeeded" } });
    expect(screen.queryByText("NaVILA v1-20260806")).not.toBeInTheDocument();
    expect(screen.getByText("NaVILA v2-20260806")).toBeVisible();
  });

  it("switches between independently loaded server resource cards", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([server, secondaryServer]);
    vi.mocked(trainingApi.getTrainingServerResources).mockImplementation(async (serverRef) => serverRef === secondaryServer.server_ref ? secondaryResources : resources);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "服务器资源" }));

    expect(await screen.findByRole("heading", { name: "服务器资源" })).toBeVisible();
    expect(screen.getByRole("group", { name: "选择训练服务器" })).toBeVisible();
    const primaryGpuRegion = screen.getByRole("region", { name: "Fake A100 Server GPU 资源" });
    expect(within(primaryGpuRegion).getByText("GPU 0")).toBeVisible();
    expect(within(primaryGpuRegion).getByText("A100 · 45°C")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Fake L40S Server/ }));
    const secondaryGpuRegion = screen.getByRole("region", { name: "Fake L40S Server GPU 资源" });
    expect(within(secondaryGpuRegion).getByText("GPU 1")).toBeVisible();
    expect(within(secondaryGpuRegion).getByText("L40S · 52°C")).toBeVisible();
    expect(within(secondaryGpuRegion).queryByText("GPU 0")).not.toBeInTheDocument();
    expect(trainingApi.getTrainingServerResources).toHaveBeenCalledWith("fake-local");
    expect(trainingApi.getTrainingServerResources).toHaveBeenCalledWith("fake-west");
  });

  it("scopes selectable GPUs to the server bound to the selected model version", async () => {
    const secondaryModel: TrainingModel = { ...model, family_ref: "navila-west-family", family_name: "NaVILA West", configuration: { ...model.configuration!, launch_template: { ...launchTemplate, server_ref: secondaryServer.server_ref } } };
    mockApi(adminCapabilities, [model, secondaryModel]);
    vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([server, secondaryServer]);
    vi.mocked(trainingApi.getTrainingServerResources).mockImplementation(async (serverRef) => serverRef === secondaryServer.server_ref ? secondaryResources : resources);
    renderPlatform();
    await openNewTraining();

    expect(screen.getByRole("checkbox", { name: "选择 GPU 0" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "训练节点" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("模型族"), { target: { value: secondaryModel.family_ref } });
    expect(screen.getByRole("checkbox", { name: "选择 GPU 1" })).toBeVisible();
    expect(screen.queryByRole("checkbox", { name: "选择 GPU 0" })).not.toBeInTheDocument();
  });

  it("hides stop without training:stop_runs and shows run snapshots, GPU metrics and audit summary", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([{ seq: 1, created_at: "2026-08-06T00:01:00Z", step: 8, total_steps: 20, epoch: 1, loss: 1.2, learning_rate: 0.0001, gpu_utilization_percent: 76, gpu_memory_mib: 24576 }]);
    renderPlatform();
    fireEvent.click(await screen.findByText("NaVILA v1-20260806"));
    expect(await screen.findByText("第一阶段参数快照")).toBeVisible();
    expect(screen.getAllByText("num_video_frames")[0]).toBeVisible();
    expect(screen.getByText("模拟训练已启动 · 2026-08-06T00:01:00Z")).toBeVisible();
    expect(screen.getByRole("heading", { name: "GPU 利用率" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "GPU 显存" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "停止任务" })).not.toBeInTheDocument();
  });

  it("switches stage-specific parameters, logs, and outputs in a multistage run detail", async () => {
    const stageOne = { ...runningStage, stage_ref: "stage-vision", stage_name: "第一阶段", status: "succeeded" as const, progress_percent: 100, current_step: 20, parameters: { train_vision: true }, output_directory: "/workspace/outputs/navila-family/v1-20260806/stage-01" };
    const stageTwo = { ...runningStage, stage_ref: "stage-language", stage_number: 2, stage_name: "第二阶段", status: "running" as const, parameters: { train_language: true }, output_directory: "/workspace/outputs/navila-family/v1-20260806/stage-02" };
    const multistageRun: TrainingRun = { ...runningRun, stage_count: 2, current_stage_number: 2, stages: [stageOne, stageTwo] };
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([multistageRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(multistageRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([
      { seq: 1, stage_ref: "stage-vision", stage_number: 1, created_at: "2026-08-06T00:01:00Z", level: "info", message: "vision complete" },
      { seq: 2, stage_ref: "stage-language", stage_number: 2, created_at: "2026-08-06T00:02:00Z", level: "info", message: "language running" },
    ]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([]);
    renderPlatform();
    fireEvent.click(await screen.findByText("NaVILA v1-20260806"));

    expect(await screen.findByText("第二阶段参数快照")).toBeVisible();
    expect(screen.getByText("train_language")).toBeVisible();
    expect(screen.getByText("language running")).toBeVisible();
    expect(screen.queryByText("vision complete")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "第一阶段 · 已完成" }));
    expect(screen.getByText("第一阶段参数快照")).toBeVisible();
    expect(screen.getByText("train_vision")).toBeVisible();
    expect(screen.getByText("vision complete")).toBeVisible();
    expect(screen.queryByText("language running")).not.toBeInTheDocument();
    expect(screen.getByText(stageOne.output_directory)).toBeVisible();
  });

  it("opens a run detail from /model/runs/:runRef and returns to /model", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([]);
    renderPlatform("/model/runs/run-running");
    expect(await screen.findByText("任务 run-running")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "返回任务列表" }));
    await waitFor(() => expect(screen.getByLabelText("状态筛选")).toBeVisible());
    expect(screen.getByTestId("location-path")).toHaveTextContent("/model");
  });

  it("leaves the run detail route when switching tabs and does not reopen it after polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi(adminCapabilities, [model]);
    vi.mocked(trainingApi.listTrainingRuns).mockImplementation(async () => [structuredClone(runningRun)]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([]);

    renderPlatform("/model/runs/run-running");
    expect(await screen.findByText("任务 run-running")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));

    expect(screen.getByRole("tab", { name: "模型注册" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("location-path")).toHaveTextContent("/model");
    expect(screen.queryByText("任务 run-running")).not.toBeInTheDocument();

    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });

    expect(screen.getByRole("tab", { name: "模型注册" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("location-path")).toHaveTextContent("/model");
  });

  it("updates the current family configuration with expected_revision", async () => {
    mockApi(adminCapabilities, [model]);
    const revised = { ...model, edit_revision: 2 };
    vi.mocked(trainingApi.updateTrainingModel).mockResolvedValue(revised);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑模型配置" }));
    const editedDefinitions = [{ ...model.configuration!.parameter_definitions[0], default: 8, display_group: "common", display_group_label: "常用参数", display_group_order: 0 }];
    fireEvent.change(screen.getByLabelText("num_video_frames 默认值"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));
    await waitFor(() => expect(trainingApi.updateTrainingModel).toHaveBeenCalledWith("navila-family", expect.objectContaining({ expected_revision: 1, configuration: { parameter_definitions: editedDefinitions, launch_template: launchTemplate } })));
    expect(screen.getByRole("button", { name: "登记新模型" })).toBeVisible();
  });

  it("validates dynamic parameter fields before model creation", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.change(screen.getByLabelText("longvila_sampler 参数字段名"), { target: { value: "bad key" } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("字段名无效");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("edits enum values as structured rows and preserves their order and default", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.change(screen.getByLabelText("模型族名称"), { target: { value: "枚举注册测试" } });
    fireEvent.change(screen.getByLabelText("领域 · Domain"), { target: { value: "vla" } });
    fireEvent.change(screen.getByLabelText("工作目录 · Working directory"), { target: { value: "/workspace/project" } });
    fireEvent.change(screen.getByLabelText("启动程序 · Executable"), { target: { value: "torchrun" } });
    fireEvent.change(screen.getByLabelText("训练入口 · Entrypoint"), { target: { value: "train.py" } });
    fireEvent.change(screen.getByLabelText("输出根目录 · Output root"), { target: { value: "/workspace/outputs" } });
    fireEvent.change(screen.getByLabelText("产物输出参数 · Output flag"), { target: { value: "--output_dir" } });
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByLabelText("parameter_1 参数字段名"), { target: { value: "optimizer" } });
    fireEvent.change(screen.getByLabelText("optimizer 类型"), { target: { value: "enum" } });

    expect(screen.queryByLabelText("optimizer 枚举选项")).not.toBeInTheDocument();
    const firstValue = screen.getByLabelText("optimizer 第 1 个枚举实际值");
    fireEvent.change(firstValue, { target: { value: "adamw_torch" } });
    expect(firstValue).toHaveValue("adamw_torch");
    expect(screen.queryByLabelText("optimizer 第 1 个枚举显示名称")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "optimizer 添加枚举选项" }));
    fireEvent.change(screen.getByLabelText("optimizer 第 2 个枚举实际值"), { target: { value: "sgd" } });
    fireEvent.click(screen.getByLabelText("optimizer 第 2 个枚举设为默认值"));
    fireEvent.change(screen.getByLabelText("optimizer 第 2 个枚举实际值"), { target: { value: "sgd_momentum" } });
    expect(screen.getByLabelText("optimizer 第 2 个枚举设为默认值")).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.configuration.parameter_definitions).toEqual([expect.objectContaining({
      key: "optimizer",
      type: "enum",
      default: "sgd_momentum",
      choices: [
        { value: "adamw_torch", label: "adamw_torch" },
        { value: "sgd_momentum", label: "sgd_momentum" },
      ],
    })]);
  });

  it("provides strict type-specific editors for numeric, string, and boolean parameters", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));

    fireEvent.change(screen.getByLabelText("parameter_1 类型"), { target: { value: "integer" } });
    const integerDefault = screen.getByLabelText("parameter_1 默认值");
    fireEvent.change(integerDefault, { target: { value: "1.5" } });
    expect(within(integerDefault.closest("label")!).getByRole("alert")).toHaveTextContent("请输入整数");
    fireEvent.change(integerDefault, { target: { value: "16" } });
    expect(within(integerDefault.closest("label")!).queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("parameter_1 最小值"), { target: { value: "20" } });
    expect(within(integerDefault.closest("label")!).getByRole("alert")).toHaveTextContent("默认值不能小于最小值");
    fireEvent.change(screen.getByLabelText("parameter_1 最大值"), { target: { value: "10" } });
    expect(within(screen.getByLabelText("parameter_1 最小值").closest("label")!).getByRole("alert")).toHaveTextContent("最小值不能大于最大值");
    expect(within(screen.getByLabelText("parameter_1 最大值").closest("label")!).getByRole("alert")).toHaveTextContent("最大值不能小于最小值");

    fireEvent.change(screen.getByLabelText("parameter_1 类型"), { target: { value: "number" } });
    fireEvent.change(screen.getByLabelText("parameter_1 默认值"), { target: { value: "0.5" } });
    fireEvent.change(screen.getByLabelText("parameter_1 最小值"), { target: { value: "1" } });
    expect(within(screen.getByLabelText("parameter_1 默认值").closest("label")!).getByRole("alert")).toHaveTextContent("默认值不能小于最小值");

    fireEvent.change(screen.getByLabelText("parameter_1 类型"), { target: { value: "string" } });
    expect(screen.getByLabelText("parameter_1 最短字符数")).toHaveValue("0");
    expect(screen.getByLabelText("parameter_1 最长字符数")).toHaveValue("512");
    fireEvent.change(screen.getByLabelText("parameter_1 最短字符数"), { target: { value: "-1" } });
    expect(within(screen.getByLabelText("parameter_1 最短字符数").closest("label")!).getByRole("alert")).toHaveTextContent("不能为负数");
    fireEvent.change(screen.getByLabelText("parameter_1 最短字符数"), { target: { value: "1.5" } });
    expect(within(screen.getByLabelText("parameter_1 最短字符数").closest("label")!).getByRole("alert")).toHaveTextContent("请输入整数");
    fireEvent.change(screen.getByLabelText("parameter_1 最短字符数"), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText("parameter_1 最长字符数"), { target: { value: "5" } });
    expect(within(screen.getByLabelText("parameter_1 最短字符数").closest("label")!).getByRole("alert")).toHaveTextContent("最短字符数不能大于最长字符数");
    expect(within(screen.getByLabelText("parameter_1 最长字符数").closest("label")!).getByRole("alert")).toHaveTextContent("最长字符数不能小于最短字符数");

    fireEvent.change(screen.getByLabelText("parameter_1 类型"), { target: { value: "boolean" } });
    expect(screen.getByText(/False 时不生成该参数/)).toBeVisible();
    expect(Array.from((screen.getByLabelText("parameter_1 argv 表达方式") as HTMLSelectElement).options, (option) => option.value)).toEqual(["explicit_boolean", "flag_when_true"]);
  });

  it("keeps exactly one string stage input while editing parameter definitions", async () => {
    mockApi(adminCapabilities);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true).mockReturnValueOnce(true);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    fireEvent.change(screen.getByLabelText("parameter_1 参数用途"), { target: { value: "stage_input" } });
    expect(screen.getByLabelText("parameter_1 参数用途")).toHaveValue("stage_input");

    fireEvent.change(screen.getByLabelText("parameter_2 参数用途"), { target: { value: "stage_input" } });
    expect(screen.getByLabelText("parameter_2 参数用途")).toHaveValue("hyperparameter");
    fireEvent.change(screen.getByLabelText("parameter_2 参数用途"), { target: { value: "stage_input" } });
    expect(screen.getByLabelText("parameter_1 参数用途")).toHaveValue("hyperparameter");
    expect(screen.getByLabelText("parameter_2 参数用途")).toHaveValue("stage_input");

    fireEvent.change(screen.getByLabelText("parameter_2 类型"), { target: { value: "integer" } });
    expect(screen.getByLabelText("parameter_2 类型")).toHaveValue("integer");
    expect(screen.getByLabelText("parameter_2 参数用途")).toHaveValue("hyperparameter");
    expect(confirm).toHaveBeenCalledTimes(3);
    confirm.mockRestore();
  });

  it("allows an optional concise parameter explanation and enforces its length", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    const explanation = screen.getByLabelText("longvila_sampler 参数解释");
    expect(explanation).toHaveAttribute("maxlength", "120");
    fireEvent.change(explanation, { target: { value: "控制是否启用长视频采样。" } });
    expect(within(explanation.closest("label")!).getByText("12/120")).toBeInTheDocument();
    fireEvent.change(explanation, { target: { value: "x".repeat(121) } });
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("不能超过 120 个字符");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("loads the complete NaVILA preset and supports adding typed parameters without a parameter count", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    expect(screen.queryByRole("region", { name: "固定参数" })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "常用参数" })).toBeVisible();
    expect(screen.getByRole("region", { name: "其他参数" })).toBeVisible();
    expect(screen.getByRole("button", { name: "管理参数分组" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    const firstAddedKey = "parameter_1";
    expect(Array.from((screen.getByLabelText(`${firstAddedKey} 展示分组`) as HTMLSelectElement).options, (option) => option.textContent)).toEqual(["常用参数（常驻）", "其他参数", "＋ 新建分组…"]);
    fireEvent.click(screen.getByRole("button", { name: `删除参数 ${firstAddedKey}` }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    expect(screen.getByLabelText("搜索训练参数")).toBeVisible();
    expect(screen.getByText("视频帧数").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByRole("region", { name: "性能与显存" })).toBeVisible();
    expect(screen.queryByLabelText("创建训练时可编辑")).not.toBeInTheDocument();
    expect(navilaTrajectoryParameters.every((parameter) => parameter.editable)).toBe(true);
    expect(screen.queryByText(/\/data\/cui\/NaVILA/)).not.toBeInTheDocument();
    expect(screen.getByText(/--nproc_per_node=<所选 GPU 数>/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "添加参数" }));
    const addedKey = `parameter_${navilaTrajectoryParameters.length + 1}`;
    fireEvent.change(screen.getByLabelText(`${addedKey} 参数字段名`), { target: { value: "custom_toggle" } });
    expect(screen.getByLabelText("custom_toggle CLI flag")).toHaveValue("--custom_toggle");
    fireEvent.change(screen.getByLabelText("custom_toggle 类型"), { target: { value: "boolean" } });
    expect(screen.getByLabelText("custom_toggle 默认值")).toHaveValue("false");
    const argumentStyle = screen.getByLabelText("custom_toggle argv 表达方式") as HTMLSelectElement;
    expect(argumentStyle).toHaveValue("explicit_boolean");
    expect(Array.from(argumentStyle.options, (option) => option.value)).toEqual(["explicit_boolean", "flag_when_true"]);
    fireEvent.click(screen.getByRole("button", { name: "删除参数 custom_toggle" }));
    fireEvent.click(screen.getByRole("button", { name: "登记模型" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalled());
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.configuration.parameter_definitions).toHaveLength(navilaTrajectoryParameters.length);
    expect(payload.configuration.parameter_definitions.map((parameter) => parameter.key)).toEqual(navilaTrajectoryParameters.map((parameter) => parameter.key));
    expect(payload.configuration.parameter_definitions.filter((parameter) => parameter.type === "boolean")).toEqual(expect.arrayContaining([expect.objectContaining({ argument_style: "explicit_boolean" })]));
  });

  it("loads logs and metrics incrementally from their last sequence", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs)
      .mockResolvedValueOnce([{ seq: 4, created_at: "2026-08-06T00:01:00Z", level: "info", message: "first log" }])
      .mockResolvedValueOnce([{ seq: 5, created_at: "2026-08-06T00:01:02Z", level: "info", message: "second log" }]);
    vi.mocked(trainingApi.getTrainingRunMetrics)
      .mockResolvedValueOnce([{ seq: 7, created_at: "2026-08-06T00:01:00Z", step: 7, total_steps: 20, epoch: 1, loss: 1.3, learning_rate: 0.0001 }])
      .mockResolvedValueOnce([{ seq: 8, created_at: "2026-08-06T00:01:02Z", step: 8, total_steps: 20, epoch: 1, loss: 1.2, learning_rate: 0.0001 }]);
    renderPlatform();
    fireEvent.click(await screen.findByText("NaVILA v1-20260806"));
    expect(await screen.findByText(/first log/)).toBeVisible();
    expect(trainingApi.getTrainingRunLogs).toHaveBeenCalledWith("run-running", 0);
    expect(trainingApi.getTrainingRunMetrics).toHaveBeenCalledWith("run-running", 0);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    await waitFor(() => expect(trainingApi.getTrainingRunLogs).toHaveBeenCalledWith("run-running", 4));
    expect(trainingApi.getTrainingRunMetrics).toHaveBeenCalledWith("run-running", 7);
    expect(await screen.findByText(/second log/)).toBeVisible();
    expect(screen.getByText(/first log/)).toBeVisible();
  });

  it("reports an SSE disconnect while polling remains active", async () => {
    vi.stubGlobal("EventSource", class EventSourceStub {});
    vi.mocked(trainingApi.openTrainingEvents).mockImplementation((_onEvent, _afterSeq, onError) => { onError?.(); return { close: vi.fn() } as unknown as EventSource; });
    renderPlatform();
    expect(await screen.findByRole("status")).toHaveTextContent("事件流已断开，正在使用轮询恢复");
    expect(trainingApi.listTrainingRuns).toHaveBeenCalled();
  });
});
