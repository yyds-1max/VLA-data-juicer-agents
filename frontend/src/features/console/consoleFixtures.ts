import type { StatusTone } from "./consoleTypes";

export type ActivityItem = {
  id: string;
  text: string;
  time: string;
  tone: StatusTone;
};

export type BatchStatus = "unlocked" | "pending" | "rejected";

export type BatchRow = {
  id: string;
  type: "多模态" | "图像" | "点云" | "文本";
  count: number;
  quality: number;
  scene: string;
  status: BatchStatus;
};

export type ModelVersionStatus = "training" | "deployed" | "archived" | "failed";

export type ModelVersion = {
  ver: string;
  date: string;
  status: ModelVersionStatus;
  data: string;
  epochs: string;
  success: string;
  note: string;
};

export const dashboardMetrics = [
  { id: "total-data", label: "总数据量", value: "248K", delta: "+12.4%", detail: "今日新增 12,847 条" },
  { id: "annotated-data", label: "已标注数据", value: "186K", delta: "75%", detail: "自动标注覆盖率 84%" },
  { id: "pending-batches", label: "待解锁批次", value: "23", delta: "7 批次待审核", detail: "质量阈值 >= 0.85" },
  { id: "model-versions", label: "模型版本", value: "v47", delta: "训练中", detail: "Epoch 18/24" },
];

export const dataDistribution = [
  { label: "图像数据", value: 42, color: "#15d1d8" },
  { label: "点云数据", value: 28, color: "#34d399" },
  { label: "文本指令", value: 18, color: "#fbbf24" },
  { label: "多模态数据", value: 12, color: "#a78bfa" },
];

export const modelCurveSuccess = {
  labels: ["v40", "v41", "v42", "v43", "v44", "v45", "v46", "v47*"],
  data: [87.2, 89.1, 91.5, null, 92.8, 93.1, 94.2, 94.8],
  label: "Success Rate (%)",
  color: "#00e59b",
};

export const modelCurveLoss = {
  labels: ["v40", "v41", "v42", "v43", "v44", "v45", "v46", "v47*"],
  data: [0.42, 0.35, 0.28, null, 0.24, 0.22, 0.19, 0.16],
  label: "Training Loss",
  color: "#ff4757",
};

export const activityFeed: ActivityItem[] = [
  { id: "act-001", text: "自动标注流水线完成 2,840 条数据", time: "3 分钟前", tone: "info" },
  { id: "act-002", text: "批次 B-2026-004 已自动解锁", time: "15 分钟前", tone: "success" },
  { id: "act-003", text: "v47 训练进度 75% (Epoch 18/24)", time: "32 分钟前", tone: "purple" },
  { id: "act-004", text: "批次 B-2026-007 质量分低于阈值", time: "1 小时前", tone: "warning" },
  { id: "act-005", text: "新增 1,200 条桌面操作场景数据", time: "2 小时前", tone: "success" },
  { id: "act-006", text: "v46 全量部署完成", time: "3 小时前", tone: "success" },
  { id: "act-007", text: "批次 B-2026-008 已拒绝", time: "5 小时前", tone: "danger" },
];

export const imageData = [
  { id: "IMG-000", scene: "室内导航", status: "已标注", conf: "0.96", seed: "vla000" },
  { id: "IMG-001", scene: "桌面操作", status: "已标注", conf: "0.92", seed: "vla001" },
  { id: "IMG-002", scene: "仓储物流", status: "待标注", conf: "0.78", seed: "vla002" },
  { id: "IMG-003", scene: "户外场景", status: "待标注", conf: "0.74", seed: "vla003" },
  { id: "IMG-004", scene: "室内导航", status: "已标注", conf: "0.89", seed: "vla004" },
  { id: "IMG-005", scene: "桌面操作", status: "已拒绝", conf: "0.41", seed: "vla005" },
];

export const pointCloudData = [
  { id: "PCD-000", points: 2048, scene: "室内导航", status: "已标注", conf: "0.93" },
  { id: "PCD-001", points: 4096, scene: "桌面操作", status: "待标注", conf: "0.76" },
  { id: "PCD-002", points: 1024, scene: "仓储物流", status: "已标注", conf: "0.88" },
  { id: "PCD-003", points: 8192, scene: "室内导航", status: "已标注", conf: "0.91" },
  { id: "PCD-004", points: 3072, scene: "桌面操作", status: "待标注", conf: "0.69" },
  { id: "PCD-005", points: 5120, scene: "仓储物流", status: "已标注", conf: "0.86" },
];

export const textInstructionData = [
  { id: "TXT-001", instruction: "将红色杯子从桌面左侧移动到右侧的蓝色托盘上", action_type: "pick_and_place", objects: ["红色杯子", "蓝色托盘"], status: "已标注", conf: "0.94" },
  { id: "TXT-002", instruction: "推开障碍物，导航至目标位置 (3.2, 1.5)", action_type: "navigation", objects: ["障碍物"], status: "已标注", conf: "0.88" },
  { id: "TXT-003", instruction: "抓取货架第二层的盒子并放置到传送带上", action_type: "pick_and_place", objects: ["盒子", "货架", "传送带"], status: "待标注", conf: "0.72" },
  { id: "TXT-004", instruction: "绕过桌子，避开地上的电缆，走到门旁边", action_type: "navigation", objects: ["桌子", "电缆", "门"], status: "已标注", conf: "0.91" },
  { id: "TXT-005", instruction: "将散落的零件按颜色分类放入对应的收纳箱", action_type: "sorting", objects: ["零件", "收纳箱"], status: "待标注", conf: "0.65" },
  { id: "TXT-006", instruction: "按下墙上的开关，等待灯光变绿后通过", action_type: "interaction", objects: ["开关"], status: "已标注", conf: "0.87" },
  { id: "TXT-007", instruction: "将托盘上的三瓶水整齐排列在冰箱第一层", action_type: "arrangement", objects: ["水瓶", "托盘", "冰箱"], status: "已拒绝", conf: "0.41" },
  { id: "TXT-008", instruction: "打开抽屉取出螺丝刀，递给操作员", action_type: "handover", objects: ["抽屉", "螺丝刀"], status: "已标注", conf: "0.83" },
];

export const batchData: BatchRow[] = [
  { id: "B-2026-001", type: "多模态", count: 12400, quality: 0.94, scene: "室内导航", status: "unlocked" },
  { id: "B-2026-002", type: "图像", count: 8600, quality: 0.91, scene: "桌面操作", status: "unlocked" },
  { id: "B-2026-003", type: "点云", count: 3200, quality: 0.89, scene: "仓储物流", status: "unlocked" },
  { id: "B-2026-004", type: "文本", count: 15800, quality: 0.87, scene: "室内导航", status: "unlocked" },
  { id: "B-2026-005", type: "多模态", count: 9400, quality: 0.86, scene: "桌面操作", status: "pending" },
  { id: "B-2026-006", type: "图像", count: 6200, quality: 0.84, scene: "户外场景", status: "pending" },
  { id: "B-2026-007", type: "点云", count: 2800, quality: 0.82, scene: "仓储物流", status: "pending" },
  { id: "B-2026-008", type: "文本", count: 11200, quality: 0.79, scene: "桌面操作", status: "rejected" },
  { id: "B-2026-009", type: "多模态", count: 7600, quality: 0.76, scene: "室内导航", status: "pending" },
  { id: "B-2026-010", type: "图像", count: 5100, quality: 0.72, scene: "户外场景", status: "rejected" },
];

export const annotationResults = [
  { id: "ANN-82401", type: "目标检测", model: "DINO-v2 + SAM", input: "IMG-000", output: "5 个目标框", conf: 0.92, time: "128ms" },
  { id: "ANN-82402", type: "点云分割", model: "PointGroup++", input: "PCD-000", output: "3 个实例", conf: 0.87, time: "345ms" },
  { id: "ANN-82403", type: "指令生成", model: "LLaMA-3-8B", input: "IMG-001 + PCD-001", output: "\"抓取桌面上的蓝色瓶子...\"", conf: 0.79, time: "1.2s" },
  { id: "ANN-82404", type: "多模态对齐", model: "CLIP + PointCLIP", input: "IMG-002 + TXT-001", output: "对齐分数 0.91", conf: 0.91, time: "89ms" },
  { id: "ANN-82405", type: "动作分解", model: "ActBERT-v2", input: "TXT-002", output: "4 步动作序列", conf: 0.84, time: "420ms" },
];

export const modelVersions: ModelVersion[] = [
  { ver: "v47", date: "2026-01-18", status: "training", data: "192K", epochs: "24/24", success: "-", note: "加入 B-2026-001~004 解锁数据" },
  { ver: "v46", date: "2026-01-15", status: "deployed", data: "186K", epochs: "24/24", success: "94.2%", note: "当前生产版本，灰度发布后全量" },
  { ver: "v45", date: "2026-01-12", status: "archived", data: "178K", epochs: "20/24", success: "93.1%", note: "提前停止，过拟合趋势" },
  { ver: "v44", date: "2026-01-08", status: "archived", data: "172K", epochs: "24/24", success: "92.8%", note: "增加点云数据增强策略" },
  { ver: "v43", date: "2026-01-04", status: "failed", data: "165K", epochs: "8/24", success: "-", note: "数据质量异常，训练崩溃" },
  { ver: "v42", date: "2026-01-02", status: "archived", data: "160K", epochs: "24/24", success: "91.5%", note: "基线版本，引入多模态对齐损失" },
];

export const agentNodes = [
  { id: "an-1", name: "数据源接入", category: "数据源", desc: "从多个数据源拉取原始数据，支持图像/点云/文本三种模态" },
  { id: "an-2", name: "预处理管线", category: "处理", desc: "对原始数据进行清洗、裁剪、增强等预处理操作" },
  { id: "an-3", name: "自动标注", category: "标注器", desc: "调用多模型集成进行自动标注，输出结构化标注结果" },
  { id: "an-4", name: "质量检查", category: "质量", desc: "对标注结果进行多维度质量评估" },
  { id: "an-5", name: "条件分支", category: "分支", desc: "根据质量检查结果分流数据" },
  { id: "an-6", name: "模型训练", category: "模型", desc: "使用高质量标注数据进行模型迭代训练" },
  { id: "an-7", name: "仿真评估", category: "仿真", desc: "在测试/仿真环境中评估模型成功率" },
  { id: "an-8", name: "部署发布", category: "发布", desc: "通过灰度发布策略上线新版本模型" },
];

export const agentConnections = [
  ["an-1", "an-2"],
  ["an-2", "an-3"],
  ["an-3", "an-4"],
  ["an-4", "an-5"],
  ["an-5", "an-6"],
  ["an-5", "an-2"],
  ["an-6", "an-7"],
  ["an-7", "an-8"],
] as const;

export const simulationReportRows = [
  { name: "TC-001: 基础导航", scene: "室内导航", count: 250, sr: 95.2, latency: 165, collisions: 0, rating: "A" },
  { name: "TC-002: 动态避障", scene: "室内导航", count: 180, sr: 88.3, latency: 198, collisions: 2, rating: "B+" },
  { name: "TC-003: 物体抓取", scene: "桌面操作", count: 150, sr: 92.7, latency: 175, collisions: 0, rating: "A-" },
  { name: "TC-004: 多步指令", scene: "仓储物流", count: 200, sr: 89.5, latency: 210, collisions: 1, rating: "B+" },
  { name: "TC-005: 复杂环境", scene: "户外复杂", count: 120, sr: 85.8, latency: 235, collisions: 3, rating: "B" },
  { name: "TC-006: 紧急制动", scene: "室内导航", count: 100, sr: 98.0, latency: 145, collisions: 0, rating: "A+" },
  { name: "TC-007: 协同作业", scene: "仓储物流", count: 147, sr: 91.2, latency: 188, collisions: 1, rating: "A-" },
];
