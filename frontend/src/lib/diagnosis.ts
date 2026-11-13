import { localizeRuntimeText } from "./format";
import type { IssueEvidence, Report, TestReport } from "../types";

export type DiagnosisTone = "critical" | "warning" | "ok" | "info";

export interface Diagnosis {
  tone: DiagnosisTone;
  title: string;
  detail: string;
  points: string[];
}

export const failedCycleStatuses = new Set(["failed", "crash", "error", "interrupted", "stopped"]);

export function cycleStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    passed: "完成",
    completed: "完成",
    failed: "失败",
    crash: "崩溃中止",
    error: "执行错误",
    interrupted: "被中断",
    stopped: "已停止",
    running: "运行中",
  };
  return labels[status] || status || "未知";
}

export function stopReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    deadline: "达到本阶段时长",
    crash: "检测到崩溃",
    process_exit: "应用进程退出",
    device_disconnected: "设备断开连接",
    manual_stop: "人工停止",
    stall_recovery: "卡页恢复",
    audit_recovery: "启动门禁恢复",
    foreground_recovery: "前台恢复",
    permission_recovery: "处理系统权限弹窗",
    permission_recovery_unclean: "权限处理时未完成设备控制交接",
    pressure_engine_exit: "压力引擎异常退出",
  };
  return labels[reason] || reason;
}

export function deviceDiagnosis(device: Report): Diagnosis {
  if (device.manualEnd) {
    return {
      tone: "info",
      title: "已手动结束本设备测试",
      detail: "旧任务的剩余进度已关闭且不能再继续，该设备已释放，可用于下一个测试任务。",
      points: ["人工结束终态已保存", "设备不再占用旧任务"],
    };
  }
  if (device.reportAvailable === false) {
    if (device.checkpointAvailable) {
      const state = String(device.resumeState || device.status || "recovering");
      const stateText: Record<string, string> = {
        recovering: "正在自动恢复",
        waiting_for_device: "等待设备回连",
        resuming: "准备续跑",
        paused: "已暂停，等待继续运行",
        needs_manual_resume: "需要手动继续运行",
        running: "正在运行",
      };
      return {
        tone: state === "paused" || state === "needs_manual_resume" ? "warning" : "info",
        title: stateText[state] || "保留了可恢复检查点",
        detail: localizeRuntimeText(device.lastError || "设备报告尚未落盘，但设备检查点已保存，可从当前轮次继续。"),
        points: [
          device.currentCycle?.cycle ? `第 ${String(device.currentCycle.cycle)} 轮` : "当前轮次已保存",
          device.remainingSeconds != null ? `剩余 ${Math.ceil(device.remainingSeconds)} 秒` : "剩余时间已保存",
          device.resumeAttempts ? `已尝试恢复 ${device.resumeAttempts} 次` : "尚未尝试自动恢复",
        ],
      };
    }
    if (device.status === "running") {
      return { tone: "info", title: "等待设备报告落盘", detail: "设备任务仍在运行，尚未生成可读取的阶段报告。", points: ["运行中数据尚未落盘"] };
    }
    return { tone: "critical", title: "未生成设备报告", detail: "测试进程没有完整收尾，开发定位证据缺失。", points: ["无设备级 report.json"] };
  }
  const cycles = device.cycleResults || [];
  const failedCycles = cycles.filter((cycle) => failedCycleStatuses.has(cycle.status));
  const cycleErrors = failedCycles.flatMap((cycle) => cycle.errors).filter(Boolean).map(localizeRuntimeText);
  const topErrors = (device.errors || []).filter(Boolean).map(localizeRuntimeText);
  const points: string[] = [];
  if (device.crashes) points.push(`${device.crashes} 类崩溃`);
  if (failedCycles.length) points.push(`${failedCycles.length} 个异常轮次`);
  if (device.status !== "running" && device.pressureTargetSeconds > 0 && device.pressurePercent < 90) points.push(`压力时长仅完成 ${device.pressurePercent}%`);
  if (device.fallbackRecoveries) points.push(`${device.fallbackRecoveries} 次随机事件保底恢复成功`);
  if (device.stallRecoveryFailures) points.push(`${device.stallRecoveryFailures} 次保底恢复失败`);
  if (device.stalls > device.fallbackRecoveries) points.push(`${device.stalls} 次页面静止观察`);
  if (device.issues) points.push(`${device.issues} 条需复核问题`);
  if (topErrors.length || cycleErrors.length) points.push((topErrors[0] || cycleErrors[0]).slice(0, 160));
  if (device.collectionWarnings?.length) points.push(`证据采集告警：${localizeRuntimeText(device.collectionWarnings[0])}`);

  if (device.status === "running") {
    if (device.crashes) return { tone: "critical", title: `压测正在运行，已检测到 ${device.crashes} 类崩溃`, detail: points.slice(0, 4).join("；"), points };
    if (topErrors.length || cycleErrors.length || failedCycles.length) return { tone: "critical", title: "压测正在运行，已记录流程异常", detail: points.slice(0, 4).join("；"), points };
    const pending = device.cycles === 0 && device.operations === 0;
    return { tone: "info", title: "压测正在运行", detail: pending ? "当前阶段尚未结束，轮次、操作数和压力时长将在阶段落盘后更新。" : points.join("；") || `已记录 ${device.pressurePercent}% 压力时长。`, points };
  }
  if (device.permissionFailureReason) return { tone: "critical", title: "权限处理期间压测中断", detail: device.permissionFailureReason, points };
  if (device.crashes) return { tone: "critical", title: `检测到 ${device.crashes} 类崩溃，需要开发处理`, detail: points.slice(0, 4).join("；"), points };
  if (topErrors.length || cycleErrors.length || failedCycles.length) return { tone: "critical", title: "压测流程存在失败或中断", detail: points.slice(0, 4).join("；"), points };
  if (device.pressureTargetSeconds > 0 && device.pressurePercent < 90) return { tone: "warning", title: `压测未跑满，仅完成 ${device.pressurePercent}%`, detail: points.join("；"), points };
  if (device.stallRecoveryFailures || device.issues) return { tone: "warning", title: "记录到需要复核的测试风险", detail: points.join("；"), points };
  return { tone: "ok", title: "未发现应用稳定性问题", detail: `完成 ${device.pressurePercent}% 压力时长，未检测到崩溃、应用无响应或流程错误。${device.fallbackRecoveries ? `随机事件保底恢复成功 ${device.fallbackRecoveries} 次，不计为应用稳定性缺陷。` : ""}`, points: [] };
}

export function runningDeviceCount(report: TestReport): number {
  if (report.status !== "running") return report.runningDevices;
  const liveDevices = report.devices.filter((device) => device.status === "running").length;
  return Math.max(report.runningDevices, liveDevices);
}

export function testDiagnosis(report: TestReport): Diagnosis {
  const missing = Math.max(0, report.deviceCount - report.reportedDevices);
  const active = report.status === "running";
  const runningDevices = runningDeviceCount(report);
  const affected = report.devices.filter((device) => ["critical", "warning"].includes(deviceDiagnosis(device).tone)).length;
  const points = [
    missing ? `${missing} 台设备${active ? "报告待落盘" : "没有报告"}` : "",
    report.failedDevices ? `${report.failedDevices} 台设备失败` : "",
    runningDevices ? `${runningDevices} 台设备运行中` : "",
    report.crashes ? `${report.crashes} 类崩溃` : "",
    report.fallbackRecoveries ? `${report.fallbackRecoveries} 次随机事件保底恢复成功` : "",
    report.stallRecoveryFailures ? `${report.stallRecoveryFailures} 次保底恢复失败` : "",
    report.pressureTargetSeconds > 0 && report.pressurePercent < 90 ? `${active ? "当前已记录" : "总"}压力覆盖 ${report.pressurePercent}%` : "",
  ].filter(Boolean);
  if (active) {
    const running = runningDevices || Math.max(0, report.deviceCount - report.failedDevices);
    if (report.crashes) return { tone: "critical", title: `测试进行中：已检测到 ${report.crashes} 类崩溃，${running} 台仍在运行`, detail: points.join("；"), points };
    if (report.failedDevices) return { tone: "critical", title: `测试进行中：${report.failedDevices} 台已中断，${running} 台仍在运行`, detail: points.join("；"), points };
    return { tone: "info", title: `测试进行中：${running || report.deviceCount} 台设备正在压测`, detail: points.join("；") || "设备仍在执行压力测试。", points };
  }
  if (report.crashes) return { tone: "critical", title: `${affected} 台设备存在问题，检测到 ${report.crashes} 类崩溃`, detail: points.join("；"), points };
  if (report.failedDevices || missing) return { tone: "critical", title: `${affected || report.failedDevices + missing} 台设备失败或缺少报告`, detail: points.join("；"), points };
  if (report.status === "running" || report.status === "incomplete") return { tone: "warning", title: "本次测试没有完整结束，不能判定为通过", detail: points.join("；") || "仍有设备处于运行或未收尾状态。", points };
  if (report.stallRecoveryFailures || report.issues) return { tone: "warning", title: `${affected} 台设备有测试风险需要复核`, detail: points.join("；"), points };
  if (report.status === "passed") return { tone: "ok", title: "本次测试通过，未发现应用稳定性问题", detail: `${report.reportedDevices} 台设备均完成目标压力时长。${report.fallbackRecoveries ? `随机事件保底恢复成功 ${report.fallbackRecoveries} 次，不作为应用缺陷。` : ""}`, points: [] };
  return { tone: "info", title: "本次测试已停止", detail: points.join("；") || "请结合设备明细确认停止原因。", points };
}

export function evidenceExcerpt(value: string, limit = 3000): string {
  const text = value.trim();
  return text.length > limit ? `${text.slice(0, limit)}\n...（内容已截断）` : text;
}

export function issueDisplay(issue: IssueEvidence): { title: string; suggestion: string } {
  const evidence = `${issue.description || ""}\n${issue.keyLog || ""}\n${issue.recoveryError || ""}`.toLowerCase();
  if (evidence.includes("screenshot capture returned no data") || evidence.includes("screencap returned no data")) {
    return {
      title: "截图验证不可用",
      suggestion: "检查页面是否启用了安全截图保护标志或厂商截图限制，并结合前台页面连续采样确认恢复；该错误与压测运行包同步无关。",
    };
  }
  return { title: issue.title, suggestion: issue.suggestion };
}
