import type { ConfigTemplate, Device, PreflightResult, Run, TestConfig } from "../types";

export const MAX_SAFE_WORKERS = 9;

export function validRunParameters(config: Pick<TestConfig, "duration" | "cycles" | "throttle" | "workers">): boolean {
  return Number.isInteger(config.duration) && config.duration >= 1 && config.duration <= 1440
    && Number.isInteger(config.cycles) && config.cycles >= 1 && config.cycles <= 100
    && Number.isInteger(config.throttle) && config.throttle >= 0 && config.throttle <= 60000
    && Number.isInteger(config.workers) && config.workers >= 1 && config.workers <= MAX_SAFE_WORKERS;
}

export function isActiveRun(run: Run): boolean {
  return ["queued", "awaiting_install", "preparing", "installing", "running", "recovering", "waiting_for_device", "resuming", "paused", "needs_manual_resume", "stopping"].includes(run.status);
}

export function taskDevicesForRun(run: Run, devices: Device[]): Device[] {
  return run.devices.map((serial) => {
    const live = devices.find((device) => device.serial === serial);
    const snapshot = run.deviceProgress?.[serial];
    const report = run.reports?.find((item) => item.deviceSerial === serial);
    const taskStatus = snapshot?.status ?? report?.status;
    const terminal = ["completed", "completed_with_crashes", "completed_with_errors", "passed", "failed", "stopped"].includes(taskStatus || "");
    const owned = !terminal && live?.jobId === run.id;
    const base: Device = live || {
      serial, alias: serial, model: "Android", brand: "Android", android: "--", sdk: "--",
      screenSize: "--", battery: null, status: "offline", adbState: "offline",
      ready: false, progress: 0, cycle: 0, elapsed: "--:--", jobId: null, reportStatus: null,
    };
    const status = owned ? base.status : taskStatus ?? "preparing";
    const reportProgress = report ? Number(report.pressurePercent) || 0 : 0;
    const elapsedSeconds = snapshot?.elapsedSeconds ?? (owned ? base.elapsedSeconds : 0);
    return {
      ...base,
      jobId: owned ? run.id : null,
      canResume: owned && base.canResume,
      canEnd: owned && base.canEnd,
      status,
      progress: snapshot?.progress ?? (owned ? base.progress : Math.floor(reportProgress)),
      cycle: snapshot?.cycle ?? (owned ? base.cycle : report?.cycles || 0),
      elapsedSeconds,
      elapsed: elapsedSeconds == null ? "--:--" : new Date(Math.max(0, elapsedSeconds) * 1000).toISOString().slice(11, 19),
      remainingSeconds: snapshot?.remainingSeconds ?? (owned ? base.remainingSeconds : undefined),
      operations: snapshot?.operations ?? (owned ? base.operations : report?.operations),
      resumeAttempts: snapshot?.resumeAttempts ?? (owned ? base.resumeAttempts : 0),
      lastError: report?.permissionFailureReason ?? snapshot?.lastError ?? (owned ? base.lastError : null),
      reportStatus: report?.rawStatus ?? (owned ? base.reportStatus : null),
      waitingForDevice: status === "waiting_for_device",
      fastbotAlive: terminal ? undefined : owned ? base.fastbotAlive : report?.fastbotAlive,
      operationStalled: terminal ? false : owned ? base.operationStalled : report?.operationStalled,
    };
  });
}

export function runProgress(run: Run, devices: Device[]): number {
  const values = taskDevicesForRun(run, devices).map((device) => Number(device.progress) || 0);
  if (!values.length) return 0;
  return Math.round(values.reduce((sum, value) => sum + Math.max(0, Math.min(100, value)), 0) / values.length);
}

export function configFromTemplate(template: ConfigTemplate, current: TestConfig, preserveUploadedPackage = false): TestConfig {
  const generic = preserveUploadedPackage && Boolean(current.apkPackageName) && current.apkPackageName !== template.packageName;
  return {
    ...current,
    configFile: template.name,
    workflowMode: generic ? "generic" : "matched",
    packageName: generic ? current.apkPackageName : template.packageName,
    duration: template.duration,
    cycles: template.cycles,
    throttle: template.throttle,
    workers: template.workers,
    screenshot: template.screenshot,
    stallWatchdog: generic ? current.stallWatchdog : template.stallWatchdog,
    stallMode: generic
      ? current.stallMode
      : template.stallMode === "recover" ? "recover_once" : template.stallMode,
    clearAppData: template.clearAppData,
    stopOnCrash: template.stopOnCrash,
    fastbotClasspath: template.classpath,
    fastbotMd5: template.expectedMd5,
    fastbotExtraArgs: template.extraArgs,
    scenarioTarget: generic ? "应用首页（通用模式）" : template.scenarioTarget,
    navigationSteps: generic ? 3 : template.navigationSteps,
    startupPage: generic ? "audit_gate" : template.startupPages[0]?.id || "",
  };
}

export function preflightFailureDetails(result: PreflightResult): string[] {
  const details: string[] = [];
  for (const check of result.checks) {
    if (check.name === "devices" && Array.isArray(check.devices)) {
      for (const device of check.devices as Array<Record<string, unknown>>) {
        if (!device.ok) {
          const fastbot = device.fastbot as Record<string, unknown> | null;
          details.push(`${String(device.serial || "设备")}: ${String(device.error || fastbot?.error || "设备预检失败")}`);
        }
      }
    } else if (!check.ok) {
      details.push(String(check.detail || check.name || "检查失败"));
    }
  }
  return details;
}
