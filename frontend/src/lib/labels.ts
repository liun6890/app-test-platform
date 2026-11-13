import { Activity, BarChart3, LayoutDashboard, MonitorSmartphone, Settings2 } from "lucide-react";
import type { Run, View } from "../types";

export const navItems: Array<{ id: View; label: string; icon: typeof Activity }> = [
  { id: "overview", label: "概览", icon: LayoutDashboard },
  { id: "devices", label: "设备", icon: MonitorSmartphone },
  { id: "config", label: "测试配置", icon: Settings2 },
  { id: "runs", label: "运行监控", icon: Activity },
  { id: "reports", label: "报告", icon: BarChart3 },
];

export const pageTitles: Record<View, { title: string; meta: string }> = {
  overview: { title: "App 测试平台", meta: "本机测试环境" },
  devices: { title: "设备管理", meta: "本地调试连接设备" },
  config: { title: "测试配置", meta: "新建稳定性测试" },
  runs: { title: "运行监控", meta: "后台测试进程" },
  reports: { title: "测试报告", meta: "本地报告归档" },
};

export const statusMeta: Record<string, { label: string; className: string }> = {
  running: { label: "运行中", className: "status-running" },
  awaiting_install: { label: "待安装", className: "status-install" },
  preparing: { label: "准备中", className: "status-preparing" },
  installing: { label: "安装中", className: "status-preparing" },
  stopping: { label: "停止中", className: "status-preparing" },
  ready: { label: "就绪", className: "status-ready" },
  offline: { label: "离线", className: "status-offline" },
  unauthorized: { label: "未授权", className: "status-stopped" },
  not_ready: { label: "未就绪", className: "status-preparing" },
};

Object.assign(statusMeta, {
  completed: { label: "已完成", className: "status-ready" },
  completed_with_crashes: { label: "完成（有崩溃）", className: "status-stopped" },
  completed_with_errors: { label: "完成（有错误）", className: "status-stopped" },
  completed_with_gaps: { label: "完成（有缺口）", className: "status-stopped" },
  passed: { label: "已完成", className: "status-ready" },
  failed: { label: "失败", className: "status-stopped" },
  recovering: { label: "恢复中", className: "status-preparing" },
  waiting_for_device: { label: "等待设备回连", className: "status-offline" },
  resuming: { label: "继续运行中", className: "status-preparing" },
  paused: { label: "已暂停", className: "status-stopped" },
  needs_manual_resume: { label: "需要手动继续", className: "status-stopped" },
  stopped: { label: "已停止", className: "status-stopped" },
});

export function phaseLabel(run: Run): string {
  if (run.status === "running" && run.evidence?.pressureStarted) return "智能压测中";
  if (run.status === "running") return "正在确认压测运行证据";
  if (run.status === "awaiting_install") return "等待人工安装确认";
  if (run.status === "installing") return "安装包安装与场景准备中";
  if (run.status === "preparing" || run.status === "queued") return "启动前准备中";
  if (run.status === "stopping") return "停止中";
  if (run.status === "recovering") return "正在恢复设备任务";
  if (run.status === "waiting_for_device") return "等待设备重新连接";
  if (run.status === "resuming") return "正在继续压力测试";
  if (run.status === "paused") return "设备任务已暂停，可手动继续";
  if (run.status === "needs_manual_resume") return "需要手动继续运行";
  return runStatusLabel(run.status);
}

export const runStatusLabels: Record<string, string> = {
  queued: "排队中", awaiting_install: "等待安装确认", preparing: "准备中",
  installing: "安装中", running: "运行中", stopping: "停止中",
  completed: "已完成", stopped: "已停止", failed: "失败", unknown: "状态未知",
};
Object.assign(runStatusLabels, {
  recovering: "恢复中",
  waiting_for_device: "等待设备回连",
  resuming: "继续运行中",
  paused: "已暂停",
  needs_manual_resume: "需要手动继续",
});
export const reportStatusLabels: Record<string, string> = {
  running: "测试中", passed: "通过", failed: "失败", stopped: "已停止",
  incomplete: "未完成", pending: "等待中",
};
Object.assign(reportStatusLabels, {
  recovering: "恢复中",
  waiting_for_device: "等待设备回连",
  resuming: "继续运行中",
  paused: "已暂停",
  needs_manual_resume: "需要手动继续",
});
export function runStatusLabel(status: Run["status"]): string { return runStatusLabels[status] || "状态未知"; }
export function reportStatusLabel(status: string | null | undefined): string {
  return reportStatusLabels[String(status || "").toLowerCase()] || "状态未知";
}
