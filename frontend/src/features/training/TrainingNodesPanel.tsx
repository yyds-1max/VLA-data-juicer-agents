import { ChevronDown, CircleHelp, Eye, EyeOff, KeyRound, Plus, RefreshCw, Server, ShieldCheck, Trash2, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { ApiResponseError, createTrainingNode, deleteTrainingNode, deployTrainingNodeWorker, discoverTrainingNodeHostKey, preflightTrainingNodeWorker, removeTrainingNodeWorker } from "../../api/client";
import type { TrainingNode, TrainingNodeHostKey, TrainingNodePreflightResult, TrainingNodeStatus } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { StatusTag } from "../../components/console/StatusTag";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import type { StatusTone } from "../console/consoleTypes";

const statusMeta: Record<TrainingNodeStatus, { label: string; tone: StatusTone }> = {
  pending_enrollment: { label: "待部署 Worker", tone: "warning" },
  online: { label: "在线", tone: "success" },
  degraded: { label: "状态异常", tone: "warning" },
  offline: { label: "离线", tone: "neutral" },
  repair_required: { label: "需要修复", tone: "danger" },
  disabled: { label: "已停用", tone: "neutral" },
};

const deploymentStatusLabel = {
  not_started: "未安装",
  deploying: "安装中",
  succeeded: "已安装",
  failed: "安装失败",
} as const;

function nodeHasInstalledWorker(node: TrainingNode | null) {
  return Boolean(node && (
    node.deployment_status === "succeeded"
    || node.installed_worker_version
    || node.worker_version
    || node.enrolled_at
  ));
}

function connectionSummary(node: TrainingNode, hasInstalledWorker: boolean) {
  if (!hasInstalledWorker) return "Worker 尚未安装，当前不能用于创建训练。";
  if (node.status === "online") return "Worker 已连接中心服务，节点可以用于创建训练。";
  if (node.status === "degraded") return "Worker 已连接，但节点状态异常，暂时不能用于创建训练。";
  if (node.status === "repair_required") return "Worker 需要修复，修复完成前不能用于创建训练。";
  if (node.status === "disabled") return "节点已停用，当前不能用于创建训练。";
  return "Worker 已安装，但当前未连接中心服务。";
}

function errorText(error: unknown) {
  if (error instanceof ApiResponseError) {
    const detail = error.body && typeof error.body === "object" && "detail" in error.body ? (error.body as { detail?: unknown }).detail : null;
    const code = detail && typeof detail === "object" && "code" in detail ? (detail as { code?: unknown }).code : null;
    if (code === "training_node_deployment_account_insufficient") return "部署账号权限不足。请改用具有 root 或 sudo 权限的 SSH 账号重新部署。";
    if (code === "training_node_ssh_authentication_failed") return "SSH 登录失败，请检查部署账号和密码。";
    if (code === "training_node_host_key_mismatch") return "服务器主机指纹已经变化，已停止部署。请先向节点管理员确认。";
    const message = detail && typeof detail === "object" && "message" in detail ? (detail as { message?: unknown }).message : null;
    if (typeof message === "string") return message;
  }
  if (error && typeof error === "object" && "message" in error && typeof error.message === "string") return error.message;
  return "请求失败，请稍后重试。";
}

function formatTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

const inputClass = "mt-1 h-9 w-full rounded-md border border-console-line bg-console-panel px-2 text-sm text-console-text focus:border-console-cyan focus:outline-none disabled:bg-slate-100";

function PasswordField({
  id,
  label,
  value,
  visible,
  disabled,
  autoComplete,
  onChange,
  onToggle,
}: {
  id: string;
  label: string;
  value: string;
  visible: boolean;
  disabled: boolean;
  autoComplete: string;
  onChange: (value: string) => void;
  onToggle: () => void;
}) {
  return <div className="text-sm text-console-muted">
    <label htmlFor={id}>{label}</label>
    <div className="relative">
      <input id={id} aria-label={label} className={`${inputClass} pr-10`} type={visible ? "text" : "password"} autoComplete={autoComplete} value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} />
      <button type="button" className="absolute inset-y-1 right-1 mt-1 flex w-8 items-center justify-center rounded text-console-muted hover:bg-slate-100 hover:text-console-text focus:outline-none focus:ring-2 focus:ring-console-cyan disabled:cursor-not-allowed disabled:opacity-50" aria-label={`${visible ? "隐藏" : "显示"} ${label}`} aria-pressed={visible} disabled={disabled} onClick={onToggle}>
        {visible ? <EyeOff className="h-4 w-4" aria-hidden="true" /> : <Eye className="h-4 w-4" aria-hidden="true" />}
      </button>
    </div>
  </div>;
}

export function TrainingNodesPanel({
  nodes,
  canManage,
  deploymentEnabled,
  deploymentDisabledReason,
  onChanged,
  onDeleted,
  onViewResources,
}: {
  nodes: TrainingNode[];
  canManage: boolean;
  deploymentEnabled: boolean;
  deploymentDisabledReason?: string | null;
  onChanged: (node: TrainingNode) => void;
  onDeleted: (nodeRef: string) => void;
  onViewResources: () => void;
}) {
  const [selectedRef, setSelectedRef] = useState<string>(nodes[0]?.node_ref ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [address, setAddress] = useState("");
  const [sshPort, setSshPort] = useState("22");
  const [registrationOpen, setRegistrationOpen] = useState(nodes.length === 0);
  const [moreActionsOpen, setMoreActionsOpen] = useState(false);
  const [sshUsername, setSshUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [deployingRef, setDeployingRef] = useState<string | null>(null);
  const [deploymentMode, setDeploymentMode] = useState<"deploy" | "repair" | "update" | "change_account">("deploy");
  const [hostKey, setHostKey] = useState<TrainingNodeHostKey | null>(null);
  const [hostKeyConfirmed, setHostKeyConfirmed] = useState(false);
  const [sshPassword, setSshPassword] = useState("");
  const [sshPasswordVisible, setSshPasswordVisible] = useState(false);
  const [sudoPasswordMode, setSudoPasswordMode] = useState<"same_as_ssh" | "separate" | "not_required">("same_as_ssh");
  const [sudoPassword, setSudoPassword] = useState("");
  const [sudoPasswordVisible, setSudoPasswordVisible] = useState(false);
  const [preflight, setPreflight] = useState<TrainingNodePreflightResult | null>(null);
  const [removalOpen, setRemovalOpen] = useState(false);
  const [removalConfirmOpen, setRemovalConfirmOpen] = useState(false);
  const [removalPurpose, setRemovalPurpose] = useState<"worker" | "node">("worker");
  const [removalNodeRef, setRemovalNodeRef] = useState<string | null>(null);
  const [removalSshUsername, setRemovalSshUsername] = useState("");
  const [removalSshPassword, setRemovalSshPassword] = useState("");
  const [removalSshPasswordVisible, setRemovalSshPasswordVisible] = useState(false);
  const [removalSudoPasswordMode, setRemovalSudoPasswordMode] = useState<"same_as_ssh" | "separate" | "not_required">("same_as_ssh");
  const [removalSudoPassword, setRemovalSudoPassword] = useState("");
  const [removalSudoPasswordVisible, setRemovalSudoPasswordVisible] = useState(false);
  const selected = nodes.find((node) => node.node_ref === selectedRef) ?? nodes[0] ?? null;
  const removalNode = nodes.find((node) => node.node_ref === removalNodeRef) ?? null;
  const port = Number(sshPort);
  const valid = Boolean(name.trim() && address.trim() && Number.isInteger(port) && port >= 1 && port <= 65535);
  const hasInstalledWorker = nodeHasInstalledWorker(selected);
  const showRegistrationForm = nodes.length === 0 || registrationOpen;
  const privilegeCheck = preflight?.checks.find((check) => check.code === "deployment_privilege");

  const openDeploymentFor = async (node: TrainingNode, mode: "deploy" | "repair" | "update" | "change_account") => {
    setBusy(true); setMessage(null); setSelectedRef(node.node_ref); setDeployingRef(node.node_ref); setDeploymentMode(mode); setSshUsername(node.ssh_username ?? ""); setHostKey(null); setHostKeyConfirmed(false); setSshPassword(""); setSshPasswordVisible(false); setSudoPasswordMode("same_as_ssh"); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null); setMoreActionsOpen(false);
    try {
      setHostKey(await discoverTrainingNodeHostKey(node.node_ref));
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const create = async () => {
    if (!valid) return;
    setBusy(true); setMessage(null); setDeployingRef(null); setHostKey(null);
    try {
      const node = await createTrainingNode({ name: name.trim(), description: description.trim() || undefined, address: address.trim(), ssh_port: port });
      onChanged(node); setName(""); setDescription(""); setAddress(""); setSshPort("22");
      setRegistrationOpen(false);
      await openDeploymentFor(node, "deploy");
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const checkPreflight = async () => {
    if (!selected || !sshUsername.trim() || !hostKey || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)) return;
    setBusy(true); setMessage(null); setPreflight(null);
    try {
      setPreflight(await preflightTrainingNodeWorker(selected.node_ref, {
        expected_revision: selected.state_revision,
        ssh_username: sshUsername.trim(),
        confirmed_host_key: hostKey,
        host_key_confirmed: true,
        ssh_password: sshPassword,
        sudo_password_mode: sudoPasswordMode,
        ...(sudoPasswordMode === "separate" ? { sudo_password: sudoPassword } : {}),
      }));
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const deploy = async () => {
    if (!selected || !sshUsername.trim() || !hostKey || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)) return;
    setBusy(true); setMessage(null);
    try {
      const result = await deployTrainingNodeWorker(selected.node_ref, {
        expected_revision: selected.state_revision,
        ssh_username: sshUsername.trim(),
        confirmed_host_key: hostKey,
        host_key_confirmed: true,
        ssh_password: sshPassword,
        sudo_password_mode: sudoPasswordMode,
        ...(sudoPasswordMode === "separate" ? { sudo_password: sudoPassword } : {}),
      });
      onChanged(result.node); setDeployingRef(null); setHostKey(null); setHostKeyConfirmed(false); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null);
      setMessage(deploymentMode === "update" ? "Worker 已更新并完成重新注册，正在等待稳定心跳。" : "Worker 已自动部署并完成注册，正在等待稳定心跳。");
    } catch (error) { setMessage(errorText(error)); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); } finally { setBusy(false); }
  };

  const resetRemoval = (node: TrainingNode | null = removalNode) => {
    setRemovalSshPassword("");
    setRemovalSshPasswordVisible(false);
    setRemovalSudoPasswordMode("same_as_ssh");
    setRemovalSudoPassword("");
    setRemovalSudoPasswordVisible(false);
    setRemovalSshUsername(node?.ssh_username ?? "");
  };

  const openRemoval = (purpose: "worker" | "node", node: TrainingNode | null = selected) => {
    if (!node) return;
    setMessage(null);
    setSelectedRef(node.node_ref);
    setRemovalNodeRef(node.node_ref);
    setRemovalPurpose(purpose);
    resetRemoval(node);
    const needsSshRemoval = nodeHasInstalledWorker(node);
    setRemovalConfirmOpen(!needsSshRemoval);
    setRemovalOpen(needsSshRemoval);
  };

  const completeRemoval = async () => {
    if (!removalNode) return;
    const removalHasInstalledWorker = nodeHasInstalledWorker(removalNode);
    if (removalHasInstalledWorker && (!removalSshUsername.trim() || !removalSshPassword || (removalSudoPasswordMode === "separate" && !removalSudoPassword))) return;
    setBusy(true); setMessage(null);
    try {
      let currentNode = removalNode;
      if (removalHasInstalledWorker) {
        const result = await removeTrainingNodeWorker(removalNode.node_ref, {
          expected_revision: removalNode.state_revision,
          ssh_username: removalSshUsername.trim(),
          ssh_password: removalSshPassword,
          sudo_password_mode: removalSudoPasswordMode,
          ...(removalSudoPasswordMode === "separate" ? { sudo_password: removalSudoPassword } : {}),
        });
        currentNode = result.node;
        onChanged(currentNode);
      }
      if (removalPurpose === "node") {
        await deleteTrainingNode(currentNode.node_ref, currentNode.state_revision);
        onDeleted(currentNode.node_ref);
        setMessage("训练节点已删除。模型工程、数据集、训练产物和 SSH 登录账号均未改动。");
      } else {
        setMessage("Worker 已卸载。节点记录仍然保留，在重新部署 Worker 前不能用于训练。");
      }
      setRemovalConfirmOpen(false);
      setRemovalOpen(false);
      setRemovalNodeRef(null);
      resetRemoval(null);
    } catch (error) {
      setMessage(errorText(error));
      setRemovalConfirmOpen(false);
      setRemovalSshPassword("");
      setRemovalSshPasswordVisible(false);
      setRemovalSudoPassword("");
      setRemovalSudoPasswordVisible(false);
    } finally { setBusy(false); }
  };

  return (
    <section className="space-y-5" aria-labelledby="training-nodes-title">
      <header className="border-b border-console-line pb-5">
        <h2 id="training-nodes-title" className="text-xl font-semibold text-console-text">训练节点</h2>
        <p className="mt-1 text-sm text-console-muted">登记训练服务器、部署 Worker 并处理连接状态。CPU、内存、磁盘和 GPU 统一在“服务器资源”查看。</p>
      </header>

      <div className={nodes.length ? "grid gap-4 xl:grid-cols-[minmax(320px,.72fr)_minmax(0,1.28fr)]" : "max-w-3xl"}>
        <div className="space-y-4">
          <ConsoleCard className="h-fit shadow-none">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2"><Plus className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">登记训练节点</h3></div>
                <p className="mt-1 text-xs leading-5 text-console-muted">登记服务器地址后，系统将继续检查连接并部署 Worker。SSH 凭据只用于本次操作，不会保存。</p>
              </div>
              {!showRegistrationForm ? <ConsoleButton disabled={!canManage || !deploymentEnabled || busy} onClick={() => setRegistrationOpen(true)}><Plus className="h-4 w-4" />登记新节点</ConsoleButton> : null}
            </div>
            {showRegistrationForm ? <>
              <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
                <label className="text-sm text-console-muted">节点名称<input className={inputClass} value={name} disabled={!canManage || busy} onChange={(event) => setName(event.target.value)} /></label>
                <label className="text-sm text-console-muted">主机地址<input className={inputClass} placeholder="例如 10.0.0.12" value={address} disabled={!canManage || busy} onChange={(event) => setAddress(event.target.value)} /></label>
                <label className="text-sm text-console-muted">SSH 端口<input className={inputClass} inputMode="numeric" value={sshPort} disabled={!canManage || busy} onChange={(event) => setSshPort(event.target.value)} /></label>
              </div>
              <label className="mt-3 block text-sm text-console-muted">说明（可选）<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 text-sm text-console-text" value={description} disabled={!canManage || busy} onChange={(event) => setDescription(event.target.value)} /></label>
              <div className="mt-4 flex flex-wrap gap-2">
                <ConsoleButton variant="primary" disabled={!canManage || !deploymentEnabled || busy || !valid} onClick={() => void create()}><Plus className="h-4 w-4" />登记并部署 Worker</ConsoleButton>
                {nodes.length ? <ConsoleButton disabled={busy} onClick={() => setRegistrationOpen(false)}>取消</ConsoleButton> : null}
              </div>
            </> : null}
            {!canManage ? <p className="mt-3 text-xs text-console-muted">当前身份仅可查看节点，不能登记或部署 Worker。</p> : null}
            {canManage && !deploymentEnabled ? <p className="mt-3 text-xs leading-5 text-amber-700">{deploymentDisabledReason || "系统尚未配置可供训练节点访问的中心 HTTPS 地址，暂不能部署 Worker。"}</p> : null}
          </ConsoleCard>

          <ConsoleCard className="shadow-none">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><h3 className="font-semibold text-console-text">已登记节点</h3><p className="mt-1 text-xs text-console-muted">在线状态由中心服务根据最近心跳计算。</p></div>
              <span className="text-xs text-console-muted">共 {nodes.length} 台</span>
            </div>
            <div className="mt-4 grid gap-2">
              {nodes.map((node) => {
                const installed = nodeHasInstalledWorker(node);
                const deleteDisabled = !canManage || busy || node.deployment_status === "deploying" || (installed && !deploymentEnabled);
                return <div key={node.node_ref} className="relative">
                  <button type="button" aria-pressed={selected?.node_ref === node.node_ref} onClick={() => { setSelectedRef(node.node_ref); setDeployingRef(null); setHostKey(null); setPreflight(null); setMessage(null); setMoreActionsOpen(false); }} className={`w-full rounded-lg border p-3 text-left ${canManage ? "pr-12" : ""} ${selected?.node_ref === node.node_ref ? "border-console-cyan bg-sky-50" : "border-console-line bg-console-panel2 hover:border-console-cyan/40"}`}>
                    <span className="flex items-center justify-between gap-2"><span className="font-medium text-console-text">{node.name}</span><StatusTag tone={statusMeta[node.status].tone}>{statusMeta[node.status].label}</StatusTag></span>
                    <span className="mt-2 block truncate font-mono text-xs text-console-muted">{node.address ? `${node.address}:${node.ssh_port ?? 22}` : "连接信息仅管理员可见"}</span>
                    {installed && node.ssh_username ? <span className="mt-1 block truncate text-xs text-console-muted">Worker/训练运行账号：{node.ssh_username}</span> : null}
                    <span className="mt-1 block truncate text-xs text-console-muted">最近心跳 {formatTime(node.last_heartbeat_at)}</span>
                  </button>
                  {canManage ? <button type="button" aria-label={`删除训练节点 ${node.name}`} title={deleteDisabled ? "当前状态下不能删除训练节点" : "删除训练节点"} disabled={deleteDisabled} onClick={() => openRemoval("node", node)} className="absolute bottom-2 right-2 flex h-8 w-8 items-center justify-center rounded-md text-red-600 transition-colors hover:bg-red-100 hover:text-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-40">
                    <Trash2 className="h-4 w-4" aria-hidden="true" />
                  </button> : null}
                </div>;
              })}
            </div>
            {!nodes.length ? <div className="py-10 text-center"><Server className="mx-auto h-8 w-8 text-console-muted" /><p className="mt-3 text-sm text-console-muted">尚未登记训练节点。</p></div> : null}
          </ConsoleCard>
        </div>

        {selected ? <div className="space-y-4">
          <ConsoleCard className="shadow-none">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><div className="flex items-center gap-2"><ShieldCheck className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">{selected.name}</h3></div><p className="mt-1 text-xs text-console-muted">{selected.address ? `${selected.address}:${selected.ssh_port ?? 22}` : "连接信息仅管理员可见"}</p></div>
                <StatusTag tone={statusMeta[selected.status].tone}>{statusMeta[selected.status].label}</StatusTag>
              </div>
              <dl className="mt-4 grid gap-x-6 gap-y-4 border-y border-console-line py-4 text-sm sm:grid-cols-2 xl:grid-cols-4">
                <div><dt className="text-xs text-console-muted">Worker</dt><dd className="mt-1 text-console-text">{deploymentStatusLabel[selected.deployment_status ?? "not_started"]}</dd>{hasInstalledWorker ? <span className="mt-1 block text-xs text-console-muted">版本 {selected.worker_version ?? selected.installed_worker_version ?? "--"}</span> : null}</div>
                <div><dt className="text-xs text-console-muted">节点连接</dt><dd className="mt-1 text-console-text">{statusMeta[selected.status].label}</dd></div>
                <div><dt className="text-xs text-console-muted">Worker/训练运行账号</dt><dd className="mt-1 text-console-text">{hasInstalledWorker ? selected.ssh_username ?? "未记录" : "尚未部署"}</dd></div>
                <div><dt className="text-xs text-console-muted">最近心跳</dt><dd className="mt-1 text-console-text">{formatTime(selected.last_heartbeat_at)}</dd></div>
              </dl>
              <p className={`mt-3 rounded-md px-3 py-2 text-sm ${selected.status === "online" ? "bg-emerald-50 text-emerald-800" : selected.status === "pending_enrollment" ? "bg-slate-50 text-console-muted" : "bg-amber-50 text-amber-800"}`}>{connectionSummary(selected, hasInstalledWorker)}</p>
              {selected.deployment_status === "failed" && selected.deployment_message ? <p className="mt-3 text-sm text-red-700">{selected.deployment_message}</p> : null}
              {selected.health_message ? <p className="mt-3 text-sm text-amber-700">{selected.health_message}</p> : null}
              <div className="mt-4 flex flex-wrap items-center gap-2">
                {!hasInstalledWorker ? <ConsoleButton variant="primary" disabled={!canManage || !deploymentEnabled || busy || selected.status === "disabled" || selected.deployment_status === "deploying"} onClick={() => void openDeploymentFor(selected, "deploy")}><KeyRound className="h-4 w-4" />部署 Worker</ConsoleButton> : selected.status === "online" ? <ConsoleButton variant="primary" onClick={onViewResources}><Server className="h-4 w-4" />查看服务器资源</ConsoleButton> : selected.status !== "disabled" ? <ConsoleButton variant="primary" disabled={!canManage || !deploymentEnabled || busy || selected.deployment_status === "deploying"} onClick={() => void openDeploymentFor(selected, "repair")}><KeyRound className="h-4 w-4" />修复 Worker</ConsoleButton> : null}
                {hasInstalledWorker && selected.status === "online" ? <ConsoleButton disabled={!canManage || !deploymentEnabled || busy || selected.deployment_status === "deploying"} onClick={() => void openDeploymentFor(selected, "update")}><RefreshCw className="h-4 w-4" />更新 Worker</ConsoleButton> : null}
                {hasInstalledWorker ? <ConsoleButton aria-expanded={moreActionsOpen} aria-controls="training-node-more-actions" disabled={!canManage || busy} onClick={() => setMoreActionsOpen((current) => !current)}><ChevronDown className={`h-4 w-4 transition-transform ${moreActionsOpen ? "rotate-180" : ""}`} />更多操作</ConsoleButton> : null}
                {hasInstalledWorker ? <ConsoleButton className="border-red-200 text-red-700 hover:border-red-300 hover:bg-red-50" disabled={!canManage || !deploymentEnabled || busy || selected.deployment_status === "deploying"} onClick={() => openRemoval("worker", selected)}><Trash2 className="h-4 w-4" />卸载 Worker（保留节点）</ConsoleButton> : null}
              </div>
              {moreActionsOpen && hasInstalledWorker ? <section id="training-node-more-actions" aria-label="更多节点操作" className="mt-3 rounded-md border border-console-line bg-console-panel2 p-3">
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <div><p className="text-sm font-medium text-console-text">更换运行账号</p><p className="mt-1 text-xs leading-5 text-console-muted">改用另一个 SSH 账号重新部署。之后的 Worker 和训练任务都由新账号运行。</p></div>
                  <ConsoleButton className="shrink-0" disabled={!deploymentEnabled || busy || selected.status === "disabled" || selected.deployment_status === "deploying"} onClick={() => void openDeploymentFor(selected, "change_account")}><KeyRound className="h-4 w-4" />更换Worker和训练所属账号</ConsoleButton>
                </div>
              </section> : null}
              {!deploymentEnabled ? <p className="mt-2 text-xs text-amber-700">{deploymentDisabledReason || "系统尚未配置可供训练节点访问的中心 HTTPS 地址，暂不能部署 Worker。"}</p> : null}
              {deployingRef === selected.node_ref && hostKey ? <div className="mt-4 space-y-3 rounded-md border border-sky-200 bg-sky-50 p-4">
                <div>
                  <p className="text-sm font-medium text-sky-950">{deploymentMode === "change_account" ? "更换 Worker 和训练所属账号" : deploymentMode === "repair" ? "修复 Worker" : deploymentMode === "update" ? "更新 Worker" : "部署 Worker"}</p>
                  <p className="mt-1 text-xs leading-5 text-sky-800">请将下方 SHA256 指纹与训练节点管理员提供的指纹逐字核对。确认一致后再继续。</p>
                  <code className="mt-2 block break-all rounded bg-white p-2 text-xs text-sky-950">{hostKey.sha256_fingerprint}</code>
                  <details className="mt-2 text-xs text-sky-900"><summary className="inline-flex cursor-pointer items-center gap-1 font-medium"><CircleHelp className="h-3.5 w-3.5" />如何确认主机指纹？</summary><p className="mt-1 max-w-2xl leading-5">向训练节点管理员索取该服务器的 SSH SHA256 指纹，并与上方内容逐字核对。无法确认时请不要继续部署。</p></details>
                </div>
                <label className="flex items-start gap-2 text-sm text-sky-950"><input className="mt-0.5" type="checkbox" checked={hostKeyConfirmed} disabled={busy} onChange={(event) => { setHostKeyConfirmed(event.target.checked); setPreflight(null); }} /><span>我已确认该主机指纹正确</span></label>
                <label className="block text-sm text-console-muted">SSH 登录账号<input aria-label="SSH 登录账号" className={inputClass} autoComplete="username" value={sshUsername} disabled={busy || ((deploymentMode === "repair" || deploymentMode === "update") && Boolean(selected.ssh_username))} onChange={(event) => { setSshUsername(event.target.value); setPreflight(null); }} /><span className="mt-1 block text-xs leading-5">{deploymentMode === "repair" ? "修复会继续使用当前运行账号，不会更换账号。" : deploymentMode === "update" ? "更新会继续使用当前运行账号，并部署中心服务提供的 Worker 版本。" : "部署成功后，Worker 和之后的训练任务都使用该账号运行。"}</span></label>
                <PasswordField id="training-node-ssh-password" label="SSH 登录密码" value={sshPassword} visible={sshPasswordVisible} disabled={busy} autoComplete="current-password" onChange={(value) => { setSshPassword(value); setPreflight(null); }} onToggle={() => setSshPasswordVisible((current) => !current)} />
                {sudoPasswordMode === "separate" ? <div className="space-y-2"><PasswordField id="training-node-sudo-password" label="独立 sudo 密码" value={sudoPassword} visible={sudoPasswordVisible} disabled={busy} autoComplete="off" onChange={(value) => { setSudoPassword(value); setPreflight(null); }} onToggle={() => setSudoPasswordVisible((current) => !current)} /><button type="button" className="text-xs font-medium text-console-cyan hover:underline" disabled={busy} onClick={() => { setSudoPasswordMode("same_as_ssh"); setSudoPassword(""); setPreflight(null); }}>改用 SSH 登录密码检查权限</button></div> : <button type="button" className="text-xs font-medium text-console-cyan hover:underline" disabled={busy} onClick={() => { setSudoPasswordMode("separate"); setPreflight(null); }}>sudo 密码与登录密码不同？</button>}
                <p className="text-xs leading-5 text-sky-800">系统会自动检查该账号是否为 root、是否具有免密 sudo，或登录密码能否用于 sudo。无需提前判断提权方式。检查过程不会修改训练节点，密码也不会保存。</p>
                {privilegeCheck?.status === "failed" ? <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">部署账号权限不足。若该账号使用独立 sudo 密码，请填写后重新检查；否则请更换具有安装权限的 SSH 账号。</p> : null}
                {preflight ? <section aria-label="Worker 部署条件检查" className={`rounded-md border p-3 ${preflight.ready ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"}`}><div className="flex items-center justify-between gap-2"><p className="text-sm font-medium text-console-text">{preflight.ready ? "部署条件已满足" : "存在阻止部署的问题"}</p><span className="text-xs text-console-muted">{formatTime(preflight.checked_at)}</span></div><ul className="mt-2 grid gap-2 sm:grid-cols-2">{preflight.checks.map((check) => <li key={check.code} className="rounded bg-white/80 p-2 text-xs"><div className="flex items-center gap-2"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${check.status === "passed" ? "bg-emerald-500" : check.status === "warning" ? "bg-amber-500" : "bg-red-500"}`} /><span className="font-medium text-console-text">{check.label}</span></div><p className="mt-1 leading-5 text-console-muted">{check.detail}</p></li>)}</ul></section> : null}
                <div className="flex flex-wrap gap-2"><ConsoleButton disabled={busy || !sshUsername.trim() || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)} onClick={() => void checkPreflight()}>检查部署条件</ConsoleButton><ConsoleButton variant="primary" disabled={busy || !preflight?.ready} onClick={() => void deploy()}>{deploymentMode === "update" ? "确认更新 Worker" : "自动部署 Worker"}</ConsoleButton><ConsoleButton variant="ghost" disabled={busy} onClick={() => { setDeployingRef(null); setHostKey(null); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null); }}>取消</ConsoleButton></div>
              </div> : null}
              {message ? <p role="status" className="mt-3 text-sm text-console-muted">{message}</p> : null}
              <details className="mt-4 border-t border-console-line pt-4 text-xs text-console-muted">
                <summary className="cursor-pointer font-medium text-console-text">技术信息</summary>
                <dl className="mt-3 grid gap-x-5 gap-y-3 sm:grid-cols-2"><div><dt>节点标识</dt><dd className="mt-1 break-all font-mono">{selected.node_ref}</dd></div><div><dt>协议版本</dt><dd className="mt-1 text-console-text">{selected.protocol_version ?? "--"}</dd></div><div><dt>首次接入</dt><dd className="mt-1 text-console-text">{formatTime(selected.enrolled_at)}</dd></div><div><dt>最近在线</dt><dd className="mt-1 text-console-text">{formatTime(selected.last_seen_at)}</dd></div></dl>
              </details>
          </ConsoleCard>
        </div> : null}
      </div>
      <Dialog open={removalOpen} onOpenChange={(open) => { if (!busy) { setRemovalOpen(open); if (!open) { resetRemoval(null); setRemovalNodeRef(null); } } }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{removalPurpose === "node" ? `删除训练节点 ${removalNode?.name}` : `卸载 ${removalNode?.name} 的 Worker`}</DialogTitle>
            <DialogDescription>该节点已安装 Worker，需要通过一次临时 SSH 连接卸载服务。登录凭据仅用于本次操作，不会保存。</DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-900"><div className="flex items-start gap-2"><TriangleAlert className="mt-1 h-4 w-4 shrink-0" /><p>{removalPurpose === "node" ? "系统会先卸载 DataPilot Worker，再删除中心服务中的节点记录。SSH 账号、模型工程、数据集和训练产物不会被删除。" : "系统只卸载 DataPilot Worker 服务和自身文件，并保留节点记录。重新部署 Worker 前，该节点不能用于训练。"}</p></div></div>
          <label className="block text-sm text-console-muted">SSH 登录账号<input aria-label="删除操作 SSH 登录账号" className={inputClass} autoComplete="username" value={removalSshUsername} disabled={busy} onChange={(event) => setRemovalSshUsername(event.target.value)} /></label>
          <PasswordField id="training-node-removal-ssh-password" label="SSH 登录密码" value={removalSshPassword} visible={removalSshPasswordVisible} disabled={busy} autoComplete="current-password" onChange={setRemovalSshPassword} onToggle={() => setRemovalSshPasswordVisible((current) => !current)} />
          {removalSudoPasswordMode === "separate" ? <div className="space-y-2"><PasswordField id="training-node-removal-sudo-password" label="独立 sudo 密码" value={removalSudoPassword} visible={removalSudoPasswordVisible} disabled={busy} autoComplete="off" onChange={setRemovalSudoPassword} onToggle={() => setRemovalSudoPasswordVisible((current) => !current)} /><button type="button" className="text-xs font-medium text-console-cyan hover:underline" disabled={busy} onClick={() => { setRemovalSudoPasswordMode("same_as_ssh"); setRemovalSudoPassword(""); }}>改用 SSH 登录密码检查权限</button></div> : <button type="button" className="text-left text-xs font-medium text-console-cyan hover:underline" disabled={busy} onClick={() => setRemovalSudoPasswordMode("separate")}>sudo 密码与登录密码不同？</button>}
          <p className="text-xs leading-5 text-console-muted">系统会自动检查 root、免密 sudo 或登录密码可用的 sudo 权限，无需提前选择提权方式。</p>
          <DialogFooter><DialogClose asChild><ConsoleButton disabled={busy}>取消</ConsoleButton></DialogClose><ConsoleButton className="border-red-600 bg-red-600 text-white hover:border-red-700 hover:bg-red-700" disabled={busy || !removalSshUsername.trim() || !removalSshPassword || (removalSudoPasswordMode === "separate" && !removalSudoPassword)} onClick={() => setRemovalConfirmOpen(true)}>{removalPurpose === "node" ? "继续删除" : "继续卸载"}</ConsoleButton></DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog open={removalConfirmOpen} onOpenChange={(open) => !busy && setRemovalConfirmOpen(open)}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>{removalPurpose === "node" ? "再次确认删除训练节点" : "再次确认卸载 Worker"}</AlertDialogTitle><AlertDialogDescription>{removalPurpose === "node" ? "删除后，该节点不会再出现在平台中，也不能用于创建训练。模型工程、数据集、训练产物和 SSH 登录账号不会被删除。" : "卸载后，节点记录仍会保留，但在重新部署 Worker 前不能用于训练。"}</AlertDialogDescription></AlertDialogHeader>
          <AlertDialogFooter><AlertDialogCancel disabled={busy}>返回检查</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={busy} onClick={(event) => { event.preventDefault(); void completeRemoval(); }}>{removalPurpose === "node" ? "确认删除训练节点" : "确认卸载 Worker"}</AlertDialogAction></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
