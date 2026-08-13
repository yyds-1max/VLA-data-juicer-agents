import { Activity, Cpu, Eye, EyeOff, HardDrive, KeyRound, Plus, Server, ShieldCheck, Trash2, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";

import { ApiResponseError, createTrainingNode, deployTrainingNodeWorker, discoverTrainingNodeHostKey, preflightTrainingNodeWorker, removeTrainingNodeWorker } from "../../api/client";
import type { TrainingNode, TrainingNodeHostKey, TrainingNodePreflightResult, TrainingNodeResourceSnapshot, TrainingNodeStatus } from "../../api/types";
import { ConsoleButton } from "../../components/console/ConsoleButton";
import { ConsoleCard } from "../../components/console/ConsoleCard";
import { ProgressBar } from "../../components/console/ProgressBar";
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
  not_started: "尚未部署",
  deploying: "正在部署",
  succeeded: "部署成功",
  failed: "部署失败",
} as const;

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

function formatBytes(value: number | undefined) {
  if (value == null || !Number.isFinite(value)) return "--";
  const gib = value / 1024 / 1024 / 1024;
  return `${gib >= 10 ? gib.toFixed(0) : gib.toFixed(1)} GiB`;
}

function formatTime(value: string | null | undefined) {
  if (!value) return "--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function availablePercent(available: number, total: number) {
  if (!Number.isFinite(available) || !Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, Math.max(0, available / total * 100));
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
  resourcesByNode,
  canManage,
  deploymentEnabled,
  deploymentDisabledReason,
  onChanged,
}: {
  nodes: TrainingNode[];
  resourcesByNode: Record<string, TrainingNodeResourceSnapshot>;
  canManage: boolean;
  deploymentEnabled: boolean;
  deploymentDisabledReason?: string | null;
  onChanged: (node: TrainingNode) => void;
}) {
  const [selectedRef, setSelectedRef] = useState<string>(nodes[0]?.node_ref ?? "");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [address, setAddress] = useState("");
  const [sshPort, setSshPort] = useState("22");
  const [sshUsername, setSshUsername] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [deployingRef, setDeployingRef] = useState<string | null>(null);
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
  const [removalSshPassword, setRemovalSshPassword] = useState("");
  const [removalSshPasswordVisible, setRemovalSshPasswordVisible] = useState(false);
  const [removalSudoPasswordMode, setRemovalSudoPasswordMode] = useState<"same_as_ssh" | "separate" | "not_required">("same_as_ssh");
  const [removalSudoPassword, setRemovalSudoPassword] = useState("");
  const [removalSudoPasswordVisible, setRemovalSudoPasswordVisible] = useState(false);
  const selected = nodes.find((node) => node.node_ref === selectedRef) ?? nodes[0] ?? null;
  const snapshot = selected ? resourcesByNode[selected.node_ref] : undefined;
  const resources = snapshot?.resources ?? selected?.resources ?? null;
  const memoryAvailablePercent = resources ? availablePercent(resources.memory.available_bytes, resources.memory.total_bytes) : 0;
  const port = Number(sshPort);
  const valid = Boolean(name.trim() && address.trim() && sshUsername.trim() && Number.isInteger(port) && port >= 1 && port <= 65535);
  const gpuSummary = useMemo(() => {
    const gpus = resources?.gpus ?? [];
    return {
      count: gpus.length,
      memory: gpus.reduce((sum, gpu) => sum + gpu.memory_total_bytes, 0),
      utilization: gpus.length ? gpus.reduce((sum, gpu) => sum + gpu.utilization_percent, 0) / gpus.length : 0,
    };
  }, [resources]);
  const hasInstalledWorker = Boolean(selected && (
    selected.deployment_status === "succeeded"
    || selected.installed_worker_version
    || selected.worker_version
    || selected.enrolled_at
  ));

  const create = async () => {
    if (!valid) return;
    setBusy(true); setMessage(null); setDeployingRef(null); setHostKey(null);
    try {
      const node = await createTrainingNode({ name: name.trim(), description: description.trim() || undefined, address: address.trim(), ssh_port: port, ssh_username: sshUsername.trim() });
      onChanged(node); setSelectedRef(node.node_ref); setName(""); setDescription(""); setAddress(""); setSshPort("22"); setSshUsername("");
      setMessage("节点已登记。点击“部署 Worker”，系统会自动完成账号、服务和注册配置。");
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const openDeployment = async () => {
    if (!selected) return;
    setBusy(true); setMessage(null); setDeployingRef(selected.node_ref); setHostKey(null); setHostKeyConfirmed(false); setSshPassword(""); setSshPasswordVisible(false); setSudoPasswordMode("same_as_ssh"); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null);
    try {
      setHostKey(await discoverTrainingNodeHostKey(selected.node_ref));
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const checkPreflight = async () => {
    if (!selected || !hostKey || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)) return;
    setBusy(true); setMessage(null); setPreflight(null);
    try {
      setPreflight(await preflightTrainingNodeWorker(selected.node_ref, {
        expected_revision: selected.state_revision,
        confirmed_host_key: hostKey,
        host_key_confirmed: true,
        ssh_password: sshPassword,
        sudo_password_mode: sudoPasswordMode,
        ...(sudoPasswordMode === "separate" ? { sudo_password: sudoPassword } : {}),
      }));
    } catch (error) { setMessage(errorText(error)); } finally { setBusy(false); }
  };

  const deploy = async () => {
    if (!selected || !hostKey || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)) return;
    setBusy(true); setMessage(null);
    try {
      const result = await deployTrainingNodeWorker(selected.node_ref, {
        expected_revision: selected.state_revision,
        confirmed_host_key: hostKey,
        host_key_confirmed: true,
        ssh_password: sshPassword,
        sudo_password_mode: sudoPasswordMode,
        ...(sudoPasswordMode === "separate" ? { sudo_password: sudoPassword } : {}),
      });
      onChanged(result.node); setDeployingRef(null); setHostKey(null); setHostKeyConfirmed(false); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null);
      setMessage("Worker 已自动部署并完成注册，正在等待稳定心跳。");
    } catch (error) { setMessage(errorText(error)); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); } finally { setBusy(false); }
  };

  const resetRemoval = () => {
    setRemovalSshPassword("");
    setRemovalSshPasswordVisible(false);
    setRemovalSudoPasswordMode("same_as_ssh");
    setRemovalSudoPassword("");
    setRemovalSudoPasswordVisible(false);
  };

  const openRemoval = () => {
    setMessage(null);
    resetRemoval();
    setRemovalConfirmOpen(false);
    setRemovalOpen(true);
  };

  const removeWorker = async () => {
    if (!selected || !removalSshPassword || (removalSudoPasswordMode === "separate" && !removalSudoPassword)) return;
    setBusy(true); setMessage(null);
    try {
      const result = await removeTrainingNodeWorker(selected.node_ref, {
        expected_revision: selected.state_revision,
        ssh_password: removalSshPassword,
        sudo_password_mode: removalSudoPasswordMode,
        ...(removalSudoPasswordMode === "separate" ? { sudo_password: removalSudoPassword } : {}),
      });
      onChanged(result.node);
      setRemovalConfirmOpen(false);
      setRemovalOpen(false);
      resetRemoval();
      setMessage("Worker 已删除。该节点已恢复为待部署状态，在重新部署 Worker 前不可用于训练。");
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
        <p className="mt-1 text-sm text-console-muted">登记训练机器、部署独立 Worker，并通过心跳安全查看资源与运行状态。</p>
      </header>

      <div className="grid gap-4 xl:grid-cols-[minmax(340px,.72fr)_minmax(0,1.28fr)]">
        <ConsoleCard className="h-fit shadow-none">
          <div className="flex items-center gap-2"><Plus className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">登记训练节点</h3></div>
          <p className="mt-1 text-xs leading-5 text-console-muted">这里只保存地址、端口和用户名。SSH 密码不会写入节点记录。</p>
          <div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
            <label className="text-sm text-console-muted">节点名称<input className={inputClass} value={name} disabled={!canManage || busy} onChange={(event) => setName(event.target.value)} /></label>
            <label className="text-sm text-console-muted">SSH 用户名<input className={inputClass} autoComplete="username" value={sshUsername} disabled={!canManage || busy} onChange={(event) => setSshUsername(event.target.value)} /></label>
            <label className="text-sm text-console-muted">主机地址<input className={inputClass} placeholder="例如 10.0.0.12" value={address} disabled={!canManage || busy} onChange={(event) => setAddress(event.target.value)} /></label>
            <label className="text-sm text-console-muted">SSH 端口<input className={inputClass} inputMode="numeric" value={sshPort} disabled={!canManage || busy} onChange={(event) => setSshPort(event.target.value)} /></label>
          </div>
          <label className="mt-3 block text-sm text-console-muted">说明（可选）<textarea className="mt-1 min-h-16 w-full rounded-md border border-console-line bg-console-panel p-2 text-sm text-console-text" value={description} disabled={!canManage || busy} onChange={(event) => setDescription(event.target.value)} /></label>
          <ConsoleButton className="mt-4" variant="primary" disabled={!canManage || busy || !valid} onClick={() => void create()}><Plus className="h-4 w-4" />登记节点</ConsoleButton>
          {!canManage ? <p className="mt-3 text-xs text-console-muted">当前身份仅可查看节点，不能登记或部署 Worker。</p> : null}
        </ConsoleCard>

        <div className="space-y-4">
          <ConsoleCard className="shadow-none">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><h3 className="font-semibold text-console-text">已登记节点</h3><p className="mt-1 text-xs text-console-muted">在线状态由中心服务根据最近心跳计算。</p></div>
              <span className="text-xs text-console-muted">共 {nodes.length} 台</span>
            </div>
            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              {nodes.map((node) => <button key={node.node_ref} type="button" onClick={() => { setSelectedRef(node.node_ref); setDeployingRef(null); setHostKey(null); setPreflight(null); setMessage(null); }} className={`rounded-lg border p-3 text-left ${selected?.node_ref === node.node_ref ? "border-console-cyan bg-sky-50" : "border-console-line bg-console-panel2 hover:border-console-cyan/40"}`}>
                <span className="flex items-center justify-between gap-2"><span className="font-medium text-console-text">{node.name}</span><StatusTag tone={statusMeta[node.status].tone}>{statusMeta[node.status].label}</StatusTag></span>
                <span className="mt-2 block truncate font-mono text-xs text-console-muted">{node.address && node.ssh_username ? `${node.ssh_username}@${node.address}:${node.ssh_port ?? 22}` : "连接信息仅管理员可见"}</span>
                <span className="mt-1 block text-xs text-console-muted">最近心跳 {formatTime(node.last_heartbeat_at)}</span>
              </button>)}
            </div>
            {!nodes.length ? <div className="py-10 text-center"><Server className="mx-auto h-8 w-8 text-console-muted" /><p className="mt-3 text-sm text-console-muted">尚未登记训练节点。</p></div> : null}
          </ConsoleCard>

          {selected ? <>
            <ConsoleCard className="shadow-none">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><div className="flex items-center gap-2"><ShieldCheck className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">{selected.name}</h3></div><p className="mt-1 font-mono text-xs text-console-muted">{selected.node_ref}</p></div>
                <StatusTag tone={statusMeta[selected.status].tone}>{statusMeta[selected.status].label}</StatusTag>
              </div>
              <dl className="mt-4 grid gap-x-6 gap-y-3 border-y border-console-line py-4 text-sm sm:grid-cols-2 xl:grid-cols-5">
                <div><dt className="text-xs text-console-muted">部署状态</dt><dd className="mt-1 text-console-text">{deploymentStatusLabel[selected.deployment_status ?? "not_started"]}</dd></div>
                <div><dt className="text-xs text-console-muted">Worker 版本</dt><dd className="mt-1 text-console-text">{selected.worker_version ?? selected.installed_worker_version ?? "尚未部署"}</dd></div>
                <div><dt className="text-xs text-console-muted">协议版本</dt><dd className="mt-1 text-console-text">{selected.protocol_version ?? "--"}</dd></div>
                <div><dt className="text-xs text-console-muted">首次接入</dt><dd className="mt-1 text-console-text">{formatTime(selected.enrolled_at)}</dd></div>
                <div><dt className="text-xs text-console-muted">最近在线</dt><dd className="mt-1 text-console-text">{formatTime(selected.last_seen_at)}</dd></div>
              </dl>
              {selected.deployment_status === "failed" && selected.deployment_message ? <p className="mt-3 text-sm text-red-700">{selected.deployment_message}</p> : null}
              {selected.health_message ? <p className="mt-3 text-sm text-amber-700">{selected.health_message}</p> : null}
              <div className="mt-4 flex flex-wrap items-center gap-3"><ConsoleButton disabled={!canManage || !deploymentEnabled || busy || selected.status === "disabled" || selected.deployment_status === "deploying"} onClick={() => void openDeployment()}><KeyRound className="h-4 w-4" />{hasInstalledWorker ? "修复 Worker" : "部署 Worker"}</ConsoleButton>{hasInstalledWorker ? <ConsoleButton className="border-red-200 text-red-700 hover:border-red-300 hover:bg-red-50" disabled={!canManage || !deploymentEnabled || busy || selected.deployment_status === "deploying"} onClick={openRemoval}><Trash2 className="h-4 w-4" />删除 Worker</ConsoleButton> : null}<span className="text-xs text-console-muted">系统自动创建低权限运行账号、安装系统服务并完成注册，不需要手工操作服务器。</span></div>
              {!deploymentEnabled ? <p className="mt-2 text-xs text-amber-700">{deploymentDisabledReason || "系统尚未配置可供训练节点访问的中心 HTTPS 地址，暂不能部署 Worker。"}</p> : null}
              {deployingRef === selected.node_ref && hostKey ? <div className="mt-4 space-y-3 rounded-md border border-sky-200 bg-sky-50 p-4"><div><p className="text-sm font-medium text-sky-950">确认服务器身份</p><p className="mt-1 text-xs text-sky-800">请核对管理员提供的主机指纹，确认后先检查部署条件，再执行安装。</p><code className="mt-2 block break-all rounded bg-white p-2 text-xs text-sky-950">{hostKey.sha256_fingerprint}</code></div><label className="flex items-start gap-2 text-sm text-sky-950"><input className="mt-0.5" type="checkbox" checked={hostKeyConfirmed} disabled={busy} onChange={(event) => { setHostKeyConfirmed(event.target.checked); setPreflight(null); }} /><span>我已确认该主机指纹正确</span></label><PasswordField id="training-node-ssh-password" label="SSH 部署密码" value={sshPassword} visible={sshPasswordVisible} disabled={busy} autoComplete="current-password" onChange={(value) => { setSshPassword(value); setPreflight(null); }} onToggle={() => setSshPasswordVisible((current) => !current)} /><label className="block text-sm text-console-muted">提权方式<select aria-label="Worker 部署提权方式" className={inputClass} value={sudoPasswordMode} disabled={busy} onChange={(event) => { setSudoPasswordMode(event.target.value as typeof sudoPasswordMode); setPreflight(null); }}><option value="same_as_ssh">SSH 密码同时用于 sudo</option><option value="not_required">root 或免密 sudo</option><option value="separate">使用不同的 sudo 密码</option></select></label>{sudoPasswordMode === "separate" ? <PasswordField id="training-node-sudo-password" label="sudo 部署密码" value={sudoPassword} visible={sudoPasswordVisible} disabled={busy} autoComplete="off" onChange={(value) => { setSudoPassword(value); setPreflight(null); }} onToggle={() => setSudoPasswordVisible((current) => !current)} /> : null}<p className="text-xs leading-5 text-sky-800">检查过程只读取系统、磁盘、GPU 工具和 root/sudo 能力，不修改训练节点。密码不保存。</p>{preflight ? <section aria-label="Worker 部署条件检查" className={`rounded-md border p-3 ${preflight.ready ? "border-emerald-200 bg-emerald-50" : "border-red-200 bg-red-50"}`}><div className="flex items-center justify-between gap-2"><p className="text-sm font-medium text-console-text">{preflight.ready ? "部署条件已满足" : "存在阻止部署的问题"}</p><span className="text-xs text-console-muted">{formatTime(preflight.checked_at)}</span></div><ul className="mt-2 grid gap-2 sm:grid-cols-2">{preflight.checks.map((check) => <li key={check.code} className="rounded bg-white/80 p-2 text-xs"><div className="flex items-center gap-2"><span aria-hidden="true" className={`h-2 w-2 rounded-full ${check.status === "passed" ? "bg-emerald-500" : check.status === "warning" ? "bg-amber-500" : "bg-red-500"}`} /><span className="font-medium text-console-text">{check.label}</span></div><p className="mt-1 leading-5 text-console-muted">{check.detail}</p></li>)}</ul></section> : null}<div className="flex flex-wrap gap-2"><ConsoleButton disabled={busy || !hostKeyConfirmed || !sshPassword || (sudoPasswordMode === "separate" && !sudoPassword)} onClick={() => void checkPreflight()}>检查部署条件</ConsoleButton><ConsoleButton variant="primary" disabled={busy || !preflight?.ready} onClick={() => void deploy()}>自动部署 Worker</ConsoleButton><ConsoleButton variant="ghost" disabled={busy} onClick={() => { setDeployingRef(null); setHostKey(null); setSshPassword(""); setSshPasswordVisible(false); setSudoPassword(""); setSudoPasswordVisible(false); setPreflight(null); }}>取消</ConsoleButton></div></div> : null}
              {message ? <p role="status" className="mt-3 text-sm text-console-muted">{message}</p> : null}
            </ConsoleCard>

            <ConsoleCard className="shadow-none">
              <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2"><Activity className="h-5 w-5 text-console-cyan" /><h3 className="font-semibold text-console-text">节点资源</h3></div><span className="text-xs text-console-muted">{snapshot?.stale ? "快照已过期" : `采样 ${formatTime(snapshot?.captured_at)}`}</span></div>
              {resources ? <><div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
                <div className="rounded-md bg-console-panel2 p-3"><Cpu className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">CPU</p><p className="mt-1 font-semibold text-console-text">{resources.cpu.logical_cores} 核 · load {resources.cpu.load_1m?.toFixed(2) ?? "--"}</p></div>
                <div className="rounded-md bg-console-panel2 p-3"><div className="flex items-start justify-between gap-3"><div><Activity className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">内存可用 / 总容量</p></div><p className="text-2xl font-semibold leading-none text-console-text" aria-label={`内存可用百分比 ${memoryAvailablePercent.toFixed(0)}%`}>{memoryAvailablePercent.toFixed(0)}<span className="text-sm">%</span></p></div><p className="mt-1 font-semibold text-console-text">{formatBytes(resources.memory.available_bytes)} / {formatBytes(resources.memory.total_bytes)}</p><ProgressBar className="mt-2" value={memoryAvailablePercent} tone="success" label="可用内存" /></div>
                <div className="rounded-md bg-console-panel2 p-3"><Server className="h-4 w-4 text-console-muted" /><p className="mt-2 text-xs text-console-muted">GPU</p><p className="mt-1 font-semibold text-console-text">{gpuSummary.count} 张 · {formatBytes(gpuSummary.memory)}</p></div>
              </div><section className="mt-4 rounded-lg border border-console-line bg-console-panel p-4" aria-labelledby="node-disks-title"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><HardDrive className="h-4 w-4 text-console-cyan" /><h4 id="node-disks-title" className="font-medium text-console-text">磁盘空间</h4></div><span className="text-xs text-console-muted">自动发现 {resources.disks.length} 个存储挂载点</span></div>{resources.disks.length ? <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{resources.disks.map((disk) => { const percent = availablePercent(disk.available_bytes, disk.total_bytes); return <article key={disk.mount} className="rounded-md bg-console-panel2 p-3"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="truncate font-mono text-sm font-medium text-console-text" title={disk.mount}>{disk.mount}</p><p className="mt-1 text-xs text-console-muted">可用 / 总容量</p></div><p className="shrink-0 text-2xl font-semibold leading-none text-console-text" aria-label={`${disk.mount} 可用 ${percent.toFixed(0)}%`}>{percent.toFixed(0)}<span className="text-sm">%</span></p></div><p className="mt-2 font-semibold text-console-text">{formatBytes(disk.available_bytes)} / {formatBytes(disk.total_bytes)}</p><ProgressBar className="mt-2" value={percent} tone={percent < 10 ? "danger" : percent < 20 ? "warning" : "success"} label="可用空间" /></article>; })}</div> : <p className="mt-3 text-sm text-console-muted">Worker 尚未上报可用存储挂载点。</p>}</section><div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">{resources.gpus.map((gpu) => <article key={gpu.uuid} className="rounded-md border border-console-line p-3"><div className="flex justify-between gap-2"><p className="font-medium text-console-text">GPU {gpu.index}</p><span className="text-xs text-console-muted">{gpu.temperature_celsius == null ? "--" : `${gpu.temperature_celsius}°C`}</span></div><p className="mt-1 truncate text-xs text-console-muted">{gpu.name}</p><ProgressBar className="mt-3" value={gpu.utilization_percent} tone="purple" label={`利用率 ${gpu.utilization_percent}%`} /><ProgressBar className="mt-3" value={gpu.memory_total_bytes ? gpu.memory_used_bytes / gpu.memory_total_bytes * 100 : 0} tone="info" label={`显存 ${formatBytes(gpu.memory_used_bytes)} / ${formatBytes(gpu.memory_total_bytes)}`} /></article>)}</div></> : <div className="py-10 text-center text-sm text-console-muted">Worker 上报心跳后显示 CPU、内存、磁盘和 GPU 资源。</div>}
            </ConsoleCard>
          </> : null}
        </div>
      </div>
      <Dialog open={removalOpen} onOpenChange={(open) => { if (!busy) { setRemovalOpen(open); if (!open) resetRemoval(); } }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>删除 {selected?.name} 的 Worker</DialogTitle>
            <DialogDescription>需要再次通过临时 SSH 连接卸载系统服务。密码仅用于本次请求，不会保存。</DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm leading-6 text-amber-900"><div className="flex items-start gap-2"><TriangleAlert className="mt-1 h-4 w-4 shrink-0" /><p>删除后，该节点会变为“待部署 Worker”，在重新部署前不可用于训练。操作只删除 DataPilot Worker 服务、专用账号和自身文件，不删除模型工程、数据集或训练产物。</p></div></div>
          <PasswordField id="training-node-removal-ssh-password" label="SSH 部署密码" value={removalSshPassword} visible={removalSshPasswordVisible} disabled={busy} autoComplete="current-password" onChange={setRemovalSshPassword} onToggle={() => setRemovalSshPasswordVisible((current) => !current)} />
          <label className="block text-sm text-console-muted">提权方式<select aria-label="Worker 删除提权方式" className={inputClass} value={removalSudoPasswordMode} disabled={busy} onChange={(event) => setRemovalSudoPasswordMode(event.target.value as typeof removalSudoPasswordMode)}><option value="same_as_ssh">SSH 密码同时用于 sudo</option><option value="not_required">root 或免密 sudo</option><option value="separate">使用不同的 sudo 密码</option></select></label>
          {removalSudoPasswordMode === "separate" ? <PasswordField id="training-node-removal-sudo-password" label="sudo 部署密码" value={removalSudoPassword} visible={removalSudoPasswordVisible} disabled={busy} autoComplete="off" onChange={setRemovalSudoPassword} onToggle={() => setRemovalSudoPasswordVisible((current) => !current)} /> : null}
          <DialogFooter><DialogClose asChild><ConsoleButton disabled={busy}>取消</ConsoleButton></DialogClose><ConsoleButton className="border-red-600 bg-red-600 text-white hover:border-red-700 hover:bg-red-700" disabled={busy || !removalSshPassword || (removalSudoPasswordMode === "separate" && !removalSudoPassword)} onClick={() => setRemovalConfirmOpen(true)}>确认删除</ConsoleButton></DialogFooter>
        </DialogContent>
      </Dialog>
      <AlertDialog open={removalConfirmOpen} onOpenChange={(open) => !busy && setRemovalConfirmOpen(open)}>
        <AlertDialogContent>
          <AlertDialogHeader><AlertDialogTitle>再次确认删除 Worker</AlertDialogTitle><AlertDialogDescription>删除后节点将立即撤销 Worker 凭据并退出可训练状态。以后仍可在此节点上重新部署 Worker。</AlertDialogDescription></AlertDialogHeader>
          <AlertDialogFooter><AlertDialogCancel disabled={busy}>返回检查</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={busy} onClick={(event) => { event.preventDefault(); void removeWorker(); }}>再次确认并删除</AlertDialogAction></AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
