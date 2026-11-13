import { useCallback, useEffect, useState } from "react";
import {
  Activity, AlertTriangle, BarChart3, BatteryMedium, Bug, Camera, Check, CheckCircle2,
  ChevronDown, ChevronRight, CircleStop, Clock3, CloudOff, Copy, Download, ExternalLink,
  FileArchive, FileJson, Gauge, HardDrive, Info, ListFilter, LoaderCircle, MonitorSmartphone,
  MoreHorizontal, Play, RefreshCw, RotateCcw, Search, Settings2, ShieldCheck, Smartphone,
  TerminalSquare, Trash2, Upload, Wifi, WifiOff, X, XCircle, Zap,
} from "lucide-react";
import { api } from "../api";
import { EmptyState, StatusBadge } from "../components/common";
import { formatDate, formatDuration, localizeRuntimeText, messageOf } from "../lib/format";
import { cycleStatusLabel, deviceDiagnosis, evidenceExcerpt, failedCycleStatuses, issueDisplay,
  runningDeviceCount, stopReasonLabel, testDiagnosis } from "../lib/diagnosis";
import { phaseLabel, reportStatusLabel, runStatusLabel } from "../lib/labels";
import { runProgress, taskDevicesForRun } from "../lib/runs";
import { ReportStatus, artifactLabels, copyToClipboard } from "./pages";
import type { CrashEvidence, Device, IssueEvidence, ReplayProgress, Report, Run, TestReport } from "../types";

type ToastTone = "success" | "error" | "info";
type ReplayDisplay = { text: string; tone: ToastTone; startMode?: string; strategy?: string };

export function crashCopyText(device: Report, crash: CrashEvidence): string {
  const pageText = crash.copiedInfo.trim();
  const crashLog = crash.crashLog.trim();
  return [
    `设备：${device.device} (${device.deviceSerial})`,
    `轮次：${crash.cycle}${crash.phase ? ` / 阶段 ${crash.phase}` : ""}`,
    `崩溃：${crash.exceptionType}${crash.message ? `: ${crash.message}` : ""}`,
    `签名：${crash.signature || "未生成"}`,
    `页面：${crash.pageType === "app_crash_page" ? "应用崩溃页" : "崩溃现场"}${crash.pageComponent ? ` (${crash.pageComponent})` : ""}`,
    `页面复制状态：${crash.copyStatus === "captured" ? "已获取页面文本" : "页面截图/层级已保存，但剪贴板文本为空"}`,
    crash.copyNote,
    crash.screenshotArtifact ? `截图证据：${crash.screenshotArtifact}` : "",
    "",
    "页面复制信息：",
    pageText || "本次崩溃页未返回剪贴板文本，以下为完整崩溃日志。",
    "",
    "崩溃日志：",
    crashLog || "未采集到崩溃文本",
  ].filter((line) => line !== "").join("\n");
}

export function developerReportText(report: TestReport, onlyDevice?: Report): string {
  const diagnosis = testDiagnosis(report);
  const devices = onlyDevice ? [onlyDevice] : report.devices;
  const lines = [
    "智能压测稳定性定位报告",
    `测试编号：${report.testId}`,
    `安装包：${report.apkName || "未记录"}`,
    `包名：${report.packageName}`,
    `时间：${formatDate(report.startedAt, true)} 至 ${formatDate(report.finishedAt, true)}`,
    `总体结果：${diagnosis.title}`,
    `压测覆盖：${formatDuration(report.pressureElapsedSeconds)} / ${formatDuration(report.pressureTargetSeconds)}（${report.pressurePercent}%）`,
    `设备：${report.reportedDevices} / ${report.deviceCount} 台有报告；操作 ${report.operations.toLocaleString()} 次${report.status === "running" ? "（仅统计已落盘阶段）" : ""}；崩溃 ${report.crashes} 类；页面静止观察 ${report.stalls} 次；随机事件保底恢复成功 ${report.fallbackRecoveries} 次；恢复失败 ${report.stallRecoveryFailures} 次；需复核问题 ${report.issues} 条`,
    diagnosis.detail ? `结论依据：${diagnosis.detail}` : "",
  ].filter(Boolean);
  if (report.errors?.length) lines.push("", "批次错误：", ...report.errors.map((item) => `- ${localizeRuntimeText(item)}`));

  devices.forEach((device, index) => {
    const result = deviceDiagnosis(device);
    const cycles = device.cycleResults || [];
    const problemCycles = cycles.filter((cycle) => failedCycleStatuses.has(cycle.status) || cycle.errors.length || cycle.warnings.length);
    const pressureLine = device.status === "running" && device.cycles === 0 && device.operations === 0
      ? `压力：当前阶段正在执行，时长、轮次和操作数尚未落盘；目标 ${formatDuration(device.pressureTargetSeconds)}`
      : `压力：${formatDuration(device.durationSeconds)} / ${formatDuration(device.pressureTargetSeconds)}（${device.pressurePercent}%），轮次 ${device.cycles}/${device.expectedCycles}，操作 ${device.operations.toLocaleString()} 次`;
    lines.push(
      "",
      `设备 ${index + 1}：${device.device}（${device.deviceSerial}）`,
      `系统：安卓 ${device.androidVersion || "未知"} / 接口级别 ${device.sdk || "未知"} / ${device.screenSize || "未知分辨率"}`,
      `结论：${result.title}`,
      pressureLine,
      `异常：崩溃 ${device.crashes} 类，保底恢复失败 ${device.stallRecoveryFailures} 次，需复核问题 ${device.issues} 条；执行记录：页面静止观察 ${device.stalls} 次，随机事件保底恢复成功 ${device.fallbackRecoveries} 次（成功恢复不计为应用稳定性问题）`,
    );
    if (device.permissionFailureReason) lines.push(`失败原因：${device.permissionFailureReason}`);
    if (device.errors?.length) lines.push("流程错误：", ...device.errors.map((item) => `- ${localizeRuntimeText(item)}`));
    if (problemCycles.length) {
      lines.push("异常轮次：", ...problemCycles.map((cycle) => {
        const detail = [...cycle.stopReasons.map(stopReasonLabel), ...cycle.errors.map(localizeRuntimeText), ...cycle.warnings.map(localizeRuntimeText)].filter(Boolean).join("；");
        return `- 第 ${cycle.cycle} 轮 ${cycleStatusLabel(cycle.status)}，执行 ${formatDuration(cycle.pressureElapsedSeconds)}${detail ? `：${detail}` : ""}`;
      }));
    }
    if (device.issueDetail?.length) {
      lines.push("问题记录：", ...device.issueDetail.map((issue) => { const display = issueDisplay(issue); return `- [${issue.severity}] ${display.title}${issue.description ? `：${evidenceExcerpt(localizeRuntimeText(issue.description), 800)}` : ""}${display.suggestion ? `；建议：${evidenceExcerpt(display.suggestion, 500)}` : ""}`; }));
    }
    device.crashDetail?.forEach((crash, crashIndex) => {
      lines.push(
        `崩溃 ${crashIndex + 1}：${crash.exceptionType}${crash.message ? ` - ${crash.message}` : ""}`,
        `签名：${crash.signature || "未生成"}`,
        `页面：${crash.pageComponent || "未捕获"}`,
        crash.replay.available
          ? `回放：可回放 ${crash.replay.eventCount} 个结构化事件${crash.replay.eventSource ? `（来源：${crash.replay.eventSource}）` : ""}`
          : `回放：不可用；${crash.replay.reason || "未找到结构化动作"}`,
        `日志：\n${evidenceExcerpt(crash.crashLog || crash.copiedInfo || "未采集到崩溃日志")}`,
      );
      const latestReplay = crash.replay.manualAttempts[crash.replay.manualAttempts.length - 1];
      if (latestReplay) {
        lines.push(
          `最近一次回放：${latestReplay.sameCrash ? "已复现相同崩溃" : latestReplay.executed ? "轨迹已执行但未再次触发相同崩溃" : "执行失败"}`
          + `，执行 ${latestReplay.eventCount || 0} 个事件`
          + (latestReplay.skippedEventCount ? `，跳过 ${latestReplay.skippedEventCount} 个文本事件` : ""),
        );
      }
    });
    lines.push(`可用证据文件：${device.artifacts.join("、") || "无"}`);
  });
  return lines.join("\n");
}

export function TestReportDialog({ report, onClose, onToast, onResume, onEnd, busyAction }: { report: TestReport; onClose: () => void; onToast: (text: string, tone?: ToastTone) => void; onResume: (jobId: string, serial: string) => void; onEnd: (jobId: string, serial: string) => void; busyAction: string | null }) {
  const [detail, setDetail] = useState<TestReport>(report);
  const [loading, setLoading] = useState(true);
  const [selectedSerial, setSelectedSerial] = useState(() => report.devices.find((item) => item.crashes)?.deviceSerial || report.devices[0]?.deviceSerial || "");
  const [replaying, setReplaying] = useState<string | null>(null);
  const [replayResults, setReplayResults] = useState<Record<string, ReplayDisplay>>({});
  const [replayProgress, setReplayProgress] = useState<Record<string, ReplayProgress>>({});

  const loadDetail = useCallback(async () => {
    const value = await api.testReport(report.id);
    setDetail(value);
    setSelectedSerial((current) => current || value.devices.find((item) => item.crashes)?.deviceSerial || value.devices[0]?.deviceSerial || "");
    return value;
  }, [report.id]);

  useEffect(() => {
    let cancelled = false;
    api.testReport(report.id)
      .then((value) => {
        if (cancelled) return;
        setDetail(value);
        setSelectedSerial((current) => current || value.devices.find((item) => item.crashes)?.deviceSerial || value.devices[0]?.deviceSerial || "");
      })
      .catch((error) => onToast(messageOf(error), "error"))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [report.id, onToast]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadDetail().catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(timer);
  }, [loadDetail]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  const selected = detail.devices.find((item) => item.deviceSerial === selectedSerial) || detail.devices[0];
  const diagnosis = testDiagnosis(detail);

  async function handleCopyDeveloperSummary(device?: Report) {
    try {
      const latest = await loadDetail();
      const latestDevice = device
        ? latest.devices.find((item) => item.deviceSerial === device.deviceSerial)
        : undefined;
      await copyToClipboard(developerReportText(latest, latestDevice));
      onToast(device ? "设备定位摘要已复制" : "开发定位报告已复制");
    } catch (error) {
      onToast(messageOf(error), "error");
    }
  }

  async function handleDownloadDeveloperSummary() {
    try {
      const latest = await loadDetail();
      const blob = new Blob([developerReportText(latest)], { type: "text/plain;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${latest.testId.replace(/[^A-Za-z0-9._-]+/g, "_") || "fastbot"}_开发定位报告.txt`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      onToast("开发定位报告已导出");
    } catch (error) {
      onToast(messageOf(error), "error");
    }
  }

  async function handleCopy(device: Report, crash: CrashEvidence) {
    try {
      await copyToClipboard(crashCopyText(device, crash));
      onToast("崩溃页面信息已复制");
    } catch (error) {
      onToast(messageOf(error), "error");
    }
  }

  async function handleReplay(device: Report, crash: CrashEvidence, startMode: "auto" | "current") {
    if (!device.id || !crash.replay.available) return;
    const anchor = crash.replay.pageAnchor?.activity || crash.replay.pageAnchor?.component || "未记录页面锚点";
    const approved = window.confirm(
      startMode === "auto"
        ? `将在设备 ${device.deviceSerial} 上尝试恢复崩溃前页面（${anchor}），不主动清除应用数据，然后回放最近 ${crash.replay.eventCount} 个事件。若页面已被系统回收，会停止并提示手动打开页面。是否继续？`
        : `请先在设备 ${device.deviceSerial} 上打开崩溃前页面（${anchor}），确认页面状态后仅回放最近 ${crash.replay.eventCount} 个事件。是否继续？`,
    );
    if (!approved) return;
    const key = `${device.id}-${crash.index}`;
    setReplaying(key);
    setReplayProgress((current) => ({
      ...current,
      [key]: { status: "preparing", executed: 0, total: crash.replay.eventCount, skipped: 0, message: startMode === "auto" ? "正在恢复崩溃前页面" : "正在校验当前页面", startMode },
    }));
    let polling = true;
    const pollProgress = async () => {
      try {
        const progress = await api.replayProgress(device.id as string, crash.index);
        if (polling) setReplayProgress((current) => ({ ...current, [key]: progress }));
      } catch {
        // The replay request remains authoritative; a transient poll failure
        // should not make the action look failed.
      }
    };
    void pollProgress();
    const timer = window.setInterval(() => { void pollProgress(); }, 600);
    try {
      const result = await api.replayCrash(device.id, crash.index, startMode);
      const skipped = result.skippedEventCount ? `，跳过 ${result.skippedEventCount} 个不支持的文本事件` : "";
      const strategy = result.replayStart?.strategy || "";
      const strategyText = strategy ? `（起点：${strategy}）` : "";
      const display: ReplayDisplay = result.sameCrash
        ? { text: `已复现相同崩溃${skipped}${strategyText}`, tone: "success", startMode, strategy }
        : result.executed
        ? { text: `轨迹已执行，但本次未再次触发相同崩溃${skipped}${strategyText}`, tone: "info", startMode, strategy }
        : { text: result.error || "复现执行失败", tone: "error", startMode, strategy };
      setReplayResults((current) => ({ ...current, [key]: display }));
      onToast(display.text, display.tone);
      await loadDetail();
    } catch (error) {
      const text = messageOf(error);
      setReplayResults((current) => ({ ...current, [key]: { text, tone: "error" } }));
      onToast(text, "error");
    } finally {
      polling = false;
      window.clearInterval(timer);
      setReplaying(null);
    }
  }

  return <div className="modal-layer test-report-layer" role="presentation" onMouseDown={onClose}>
    <section className="report-dialog test-report-dialog" role="dialog" aria-modal="true" aria-labelledby="test-report-title" onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-heading test-dialog-heading">
        <div><span className="eyebrow">{detail.testId}</span><h2 id="test-report-title">{detail.apkName || detail.packageName}</h2><code>{detail.packageName}</code></div>
        <div className="test-dialog-actions">
          <button className="secondary-button" type="button" onClick={() => handleCopyDeveloperSummary()} disabled={loading} title="复制可直接转发给开发的文本报告"><Copy size={16} />复制定位报告</button>
          <button className="secondary-button" type="button" onClick={handleDownloadDeveloperSummary} disabled={loading} title="导出可转发的 TXT 定位报告"><Download size={16} />导出 TXT</button>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭"><X size={20} /></button>
        </div>
      </div>
      <div className="test-result-summary">
        <ReportStatus status={detail.status} />
        <div><small>测试设备</small><strong>{detail.reportedDevices} / {detail.deviceCount} 台有报告</strong></div>
        <div><small>有效压测</small><strong>{formatDuration(detail.pressureElapsedSeconds)} / {formatDuration(detail.pressureTargetSeconds)}</strong></div>
        <div><small>压力覆盖</small><strong>{detail.pressurePercent}%</strong></div>
        <div className={detail.crashes ? "has-risk" : ""}><small>崩溃</small><strong>{detail.crashes} 个</strong></div>
      </div>
      <div className={`test-diagnosis-strip ${diagnosis.tone}`}>
        <span className="diagnosis-icon">{diagnosis.tone === "ok" ? <CheckCircle2 size={22} /> : <AlertTriangle size={22} />}</span>
        <div><small>本次测试结论</small><strong>{diagnosis.title}</strong><p>{diagnosis.detail}</p></div>
      </div>
      {loading ? <div className="test-report-loading"><LoaderCircle size={22} className="spin" />正在整理设备报告与崩溃证据</div> : <div className="test-report-body">
        <aside className="test-device-nav" aria-label="测试设备">
          <div className="test-device-nav-title"><span>设备测试情况</span><small>{detail.devices.length} 台</small></div>
          {detail.devices.map((device) => { const deviceResult = deviceDiagnosis(device); return <button type="button" className={device.deviceSerial === selected?.deviceSerial ? "is-active" : ""} key={device.deviceSerial} onClick={() => setSelectedSerial(device.deviceSerial)}>
            <span className="device-report-icon"><Smartphone size={18} /></span>
            <span><strong>{device.device}</strong><small>{device.deviceSerial}</small><i><span style={{ width: `${Math.min(100, device.pressurePercent || 0)}%` }} /></i><em className={deviceResult.tone}>{deviceResult.title}</em></span>
            <span><ReportStatus status={device.status} manualEnd={device.manualEnd} />{device.crashes > 0 && <b><Bug size={12} />{device.crashes}</b>}</span>
          </button>; })}
        </aside>
        <div className="test-device-detail">
          {selected ? <DeviceReportDetail device={selected} replaying={replaying} replayResults={replayResults} replayProgress={replayProgress} onCopy={handleCopy} onCopySummary={() => handleCopyDeveloperSummary(selected)} onReplay={handleReplay} onResume={() => onResume(detail.testId, selected.deviceSerial)} onEnd={() => onEnd(detail.testId, selected.deviceSerial)} endBusy={busyAction === `end-${selected.deviceSerial}`} /> : <EmptyState icon={Smartphone} title="没有设备报告" note="该测试未生成设备级报告" />}
        </div>
      </div>}
    </section>
  </div>;
}

export function DeviceReportDetail({ device, replaying, replayResults, replayProgress, onCopy, onCopySummary, onReplay, onResume, onEnd, endBusy }: { device: Report; replaying: string | null; replayResults: Record<string, ReplayDisplay>; replayProgress: Record<string, ReplayProgress>; onCopy: (device: Report, crash: CrashEvidence) => void; onCopySummary: () => void; onReplay: (device: Report, crash: CrashEvidence, startMode: "auto" | "current") => void; onResume: () => void; onEnd: () => void; endBusy: boolean }) {
  if (!device.reportAvailable && !device.checkpointAvailable) return <div className="missing-device-report"><FileArchive size={28} /><strong>{device.deviceSerial}</strong><span>{device.manualEnd ? "该设备测试已手动结束，设备已释放。" : device.status === "running" ? "设备任务仍在运行，阶段报告尚未落盘。" : "该设备没有生成可读取的报告，测试流程未完整收尾。"}</span></div>;
  if (!device.reportAvailable && device.checkpointAvailable) {
    const current = device.currentCycle || {};
    const state = String(device.resumeState || device.status || "recovering");
    const stateLabel: Record<string, string> = {
      recovering: "自动恢复中",
      waiting_for_device: "等待设备回连",
      resuming: "准备续跑",
      paused: "已暂停",
      needs_manual_resume: "需要手动继续",
      manually_ended: "已手动结束",
      running: "运行中",
    };
    const cycle = Number(current.cycle || 0);
    const phase = Number(current.phase || 0);
    const elapsed = Number(current.pressure_elapsed_seconds || current.elapsed_seconds || device.durationSeconds || 0);
    const remaining = Number(device.remainingSeconds || current.remaining_seconds || 0);
    return <div className="checkpoint-recovery-detail">
      <header className="device-report-heading"><div><span className="eyebrow">设备检查点</span><h3>{device.device || device.deviceSerial}</h3><code>{device.deviceSerial}</code></div><div className="device-heading-actions"><ReportStatus status={device.status} manualEnd={device.manualEnd} />{device.canResume && <button className="primary-button" type="button" onClick={onResume}><Play size={15} />继续运行</button>}{device.canEnd && <button className="danger-button" type="button" onClick={onEnd} disabled={endBusy}>{endBusy ? <LoaderCircle size={15} className="spin" /> : <XCircle size={15} />}手动结束本设备</button>}</div></header>
      <div className="device-diagnosis info"><span><Info size={22} /></span><div><small>恢复状态</small><strong>{stateLabel[state] || state}</strong><p>{localizeRuntimeText(device.lastError || "检查点已完整保存，后续执行进程会在同一任务和设备目录中继续。")}</p></div></div>
      <dl className="device-environment checkpoint-facts">
        <div><dt>当前轮次</dt><dd>{cycle || "—"}{phase ? ` · 阶段 ${phase}` : ""}</dd></div>
        <div><dt>本轮已运行</dt><dd>{Math.floor(elapsed)} 秒</dd></div>
        <div><dt>剩余时间</dt><dd>{Math.ceil(remaining)} 秒</dd></div>
        <div><dt>操作数</dt><dd>{Number(device.operations || current.operation_count || 0).toLocaleString()}</dd></div>
        <div><dt>恢复次数</dt><dd>{device.resumeAttempts || 0}</dd></div>
        <div><dt>最后心跳</dt><dd>{formatDate(device.lastHeartbeat, true)}</dd></div>
        <div><dt>前台组件</dt><dd><code>{device.foregroundComponent || "未记录"}</code></dd></div>
        <div><dt>压测进程编号</dt><dd>{device.fastbotPid || "已退出/未记录"}</dd></div>
      </dl>
      <section className="device-report-section"><div className="device-section-heading"><div><span className="eyebrow">持久化证据</span><h4>恢复所需文件</h4></div></div><div className="device-artifact-links"><span><FileJson size={17} /><span><strong>检查点文件</strong><small>{device.checkpointPath || "已保存"}</small></span></span>{device.remoteLogPath ? <span><TerminalSquare size={17} /><span><strong>远端压测日志</strong><small>{device.remoteLogPath} · 读取位置 {device.remoteLogOffset || 0}</small></span></span> : null}</div></section>
      {device.canResume ? <div className="checkpoint-resume-callout"><AlertTriangle size={17} /><span>该设备处于人工恢复状态，点击“继续运行”会复用原任务编号，并从剩余时间继续。</span><button className="secondary-button" type="button" onClick={onResume}><Play size={15} />继续运行</button></div> : null}
    </div>;
  }
  const cycles = device.cycleResults || [];
  const crashes = device.crashDetail || [];
  const issues = device.issueDetail || [];
  const recoveries = device.recoveryDetail || [];
  const warnings = device.collectionWarnings || [];
  const problemCycles = cycles.filter((cycle) => failedCycleStatuses.has(cycle.status) || cycle.errors.length || cycle.warnings.length);
  const diagnosis = deviceDiagnosis(device);
  const hasStabilityRisk = Boolean(device.crashes || device.stallRecoveryFailures || device.issues);
  return <>
    <header className="device-report-heading"><div><span className="eyebrow">设备报告</span><h3>{device.device}</h3><code>{device.deviceSerial}</code></div><div className="device-heading-actions"><ReportStatus status={device.status} manualEnd={device.manualEnd} />{device.canEnd && <button className="danger-button" type="button" onClick={onEnd} disabled={endBusy}>{endBusy ? <LoaderCircle size={15} className="spin" /> : <XCircle size={15} />}手动结束本设备</button>}<button className="secondary-button" type="button" onClick={onCopySummary} title="复制这台设备的定位信息"><Copy size={15} />复制设备定位</button></div></header>
    <div className={`device-diagnosis ${diagnosis.tone}`}>
      <span>{diagnosis.tone === "ok" ? <CheckCircle2 size={22} /> : <AlertTriangle size={22} />}</span>
      <div><small>{device.permissionFailureReason ? "失败原因" : "定位结论"}</small><strong>{diagnosis.title}</strong><p>{diagnosis.detail}</p></div>
    </div>
    <dl className="device-environment">
      <div><dt>设备序列号</dt><dd>{device.deviceSerial}</dd></div>
      <div><dt>安卓版本</dt><dd>{device.androidVersion || "未知"}</dd></div>
      <div><dt>系统接口级别</dt><dd>{device.sdk || "未知"}</dd></div>
      <div><dt>分辨率</dt><dd>{device.screenSize || "未知"}</dd></div>
    </dl>
    <div className="device-pressure-summary">
      <div><small>实际压测</small><strong>{formatDuration(device.durationSeconds)}</strong><span>目标 {formatDuration(device.pressureTargetSeconds)}</span></div>
      <div><small>压力覆盖</small><strong>{device.pressurePercent}%</strong><span>{device.cycles} / {device.expectedCycles} 轮有记录</span></div>
      <div><small>操作次数</small><strong>{device.operations.toLocaleString()}</strong><span>智能压测输入事件</span></div>
      <div className={hasStabilityRisk ? "has-risk" : ""}><small>应用稳定性结论</small><strong>{device.crashes ? `${device.crashes} 类崩溃` : device.stallRecoveryFailures ? `${device.stallRecoveryFailures} 次保底恢复失败` : device.issues ? `${device.issues} 条风险待复核` : "未发现稳定性异常"}</strong><span>{device.fallbackRecoveries ? `${device.fallbackRecoveries} 次随机事件保底恢复成功，不计缺陷` : "未触发随机事件保底恢复"}</span></div>
    </div>
    <section className="device-report-section">
      <div className="device-section-heading"><div><span className="eyebrow">压力执行</span><h4>逐轮压测情况</h4></div><strong>{formatDuration(device.durationSeconds)} / {formatDuration(device.pressureTargetSeconds)}</strong></div>
      {cycles.length ? <div className="cycle-result-grid">{cycles.map((cycle) => { const needsAttention = failedCycleStatuses.has(cycle.status) || cycle.errors.length > 0 || cycle.warnings.length > 0; return <div className={`cycle-result ${cycle.status}`} key={cycle.cycle}><span>第 {cycle.cycle} 轮 · {cycleStatusLabel(cycle.status)}</span><strong>{formatDuration(cycle.pressureElapsedSeconds)}</strong><small>{cycle.operations.toLocaleString()} 次操作</small>{(cycle.crashSignatures.length > 0 || needsAttention) && <b>{cycle.crashSignatures.length > 0 ? <Bug size={12} /> : <AlertTriangle size={12} />}{cycle.crashSignatures.length > 0 ? "检测到崩溃" : "需要关注"}</b>}</div>; })}</div> : <div className="inline-report-empty">未记录到有效压力轮次</div>}
    </section>
    {recoveries.length ? <section className="device-report-section fallback-recovery-section">
      <div className="device-section-heading"><div><span className="eyebrow">执行保底记录</span><h4>随机事件页面脱困与继续压测</h4></div><strong>{device.fallbackRecoveries} 次恢复成功</strong></div>
      <div className="issue-evidence-list">{recoveries.map((record) => <article className="issue-evidence info" key={record.id}><span><CheckCircle2 size={17} /></span><div><strong>{localizeRuntimeText(record.title)}</strong><p>{localizeRuntimeText(record.description)}</p><small>第 {record.cycle || "-"} 轮{record.component ? ` · ${record.component}` : ""}{record.detectedAt ? ` · ${formatDate(record.detectedAt, true)}` : ""}</small><div className="fallback-note">这是随机事件工具无法自行退出页面时的保底动作；恢复成功且无应用无响应或崩溃证据时，不计为应用稳定性问题。</div></div></article>)}</div>
    </section> : null}
    {(device.errors?.length || problemCycles.length || issues.length || warnings.length) ? <section className="device-report-section issue-report-section">
      <div className="device-section-heading"><div><span className="eyebrow">问题定位</span><h4>异常原因与排查建议</h4></div><strong>{(device.errors?.length || 0) + problemCycles.length + issues.length + warnings.length} 条</strong></div>
      <div className="issue-evidence-list">
        {device.errors?.map((error, index) => <article className="issue-evidence high" key={`error-${index}-${error}`}><span><XCircle size={17} /></span><div><strong>流程执行错误</strong><p>{localizeRuntimeText(error)}</p></div></article>)}
        {problemCycles.map((cycle) => { const details = [...cycle.stopReasons.map(stopReasonLabel), ...cycle.errors.map(localizeRuntimeText), ...cycle.warnings.map(localizeRuntimeText)].filter(Boolean); return <article className="issue-evidence high" key={`cycle-${cycle.cycle}`}><span><AlertTriangle size={17} /></span><div><strong>第 {cycle.cycle} 轮{cycleStatusLabel(cycle.status)}</strong><p>实际执行 {formatDuration(cycle.pressureElapsedSeconds)}，{cycle.operations.toLocaleString()} 次操作{details.length ? `；${details.join("；")}` : ""}</p></div></article>; })}
        {issues.map((issue) => { const display = issueDisplay(issue); return <article className={`issue-evidence ${issue.severity}`} key={issue.id}><span><AlertTriangle size={17} /></span><div><strong>{display.title}</strong><p>{localizeRuntimeText(issue.description || "报告记录了异常，但没有补充问题描述。")}</p><small>第 {issue.cycle || "-"} 轮{issue.component ? ` · ${issue.component}` : ""}{issue.detectedAt ? ` · ${formatDate(issue.detectedAt, true)}` : ""}</small>{display.suggestion && <div className="issue-suggestion"><b>建议排查</b><p>{display.suggestion}</p></div>}{issue.keyLog && <details><summary>查看关键日志</summary><pre>{issue.keyLog}</pre></details>}</div></article>; })}
        {warnings.map((warning) => <article className="issue-evidence warning" key={warning}><span><AlertTriangle size={17} /></span><div><strong>证据采集告警</strong><p>{localizeRuntimeText(warning)}</p></div></article>)}
      </div>
    </section> : null}
    <section className="device-report-section crash-report-section">
      <div className="device-section-heading"><div><span className="eyebrow">崩溃证据</span><h4>{crashes.length ? `${crashes.length} 类崩溃` : "未检测到崩溃"}</h4></div>{crashes.length > 0 && <span className="crash-review-count"><Bug size={15} />共 {crashes.reduce((sum, item) => sum + item.occurrenceCount, 0)} 次命中</span>}</div>
      {crashes.length ? crashes.map((crash) => { const replayKey = `${device.id}-${crash.index}`; return <CrashEvidenceView key={replayKey} device={device} crash={crash} replaying={replaying === replayKey} replayProgress={replayProgress[replayKey]} replayResult={replayResults[replayKey]} onCopy={() => onCopy(device, crash)} onReplay={(startMode) => onReplay(device, crash, startMode)} />; }) : <div className="no-crash-state"><CheckCircle2 size={22} /><div><strong>没有崩溃证据</strong><span>该设备报告中未记录目标应用崩溃。</span></div></div>}
    </section>
    <section className="device-report-section"><div className="device-section-heading"><div><span className="eyebrow">报告产物</span><h4>文件与证据</h4></div></div><div className="device-artifact-links">{device.artifacts.length ? device.artifacts.map((name) => { const meta = artifactLabels[name] || { title: name, note: "测试产物", icon: FileArchive }; const Icon = meta.icon; const inline = name === "html" || name === "screen_record"; return <a href={api.artifactUrl(device.id, name)} target={inline ? "_blank" : undefined} rel="noreferrer" download={!inline} key={name}><Icon size={17} /><span><strong>{meta.title}</strong><small>{meta.note}</small></span>{inline ? <ExternalLink size={15} /> : <Download size={15} />}</a>; }) : <span className="inline-report-empty">没有可用的报告文件</span>}</div></section>
  </>;
}

export function CrashEvidenceView({ device, crash, replaying, replayProgress, replayResult, onCopy, onReplay }: { device: Report; crash: CrashEvidence; replaying: boolean; replayProgress?: ReplayProgress; replayResult?: ReplayDisplay; onCopy: () => void; onReplay: (startMode: "auto" | "current") => void }) {
  const screenshotUrl = crash.screenshotArtifact ? api.artifactUrl(device.id, crash.screenshotArtifact) : "";
  const automaticReproduced = crash.reproducible || crash.replay.automaticAttempts.some((attempt) => attempt.reproducible);
  const latestReplay = crash.replay.manualAttempts.length ? crash.replay.manualAttempts[crash.replay.manualAttempts.length - 1] : undefined;
  return <article className="crash-evidence-block">
    <div className="crash-evidence-heading"><span className="crash-index"><Bug size={18} /></span><div><strong>{crash.exceptionType}</strong><small>第 {crash.cycle} 轮{crash.phase ? ` · 阶段 ${crash.phase}` : ""} · 命中 {crash.occurrenceCount} 次 · {formatDate(crash.detectedAt, true)}</small></div><span className={`reproducible-state ${automaticReproduced ? "is-yes" : ""}`}>{automaticReproduced ? "已自动复现" : "未自动复现"}</span></div>
    <p className="crash-message">{crash.message || "未记录异常消息"}</p>
    <dl className="crash-facts"><div><dt>崩溃签名</dt><dd>{crash.signature || "未生成"}</dd></div><div><dt>代码位置</dt><dd>{crash.location || crash.source || "未定位"}</dd></div><div><dt>崩溃页面</dt><dd>{crash.pageComponent ? <><strong>{crash.pageType === "app_crash_page" ? "应用崩溃页" : "崩溃现场页面"}</strong><code>{crash.pageComponent}</code></> : "未捕获到页面组件"}</dd></div></dl>
    <div className="crash-evidence-actions">
      <button className="secondary-button" type="button" onClick={onCopy} disabled={!crash.copiedInfo && !crash.crashLog && !crash.signature && !crash.pageComponent && !crash.screenshotArtifact}><Copy size={16} />复制崩溃定位信息</button>
      <button className="primary-button replay-button" type="button" onClick={() => onReplay("auto")} disabled={replaying || !crash.replay.available} title={crash.replay.available ? "优先恢复保存的任务栈和崩溃前页面，再回放短尾部" : localizeRuntimeText(crash.replay.reason)}>{replaying ? <LoaderCircle size={16} className="spin" /> : <RotateCcw size={16} />}{replaying ? (replayProgress?.status === "preparing" ? "准备中" : `复现 ${Math.min(replayProgress?.executed || 0, replayProgress?.total || crash.replay.eventCount)} / ${replayProgress?.total || crash.replay.eventCount}`) : "智能复现"}</button>
      <button className="secondary-button replay-button" type="button" onClick={() => onReplay("current")} disabled={replaying || !crash.replay.available} title="先在设备上打开崩溃前页面，再只回放短尾部事件"><Play size={16} />从当前页复现</button>
      {crash.replay.available && <span className="replay-capability">短回放 {crash.replay.eventCount} 个事件{crash.replay.originalEventCount && crash.replay.originalEventCount > crash.replay.eventCount ? `（原始 ${crash.replay.originalEventCount}）` : ""}{crash.replay.windowSeconds ? ` · 最近 ${crash.replay.windowSeconds} 秒` : ""}{crash.replay.pageAnchor?.activity ? ` · 起点 ${crash.replay.pageAnchor.activity}` : ""}{crash.replay.eventSource ? ` · ${crash.replay.eventSource}` : ""}</span>}
      {replaying && replayProgress && <span className="replay-progress-text">{replayProgress.message || "正在执行回放"} · {replayProgress.executed} / {replayProgress.total} 个事件{replayProgress.skipped ? ` · 跳过 ${replayProgress.skipped}` : ""}{replayProgress.foregroundRecoveryCount ? ` · 已拉回应用 ${replayProgress.foregroundRecoveryCount} 次` : ""}</span>}
      {!crash.replay.available && <span className="replay-unavailable">{crash.replay.reason || "未找到该阶段的结构化动作；FBM 是二进制模型，不是回放脚本。"}</span>}
      {replayResult && <span className={`replay-result ${replayResult.tone}`}>{replayResult.text}</span>}
      {!replayResult && latestReplay && <span className="replay-history">上次回放：{latestReplay.sameCrash ? "已复现" : latestReplay.executed ? "已执行，未复现" : "执行失败"} · {latestReplay.eventCount || 0} 个事件{latestReplay.skippedEventCount ? ` · 跳过 ${latestReplay.skippedEventCount} 个文本事件` : ""}</span>}
    </div>
    <div className="crash-evidence-content">
      <figure className="crash-screenshot">{screenshotUrl ? <><a href={screenshotUrl} target="_blank" rel="noreferrer"><img src={screenshotUrl} alt={`第 ${crash.cycle} 轮崩溃页面截图`} /><span><ExternalLink size={15} />查看原图</span></a><figcaption><Camera size={14} />{crash.pageType === "app_crash_page" ? "应用崩溃页截图" : "崩溃发生时页面"}</figcaption></> : <div><Camera size={26} /><strong>没有崩溃页面截图</strong><span>该次崩溃未生成有效截图文件</span></div>}</figure>
      <div className="crash-text-evidence"><div><span>{crash.pageType === "app_crash_page" ? "应用崩溃页复制信息" : "崩溃页面复制信息"}</span><pre>{crash.copiedInfo || crash.copyNote || "本次崩溃页没有返回剪贴板文本，但截图和运行时证据已保存。"}</pre></div><details open><summary>崩溃日志</summary><pre>{crash.crashLog || "未采集到崩溃堆栈"}</pre></details></div>
    </div>
  </article>;
}
