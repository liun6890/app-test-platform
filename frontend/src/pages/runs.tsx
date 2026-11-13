import { useEffect, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BarChart3, BatteryMedium, Bug, Camera, Check, CheckCircle2,
  ChevronDown, ChevronRight, CircleStop, Clock3, CloudOff, Copy, Download, ExternalLink,
  FileArchive, FileJson, Gauge, HardDrive, Info, ListFilter, LoaderCircle, MonitorSmartphone,
  MoreHorizontal, Play, RefreshCw, RotateCcw, Search, Settings2, ShieldCheck, Smartphone,
  TerminalSquare, Trash2, Upload, Wifi, WifiOff, X, XCircle, Zap,
} from "lucide-react";
import { api } from "../api";
import { EmptyState, StatusBadge } from "../components/common";
import { formatDate, formatDuration, formatMinutes, localizeRuntimeText, messageOf } from "../lib/format";
import { phaseLabel, runStatusLabel } from "../lib/labels";
import { isActiveRun, runProgress, taskDevicesForRun } from "../lib/runs";
import { ReportStatus, copyToClipboard } from "./pages";
import type { Device, ReplayProgress, Report, Run, TestReport, View } from "../types";

type RunsPageProps = {
  runs: Run[];
  devices: Device[];
  busyAction: string | null;
  onStop: (run: Run) => void;
  onResume: (jobId: string, serial: string) => void;
  onEnd: (jobId: string, serial: string) => void;
  onConfirmInstall: (run: Run) => void;
  onNavigate: (view: View) => void;
};

export function RunFlow({ run }: { run: Run }) {
  const steps = [
    { label: "准备", note: "校验配置" },
    { label: "设备就绪", note: "安装与连接" },
    { label: "压力执行", note: "采集运行证据" },
    { label: "证据归档", note: "整理测试结果" },
  ];
  const terminal = ["completed", "stopped", "failed"].includes(run.status);
  const completed = run.status === "completed";
  const currentStep = run.status === "queued" || run.status === "preparing"
    ? 0
    : ["awaiting_install", "installing"].includes(run.status)
      ? 1
      : ["running", "recovering", "waiting_for_device", "resuming", "paused", "needs_manual_resume"].includes(run.status)
        ? 2
        : 3;

  return (
    <section className={`run-flow ${terminal ? "is-terminal" : ""} ${run.status === "failed" ? "is-failed" : ""}`} aria-label="任务流程">
      <div className="run-flow-heading"><span className="eyebrow">执行流程</span><span>{phaseLabel(run)}</span></div>
      <div className="run-flow-track">
        {steps.map((step, index) => {
          const isDone = completed || (!terminal && index < currentStep);
          const isCurrent = (!terminal && index === currentStep) || (terminal && index === 3);
          return (
            <div className={`run-flow-step ${isDone ? "is-done" : ""} ${isCurrent ? "is-current" : ""}`} key={step.label}>
              {index < steps.length - 1 && <span className="run-flow-connector" aria-hidden="true" />}
              <span className="run-flow-node">{isDone ? <Check size={14} /> : index + 1}</span>
              <span className="run-flow-copy"><strong>{step.label}</strong><small>{step.note}</small></span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function ActiveRunMonitor({ run, devices, busyAction, onStop, onResume, onEnd, onConfirmInstall }: Omit<RunsPageProps, "runs" | "onNavigate"> & { run: Run }) {
  const activeDevices = taskDevicesForRun(run, devices);
  const lines = (run.log || "").split(/\r?\n/).filter(Boolean).slice(-100);
  const installationDevices = run.installation?.devices ? Object.values(run.installation.devices) : [];
  const installedCount = installationDevices.filter((item) => item.installed).length;
  const pendingDevices = run.installation ? run.installation.pendingDevices : run.devices;
  const runTitle = run.apkName || run.packageName || run.id;

  return (
    <div className="active-run-monitor" data-run-id={run.id}>
      <section className="run-hero">
        <div>
          <span className="eyebrow"><span className="live-dot" />{phaseLabel(run)}</span>
          <h2>{runTitle}</h2>
          <p>智能压测 · {run.id} · {run.devices.length} 台设备 · {run.cycles} 轮</p>
          {!run.evidence?.pressureStarted && run.status !== "stopping" && run.status !== "awaiting_install" && (
            <p className="run-warning"><AlertTriangle size={15} />当前尚未检测到设备侧运行证据，未将此任务计为压测中。</p>
          )}
        </div>
        <div className="run-clock"><small>已运行</small><strong>{run.elapsed}</strong></div>
        <button className="danger-button" type="button" onClick={() => onStop(run)} disabled={busyAction === `stop-${run.id}`}>
          {busyAction === `stop-${run.id}` ? <LoaderCircle size={17} className="spin" /> : <CircleStop size={17} />}停止全部
        </button>
      </section>

      <RunFlow run={run} />

      {run.status === "awaiting_install" && (
        <section className="install-gate panel">
          <span className="install-gate-icon"><Smartphone size={24} /></span>
          <div className="install-gate-copy">
            <span className="eyebrow">需要人工操作</span>
            <h2>请在设备上完成安装包安装</h2>
            <p>系统安装页已经拉起。请逐台点击“安装”或“继续安装”，完成后回到这里确认。平台会校验包是否真实存在，通过后才会清除应用数据并继续场景流程。</p>
            <p className="install-package-note">当前核验包名：<code>{run.packageName}</code></p>
            <p className="install-progress-note">已检测到 {installedCount} / {run.devices.length} 台设备完成安装，仍需处理 {pendingDevices.length} 台</p>
            <div className="install-device-chips">{pendingDevices.map((serial) => <span key={serial}><Smartphone size={13} />{serial}</span>)}</div>
          </div>
          <div className="install-gate-actions">
            <button className="primary-button" type="button" onClick={() => onConfirmInstall(run)} disabled={busyAction === `confirm-install-${run.id}`}>
              {busyAction === `confirm-install-${run.id}` ? <LoaderCircle size={17} className="spin" /> : <CheckCircle2 size={17} />}
              {busyAction === `confirm-install-${run.id}` ? "正在核验" : "已完成安装，继续"}
            </button>
            <small>未安装完成时不会放行</small>
          </div>
        </section>
      )}

      <div className="run-device-grid">
        {activeDevices.length ? activeDevices.map((device) => (
          <section className="panel run-device-card" key={device.serial}>
            <div className="run-card-top">
              <div className="device-identity"><span className="device-avatar"><Smartphone size={20} /></span><div><strong>{device.alias}</strong><small>{device.serial}</small></div></div>
              <div className="run-card-actions">
                <StatusBadge status={device.status} />
                {device.canResume && device.jobId ? <button className="icon-button small resume-button" type="button" onClick={() => onResume(device.jobId as string, device.serial)} disabled={busyAction === `resume-${device.serial}`} title="继续运行设备任务" aria-label={`继续运行 ${device.alias}`}>{busyAction === `resume-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <Play size={16} fill="currentColor" />}</button> : null}
                {device.canEnd && device.jobId ? <button className="icon-button small manual-end-button" type="button" onClick={() => onEnd(device.jobId as string, device.serial)} disabled={busyAction === `end-${device.serial}`} title="手动结束本设备测试（结束后不可继续）" aria-label={`手动结束 ${device.alias}`}>{busyAction === `end-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <XCircle size={17} />}</button> : null}
              </div>
            </div>
            <div className="run-progress-number"><strong>{device.progress}</strong><span>%</span></div>
            <div className="progress-track large"><span style={{ width: `${device.progress}%` }} /></div>
            <div className="run-stats"><div><small>当前轮次</small><strong>{device.cycle || 1} / {run.cycles}</strong></div><div><small>剩余时间</small><strong>{device.remainingSeconds != null ? `${Math.ceil(device.remainingSeconds)} 秒` : device.elapsed}</strong></div><div><small>恢复次数</small><strong>{device.resumeAttempts || 0}</strong></div></div>
            {device.operations != null ? <div className="run-device-operations">操作 {device.operations.toLocaleString()} 次{device.operationStalled || device.fastbotAlive === false ? " · 操作心跳已停止，正在恢复" : ""}</div> : null}
            {device.lastError ? <div className="run-device-error" title={localizeRuntimeText(device.lastError)}><AlertTriangle size={15} /><span>{localizeRuntimeText(device.lastError)}</span></div> : null}
            {device.waitingForDevice || device.status === "waiting_for_device" ? <div className="run-device-waiting"><CloudOff size={15} />等待设备重新连接</div> : null}
          </section>
        )) : <div className="inline-empty run-devices-empty"><LoaderCircle className="spin" size={19} />正在等待设备任务状态</div>}
      </div>

      <section className="panel log-panel">
        <div className="panel-heading"><div><span className="eyebrow">实时输出</span><h2>任务运行日志</h2><small className="run-log-id">{run.id}</small></div><div className="log-filter"><Wifi size={15} />自动刷新</div></div>
        {run.evidence?.errors?.length ? <div className="run-error-summary"><AlertTriangle size={17} /><div><strong>{run.evidence.pressureStarted ? "运行异常" : "安装包安装失败或准备失败，未进入压测阶段"}</strong>{run.evidence.errors.map((item, index) => <small key={`${index}-${item}`}>{localizeRuntimeText(item)}</small>)}</div></div> : null}
        <div className="terminal-log">{lines.length ? lines.map((line, index) => <div className="raw-log-line" key={`${index}-${line}`}><time>{String(index + 1).padStart(3, "0")}</time><span className={/error|failed|exception/i.test(line) ? "warning" : "success"}>{/error|failed|exception/i.test(line) ? "错误" : "日志"}</span><p>{localizeRuntimeText(line)}</p></div>) : <div className="terminal-wait"><LoaderCircle size={16} className="spin" />等待任务输出</div>}</div>
      </section>
    </div>
  );
}

export function RunsPageAllTasks({ runs, devices, busyAction, onStop, onResume, onEnd, onConfirmInstall, onNavigate }: RunsPageProps) {
  const activeRuns = runs.filter(isActiveRun);
  if (!activeRuns.length && !runs.length) {
    return <section className="panel page-panel"><EmptyState icon={Activity} title="还没有测试任务" note="选择设备并配置安装包后启动压测" action={<button className="primary-button" type="button" onClick={() => onNavigate("config")}><Play size={16} />新建测试</button>} /></section>;
  }

  return (
    <>
      {activeRuns.length ? <div className="active-run-list">{activeRuns.map((run) => <ActiveRunMonitor key={run.id} run={run} devices={devices} busyAction={busyAction} onStop={onStop} onResume={onResume} onEnd={onEnd} onConfirmInstall={onConfirmInstall} />)}</div> : <section className="panel page-panel"><EmptyState icon={CheckCircle2} title="当前没有运行中的任务" note="最近任务已结束，可在下方查看状态" /></section>}
      <section className="panel recent-panel"><div className="panel-heading"><div><span className="eyebrow">本次服务会话</span><h2>任务记录</h2></div></div><div className="run-history">{runs.map((run) => <div key={run.id}><span className={`run-history-icon ${run.status}`}>{run.status === "completed" ? <CheckCircle2 size={18} /> : run.status === "failed" ? <XCircle size={18} /> : isActiveRun(run) ? <LoaderCircle size={18} className="spin" /> : <CircleStop size={18} />}</span><div><strong>{run.apkName || run.packageName}</strong><small>{run.id} · {formatDate(run.startedAt)}</small></div><span>{run.devices.length} 台 · {run.cycles} 轮</span><b>{runStatusLabel(run.status)}</b></div>)}</div></section>
    </>
  );
}

export function RunsPage({ runs, devices, busyAction, onStop, onResume, onEnd, onConfirmInstall, onNavigate }: RunsPageProps) {
  const activeRuns = runs.filter(isActiveRun);
  const activeRunIds = activeRuns.map((run) => run.id).join("|");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  // Keep the selected task stable during polling and choose the newest task
  // when the previously selected task reaches a terminal state.
  useEffect(() => {
    setSelectedRunId((current) => activeRuns.some((run) => run.id === current) ? current : activeRuns[0]?.id || null);
  }, [activeRunIds]);

  const selectedRun = activeRuns.find((run) => run.id === selectedRunId) || activeRuns[0] || null;
  if (!activeRuns.length && !runs.length) {
    return <section className="panel page-panel"><EmptyState icon={Activity} title="还没有测试任务" note="选择设备并配置安装包后启动压测" action={<button className="primary-button" type="button" onClick={() => onNavigate("config")}><Play size={16} />新建测试</button>} /></section>;
  }

  return (
    <>
      {activeRuns.length ? <section className="run-task-tabs" aria-label="运行中的任务">
        <div className="run-task-tabs-heading"><div><span className="eyebrow"><span className="live-dot" />运行中的任务</span><h2>任务监控</h2></div><span className="run-task-count">{activeRuns.length} 个活动任务</span></div>
        <div className="run-task-tab-list" role="tablist" aria-label="选择要查看的任务">
          {activeRuns.map((run) => {
            const progress = runProgress(run, devices);
            const selected = run.id === selectedRun?.id;
            return <button className={`run-task-tab ${selected ? "is-selected" : ""}`} type="button" role="tab" tabIndex={selected ? 0 : -1} aria-selected={selected} aria-controls={`run-panel-${run.id}`} ref={(element) => { tabRefs.current[run.id] = element; }} onClick={() => setSelectedRunId(run.id)} onKeyDown={(event) => {
              const index = activeRuns.findIndex((item) => item.id === run.id);
              let nextIndex = -1;
              if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % activeRuns.length;
              if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + activeRuns.length) % activeRuns.length;
              if (event.key === "Home") nextIndex = 0;
              if (event.key === "End") nextIndex = activeRuns.length - 1;
              if (nextIndex >= 0 && activeRuns[nextIndex]) {
                event.preventDefault();
                const nextId = activeRuns[nextIndex].id;
                setSelectedRunId(nextId);
                window.requestAnimationFrame(() => tabRefs.current[nextId]?.focus());
              }
            }} key={run.id}>
              <span className="run-task-tab-top"><span className={`run-task-state ${run.status}`}><span />{runStatusLabel(run.status)}</span><b>{progress}%</b></span>
              <strong title={run.apkName || run.packageName || run.id}>{run.apkName || run.packageName || run.id}</strong>
              <small>{run.id} · {run.devices.length} 台设备</small>
              <span className="run-task-tab-progress"><i style={{ width: `${progress}%` }} /></span>
            </button>;
          })}
        </div>
      </section> : null}
      {selectedRun ? <div className="active-run-list"><div key={selectedRun.id} id={`run-panel-${selectedRun.id}`} role="tabpanel" aria-label={`${selectedRun.apkName || selectedRun.packageName || selectedRun.id}`}><ActiveRunMonitor run={selectedRun} devices={devices} busyAction={busyAction} onStop={onStop} onResume={onResume} onEnd={onEnd} onConfirmInstall={onConfirmInstall} /></div></div> : <section className="panel page-panel"><EmptyState icon={CheckCircle2} title="当前没有运行中的任务" note="最近任务已结束，可在下方查看状态" /></section>}
      <section className="panel recent-panel"><div className="panel-heading"><div><span className="eyebrow">本次服务会话</span><h2>任务记录</h2></div></div><div className="run-history">{runs.map((run) => <div key={run.id}><span className={`run-history-icon ${run.status}`}>{run.status === "completed" ? <CheckCircle2 size={18} /> : run.status === "failed" ? <XCircle size={18} /> : isActiveRun(run) ? <LoaderCircle size={18} className="spin" /> : <CircleStop size={18} />}</span><div><strong>{run.apkName || run.packageName}</strong><small>{run.id} · {formatDate(run.startedAt)}</small></div><span>{run.devices.length} 台 · {run.cycles} 轮</span><b>{runStatusLabel(run.status)}</b></div>)}</div></section>
    </>
  );
}
