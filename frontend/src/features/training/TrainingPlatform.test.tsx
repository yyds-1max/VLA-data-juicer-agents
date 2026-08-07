import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as trainingApi from "../../api/client";
import type { TrainingCapabilities, TrainingModel, TrainingRun, TrainingServer, TrainingServerResources } from "../../api/types";
import { TrainingPlatform } from "./TrainingPlatform";
import { navilaTrajectoryParameters } from "./navilaTemplate";

vi.mock("../../api/client", () => ({
  getTrainingCapabilities: vi.fn(), listTrainingModels: vi.fn(), listTrainingServers: vi.fn(),
  getTrainingServerResources: vi.fn(), listTrainingRuns: vi.fn(), createTrainingModel: vi.fn(),
  updateTrainingModel: vi.fn(),
  previewTrainingRun: vi.fn(), createTrainingRun: vi.fn(), getTrainingRun: vi.fn(),
  getTrainingRunLogs: vi.fn(), getTrainingRunMetrics: vi.fn(), stopTrainingRun: vi.fn(),
  openTrainingEvents: vi.fn(),
}));

const readonlyCapabilities: TrainingCapabilities = {
  permissions: ["training:view"], authentication_mode: "read_only", simulation_enabled: true,
  real_execution_enabled: false, real_execution_disabled_reason: "真实执行未配置",
};
const adminCapabilities: TrainingCapabilities = { ...readonlyCapabilities, authentication_mode: "development_admin", permissions: ["training:view", "training:manage_models", "training:create_runs", "training:stop_runs"] };
const server: TrainingServer = { server_ref: "fake-local", name: "Fake A100 Server", kind: "simulation", gpu_count: 8 };
const resources: TrainingServerResources = { server, sampled_at: "2026-08-06T00:00:00Z", gpus: [{ gpu_uuid: "GPU-0", index: 0, name: "A100", total_memory_mib: 81920, used_memory_mib: 1024, utilization_percent: 2, temperature_c: 45, externally_occupied: false }] };
const launchTemplate = { domain: "vla", server_ref: "fake-local", working_directory: "/workspace/project", executable: "python", entrypoint: "train.py", fixed_argv: ["--deepspeed", "configs/zero3.json"], output_root: "/workspace/outputs", output_flag: "--output_dir" };
const model: TrainingModel = { model_ref: "navila", name: "NaVILA", description: "draft model", status: "draft", latest_revision: 1, created_at: "2026-08-06T00:00:00Z", updated_at: "2026-08-06T00:00:00Z", revision: { revision: 1, created_at: "2026-08-06T00:00:00Z", fixed_argv: launchTemplate.fixed_argv, launch_template: launchTemplate, parameter_definitions: [{ key: "num_video_frames", label: "视频帧数", type: "integer", default: 4, minimum: 1, maximum: 64, editable: true, description: "控制每个训练样本使用的视频帧数。" }] } };
const runningRun: TrainingRun = { run_ref: "run-running", model_ref: "navila", model_name: "NaVILA Running", model_revision: 1, status: "running", state_revision: 3, server_ref: "fake-local", gpu_uuids: ["GPU-0"], progress_percent: 40, current_step: 8, total_steps: 20, current_epoch: 1, total_epochs: 3, created_at: "2026-08-06T00:00:00Z", parameters: { num_video_frames: 8, learning_rate: 0.0001 }, audit_events: [{ created_at: "2026-08-06T00:01:00Z", action: "run.started", summary: "模拟训练已启动" }] };
const succeededRun: TrainingRun = { ...runningRun, run_ref: "run-succeeded", model_name: "NaVILA Succeeded", status: "succeeded", state_revision: 5, progress_percent: 100, current_step: 20 };

function renderPlatform(path = "/model") {
  function LocationProbe() { return <span data-testid="location-path">{useLocation().pathname}</span>; }
  return render(<MemoryRouter initialEntries={[path]}><TrainingPlatform /><LocationProbe /></MemoryRouter>);
}

function mockApi(capabilities = readonlyCapabilities, models: TrainingModel[] = []) {
  vi.mocked(trainingApi.getTrainingCapabilities).mockResolvedValue(capabilities);
  vi.mocked(trainingApi.listTrainingModels).mockResolvedValue(models);
  vi.mocked(trainingApi.listTrainingServers).mockResolvedValue([server]);
  vi.mocked(trainingApi.getTrainingServerResources).mockResolvedValue(resources);
  vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([]);
}

describe("TrainingPlatform", () => {
  beforeEach(() => { vi.clearAllMocks(); mockApi(); });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("shows a real-execution disabled notice and keeps write flows disabled for a read-only principal", async () => {
    renderPlatform();
    expect(await screen.findByText("真实训练未启用")).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));
    expect(await screen.findByRole("button", { name: "创建草稿" })).toBeDisabled();
    fireEvent.click(screen.getByRole("tab", { name: "新建训练" }));
    expect(await screen.findByRole("button", { name: "启动模拟训练" })).toBeDisabled();
  });

  it("sends an admin model registration payload with the structured launch template", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    fireEvent.click(screen.getByRole("tab", { name: "模型注册" }));
    expect(screen.getByLabelText("领域 · Domain")).toBeVisible();
    expect(screen.getByLabelText("服务器标识 · Server ref")).toBeVisible();
    expect(screen.getByLabelText("工作目录 · Working directory")).toBeVisible();
    expect(screen.getByLabelText("启动程序 · Executable")).toBeVisible();
    expect(screen.getByLabelText("训练入口 · Entrypoint")).toBeVisible();
    expect(screen.getByLabelText("输出根目录 · Output root")).toBeVisible();
    expect(screen.getByLabelText("输出参数标志 · Output flag")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.click(await screen.findByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    expect(trainingApi.createTrainingModel).toHaveBeenCalledWith(expect.objectContaining({
      name: "NaVILA 轨迹训练",
      launch_template: expect.objectContaining({ server_ref: "fake-local", executable: "torchrun", entrypoint: "llava/train/train_mem.py", fixed_argv: [], output_flag: "--output_dir" }),
      parameter_definitions: expect.arrayContaining([expect.objectContaining({ key: "num_video_frames", default: 4 }), expect.objectContaining({ key: "tune_vision_tower", argument_style: "explicit_boolean" }), expect.objectContaining({ key: "longvila_sampler", editable: true }), expect.objectContaining({ key: "save_steps", visible_when: { parameter_key: "save_strategy", equals: "steps" } })]),
    }));
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
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.parameter_definitions).toContainEqual(expect.objectContaining({
      key: "tune_vision_tower",
      visible_when: { parameter_key: "bf16", equals: true },
    }));
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
  });

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
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ command_preview: "python train.py --num_video_frames 8", run_spec: { contract_version: 1, execution_mode: "simulation", server_ref: "fake-local", gpu_uuids: ["GPU-0"], nnodes: 1, master_addr: "127.0.0.1", master_port: 29500, node_rank: 0, nproc_per_node: 1, environment: { CUDA_VISIBLE_DEVICES: "0" }, parameters: { num_video_frames: 8 }, argv: ["python", "train.py"] }, preflight: [{ ok: true, message: "资源可用" }] });
    vi.mocked(trainingApi.createTrainingRun).mockResolvedValue({ run_ref: "run-1", model_ref: "navila", model_name: "NaVILA", model_revision: 1, status: "queued", state_revision: 1, server_ref: "fake-a100", gpu_uuids: ["GPU-0"], progress_percent: 0, current_step: 0, total_steps: 20, current_epoch: 0, total_epochs: 1, created_at: "2026-08-06T00:00:00Z" });
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    fireEvent.click(screen.getByRole("tab", { name: "新建训练" }));
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));
    const frames = screen.getByLabelText("视频帧数");
    const parameterCard = frames.closest("label");
    expect(parameterCard).not.toBeNull();
    expect(within(parameterCard!).getByText("num_video_frames")).toBeVisible();
    expect(within(parameterCard!).queryByText("--num_video_frames")).not.toBeInTheDocument();
    const parameterHelp = within(parameterCard!).getByLabelText("视频帧数 参数说明");
    const parameterTooltip = within(parameterCard!).getByRole("tooltip");
    expect(parameterCard).not.toHaveClass("group");
    expect(parameterHelp).toHaveClass("group/help");
    expect(parameterHelp).toHaveAttribute("aria-describedby", "training-parameter-help-num_video_frames");
    expect(parameterTooltip).toHaveClass("group-hover/help:visible", "group-focus-within/help:visible");
    expect(parameterTooltip).toHaveTextContent("控制每个训练样本使用的视频帧数");
    fireEvent.change(frames, { target: { value: "8" } });
    expect(screen.getByRole("button", { name: "启动模拟训练" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ model_revision: 1, parameters: { num_video_frames: 8 }, gpu_uuids: ["GPU-0"] })));
    fireEvent.click(await screen.findByRole("button", { name: "启动模拟训练" }));
    await waitFor(() => expect(trainingApi.createTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ model_revision: 1, parameters: { num_video_frames: 8 }, execution_mode: "simulation" })));
  });

  it("preserves edited parameters and the generated preview across background polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const pollingModel: TrainingModel = {
      ...model,
      revision: {
        ...model.revision!,
        parameter_definitions: [
          { key: "test_argv", label: "测试参数", type: "integer", default: 0, minimum: 0, maximum: 10, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
        ],
      },
    };
    mockApi(adminCapabilities, [pollingModel]);
    vi.mocked(trainingApi.listTrainingModels).mockImplementation(async () => [structuredClone(pollingModel)]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ command_preview: "python train.py --test_argv 6", run_spec: { contract_version: 1, execution_mode: "simulation", server_ref: "fake-local", gpu_uuids: ["GPU-0"], nnodes: 1, master_addr: "127.0.0.1", master_port: 29500, node_rank: 0, nproc_per_node: 1, environment: { CUDA_VISIBLE_DEVICES: "0" }, parameters: { test_argv: 6 }, argv: ["python", "train.py", "--test_argv", "6"] }, preflight: [{ ok: true, message: "资源可用" }] });

    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "新建训练" }));
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
      revision: {
        ...model.revision!,
        parameter_definitions: [
          { key: "bf16", label: "启用 BF16", type: "boolean", default: false, editable: true, argument_style: "explicit_boolean" },
          { key: "learning_rate", label: "学习率", type: "number", default: 0.00001, editable: true, visible_when: { parameter_key: "bf16", equals: true } },
        ],
      },
    };
    mockApi(adminCapabilities, [conditionalModel]);
    vi.mocked(trainingApi.previewTrainingRun).mockResolvedValue({ command_preview: "python train.py --bf16 True --learning_rate 0.00002", run_spec: { contract_version: 1, execution_mode: "simulation", server_ref: "fake-local", gpu_uuids: ["GPU-0"], nnodes: 1, master_addr: "127.0.0.1", master_port: 29500, node_rank: 0, nproc_per_node: 1, environment: { CUDA_VISIBLE_DEVICES: "0" }, parameters: { bf16: true, learning_rate: 0.00002 }, argv: ["python", "train.py"] }, preflight: [{ ok: true, message: "资源可用" }] });
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "新建训练" }));
    fireEvent.click(await screen.findByLabelText("选择 GPU 0"));

    expect(screen.getByLabelText("启用 BF16")).not.toBeChecked();
    const learningRate = screen.getByLabelText("学习率");
    expect(learningRate).toBeVisible();
    expect(learningRate).toBeDisabled();
    expect(learningRate).toHaveClass("disabled:cursor-not-allowed");
    expect(learningRate.closest("label")).toHaveClass("opacity-50");
    expect(learningRate).toHaveAttribute("aria-describedby", "training-parameter-condition-learning_rate");
    expect(screen.getByText("仅当「启用 BF16」等于 True 时，「学习率」才可设置。")).toBeVisible();
    fireEvent.click(screen.getByLabelText("启用 BF16"));
    expect(learningRate).toBeEnabled();
    expect(learningRate.closest("label")).not.toHaveClass("opacity-50");
    fireEvent.change(screen.getByLabelText("学习率"), { target: { value: "0.00002" } });
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenCalledWith(expect.objectContaining({ parameters: { bf16: true, learning_rate: 0.00002 } })));

    fireEvent.click(screen.getByLabelText("启用 BF16"));
    expect(learningRate).toBeVisible();
    expect(learningRate).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(trainingApi.previewTrainingRun).toHaveBeenLastCalledWith(expect.objectContaining({ parameters: { bf16: false } })));
  });

  it("validates run values inline and masks sensitive inputs", async () => {
    const constrainedModel: TrainingModel = {
      ...model,
      revision: {
        ...model.revision!,
        parameter_definitions: [
          { key: "seed", label: "随机种子", type: "integer", default: 10, minimum: 0, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
          { key: "run_name", label: "运行名称", type: "string", default: "demo", string_min_length: 3, string_max_length: 8, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
          { key: "hub_token", label: "访问令牌", type: "string", default: "local-token", sensitive: true, editable: true, display_group: "common", display_group_label: "常用参数", display_group_order: 0 },
        ],
      },
    };
    mockApi(adminCapabilities, [constrainedModel]);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "新建训练" }));
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

  it("rejects a fixed argv flag already registered as a parameter", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.change(screen.getByLabelText("额外固定 argv（每行一个 token）"), { target: { value: "--seed=10" } });
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("额外固定 argv 与训练参数重复声明了 --seed");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("keeps common parameters visible and folds advanced and custom parameters by category", async () => {
    const groupedModel: TrainingModel = {
      ...model,
      revision: {
        ...model.revision!,
        parameter_definitions: [
          ...navilaTrajectoryParameters,
          { key: "custom_temperature", label: "自定义温度", type: "number", default: 0.5, editable: true, cli_flag: "--custom_temperature", description: "自定义模型参数。" },
        ],
      },
    };
    mockApi(adminCapabilities, [groupedModel]);
    renderPlatform();
    await screen.findByRole("tab", { name: "训练任务" });
    fireEvent.click(screen.getByRole("tab", { name: "新建训练" }));

    expect(await screen.findByRole("heading", { name: "常用参数 (18)" })).toBeVisible();
    expect(screen.getByLabelText("学习率")).toBeVisible();
    expect(screen.getByLabelText("随机种子")).toBeVisible();
    expect(screen.getByLabelText("保存 step 间隔")).toBeVisible();
    fireEvent.change(screen.getByLabelText("保存策略"), { target: { value: "epoch" } });
    expect(screen.getByLabelText("保存 step 间隔")).toBeVisible();
    expect(screen.getByLabelText("保存 step 间隔")).toBeDisabled();
    expect(screen.getByLabelText("保存 step 间隔").closest("label")).toHaveClass("opacity-50");
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
    expect(await screen.findByText("NaVILA Running")).toBeVisible();
    expect(screen.getByText("NaVILA Succeeded")).toBeVisible();
    fireEvent.change(screen.getByLabelText("状态筛选"), { target: { value: "succeeded" } });
    expect(screen.queryByText("NaVILA Running")).not.toBeInTheDocument();
    expect(screen.getByText("NaVILA Succeeded")).toBeVisible();
  });

  it("hides stop without training:stop_runs and shows run snapshots, GPU metrics and audit summary", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([{ seq: 1, created_at: "2026-08-06T00:01:00Z", step: 8, total_steps: 20, epoch: 1, loss: 1.2, learning_rate: 0.0001, gpu_utilization_percent: 76, gpu_memory_mib: 24576 }]);
    renderPlatform();
    fireEvent.click(await screen.findByText("NaVILA Running"));
    expect(await screen.findByText("参数快照")).toBeVisible();
    expect(screen.getAllByText("num_video_frames")[0]).toBeVisible();
    expect(screen.getByText("模拟训练已启动 · 2026-08-06T00:01:00Z")).toBeVisible();
    expect(screen.getByRole("heading", { name: "GPU 利用率" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "GPU 显存" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "停止任务" })).not.toBeInTheDocument();
  });

  it("opens a run detail from /model/runs/:runRef and returns to /model", async () => {
    mockApi(readonlyCapabilities);
    vi.mocked(trainingApi.listTrainingRuns).mockResolvedValue([runningRun]);
    vi.mocked(trainingApi.getTrainingRun).mockResolvedValue(runningRun);
    vi.mocked(trainingApi.getTrainingRunLogs).mockResolvedValue([]);
    vi.mocked(trainingApi.getTrainingRunMetrics).mockResolvedValue([]);
    renderPlatform("/model/runs/run-running");
    expect(await screen.findByText("任务 run-running · 模型 revision 1")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "返回任务列表" }));
    await waitFor(() => expect(screen.getByLabelText("状态筛选")).toBeVisible());
    expect(screen.getByTestId("location-path")).toHaveTextContent("/model");
  });

  it("updates a draft by creating a new immutable revision with expected_revision", async () => {
    mockApi(adminCapabilities, [model]);
    const revised = { ...model, latest_revision: 2, revision: { ...model.revision!, revision: 2 } };
    vi.mocked(trainingApi.updateTrainingModel).mockResolvedValue(revised);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "编辑并创建新 revision" }));
    const editedDefinitions = [{ ...model.revision!.parameter_definitions[0], default: 8, display_group: "common", display_group_label: "常用参数", display_group_order: 0 }];
    fireEvent.change(screen.getByLabelText("num_video_frames 默认值"), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "NaVILA revised" } });
    fireEvent.click(screen.getByRole("button", { name: "创建新 revision" }));
    await waitFor(() => expect(trainingApi.updateTrainingModel).toHaveBeenCalledWith("navila", expect.objectContaining({ expected_revision: 1, name: "NaVILA revised", parameter_definitions: editedDefinitions, launch_template: launchTemplate })));
    expect(screen.getByRole("button", { name: "创建新模型" })).toBeVisible();
  });

  it("validates dynamic parameter fields before model creation", async () => {
    mockApi(adminCapabilities);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.click(screen.getByRole("button", { name: "一键载入 NaVILA 轨迹训练模板" }));
    fireEvent.change(screen.getByLabelText("longvila_sampler 参数字段名"), { target: { value: "bad key" } });
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("字段名无效");
    expect(trainingApi.createTrainingModel).not.toHaveBeenCalled();
  });

  it("edits enum values as structured rows and preserves their order and default", async () => {
    mockApi(adminCapabilities);
    vi.mocked(trainingApi.createTrainingModel).mockResolvedValue(model);
    renderPlatform();
    fireEvent.click(await screen.findByRole("tab", { name: "模型注册" }));
    fireEvent.change(screen.getByLabelText("名称"), { target: { value: "枚举注册测试" } });
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

    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.parameter_definitions).toEqual([expect.objectContaining({
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
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
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
    fireEvent.click(screen.getByRole("button", { name: "创建草稿" }));
    await waitFor(() => expect(trainingApi.createTrainingModel).toHaveBeenCalled());
    const payload = vi.mocked(trainingApi.createTrainingModel).mock.calls[0][0];
    expect(payload.parameter_definitions).toHaveLength(navilaTrajectoryParameters.length);
    expect(payload.parameter_definitions.map((parameter) => parameter.key)).toEqual(navilaTrajectoryParameters.map((parameter) => parameter.key));
    expect(payload.parameter_definitions.filter((parameter) => parameter.type === "boolean")).toEqual(expect.arrayContaining([expect.objectContaining({ argument_style: "explicit_boolean" })]));
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
    fireEvent.click(await screen.findByText("NaVILA Running"));
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
