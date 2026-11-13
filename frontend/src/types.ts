export type View = "overview" | "devices" | "config" | "runs" | "reports";

export type DeviceStatus =
  | "running"
  | "completed"
  | "completed_with_crashes"
  | "completed_with_errors"
  | "passed"
  | "failed"
  | "recovering"
  | "waiting_for_device"
  | "resuming"
  | "paused"
  | "needs_manual_resume"
  | "stopped"
  | "awaiting_install"
  | "preparing"
  | "installing"
  | "stopping"
  | "ready"
  | "offline"
  | "unauthorized"
  | "not_ready";

export interface RunDeviceProgress {
  status: DeviceStatus;
  progress: number;
  cycle: number;
  elapsedSeconds: number;
  remainingSeconds: number | null;
  operations: number;
  resumeAttempts: number;
  lastError: string | null;
}

export interface Device {
  serial: string;
  alias: string;
  model: string;
  brand: string;
  android: string;
  sdk: string;
  screenSize: string;
  battery: number | null;
  status: DeviceStatus;
  adbState: string;
  ready: boolean;
  progress: number;
  cycle: number;
  phase?: number;
  elapsed: string;
  elapsedSeconds?: number;
  remainingSeconds?: number;
  currentCycle?: Record<string, unknown> | null;
  resumeState?: string | null;
  resumeAttempts?: number;
  operations?: number;
  fastbotAlive?: boolean | null;
  fastbotStartedAt?: string | null;
  lastOperationAt?: string | null;
  operationStalled?: boolean;
  stalledForSeconds?: number | null;
  lastError?: string | null;
  lastHeartbeat?: string | null;
  waitingForDevice?: boolean;
  canResume?: boolean;
  canEnd?: boolean;
  manualEnd?: boolean;
  jobId: string | null;
  reportStatus: string | null;
}

export interface TestConfig {
  configFile: string;
  workflowMode: "matched" | "generic";
  packageName: string;
  apkPackageName: string;
  apkName: string;
  apkPath: string;
  duration: number;
  cycles: number;
  throttle: number;
  workers: number;
  screenshot: boolean;
  stallWatchdog: boolean;
  stallMode: "observe" | "recover_once";
  clearAppData: boolean;
  stopOnCrash: boolean;
  fastbotClasspath: string;
  fastbotMd5: string;
  fastbotExtraArgs: string[];
  scenarioTarget: string;
  navigationSteps: number;
  startupPage: string;
  auditGateEveryLaunch: boolean;
}

export interface ScenarioStartPage {
  id: "audit_gate" | "app_home";
  label: string;
  description: string;
}

export type RunStatus = "queued" | "awaiting_install" | "preparing" | "installing" | "running" | "recovering" | "waiting_for_device" | "resuming" | "paused" | "needs_manual_resume" | "stopping" | "completed" | "stopped" | "failed" | "unknown";

export interface InstallationState {
  required: boolean;
  status: "waiting" | "confirmed" | "not_required" | "cancelled";
  pendingDevices: string[];
  confirmedAt: string | null;
  devices: Record<string, {
    serial: string;
    required: boolean;
    installed: boolean;
    status: "waiting" | "already_installed" | "confirmed";
  }>;
}

export interface RunEvidence {
  phase: string;
  pressureStarted: boolean;
  errors: string[];
  installation?: InstallationState;
  reports: Record<string, {
    serial: string;
    preflight: boolean;
    fastbotStarted: boolean;
    cycles: number;
    operations: number;
    status: string;
    errors: string[];
  }>;
}

export interface Run {
  id: string;
  status: RunStatus;
  startedAt: string;
  finishedAt: string | null;
  packageName: string;
  apkName: string;
  devices: string[];
  duration: number;
  cycles: number;
  workers: number;
  pid: number | null;
  exitCode: number | null;
  elapsed: string;
  log?: string;
  reports: Report[];
  deviceProgress?: Record<string, RunDeviceProgress>;
  phase?: string;
  resumeState?: string | null;
  resumeAttempts?: number;
  remainingSeconds?: number;
  lastHeartbeat?: string | null;
  fastbotAlive?: boolean | null;
  fastbotStartedAt?: string | null;
  lastOperationAt?: string | null;
  operationStalled?: boolean;
  stalledForSeconds?: number | null;
  evidence?: RunEvidence;
  installation?: InstallationState;
}

export interface ConfigTemplate {
  name: string;
  packageName: string;
  duration: number;
  cycles: number;
  throttle: number;
  workers: number;
  screenshot: boolean;
  stallWatchdog: boolean;
  stallMode: "observe" | "recover_once" | "recover";
  clearAppData: boolean;
  stopOnCrash: boolean;
  classpath: string;
  expectedMd5: string;
  extraArgs: string[];
  scenarioTarget: string;
  navigationSteps: number;
  startupPages: ScenarioStartPage[];
}

export interface Draft {
  selectedDevices: string[];
  config: Partial<TestConfig>;
  savedAt?: string;
}

export interface PreflightResult {
  success: boolean;
  engine: "fastbot";
  config: { file: string; packageName: string; classpath: string; expectedMd5?: string; expectedNativeMd5?: string };
  checks: Array<Record<string, unknown>>;
}

export type TestReportStatus = "running" | "passed" | "failed" | "stopped" | "paused" | "needs_manual_resume" | "recovering" | "waiting_for_device" | "resuming" | "incomplete";

export interface CycleResult {
  cycle: number;
  status: string;
  pressureElapsedSeconds: number;
  operations: number;
  crashSignatures: string[];
  errors: string[];
  warnings: string[];
  stopReasons: string[];
}

export interface IssueEvidence {
  id: string;
  type: string;
  severity: string;
  title: string;
  description: string;
  cycle: number;
  phase: number;
  detectedAt: string | null;
  component: string;
  keyLog: string;
  suggestion: string;
  recoveryError: string;
  recoverySuccess: boolean;
  recoveryAction: string;
  deviceModel: string;
}

export interface ReplayAttempt {
  attempt?: number;
  restored?: boolean;
  replayed?: boolean;
  reproducible?: boolean;
  id?: string;
  status?: "reproduced" | "not_reproduced" | "failed";
  startedAt?: string;
  finishedAt?: string;
  sameCrash?: boolean;
  executed?: boolean;
  eventCount?: number;
  skippedEventCount?: number;
  foregroundRecoveryCount?: number;
  failedEventId?: string | number | null;
  transport?: string;
  error?: string;
  startMode?: "auto" | "current" | string;
  trajectoryPath?: string;
  replayPlan?: ReplayPlan;
  replayStart?: ReplayStart;
}

export interface ReplayAnchor {
  activity: string;
  component: string;
  source?: string;
  confidence?: string;
  observedAt?: string | null;
  eventId?: string | number | null;
}

export interface ReplayPlan {
  version?: number;
  strategy?: string;
  requestedWindowSeconds?: number | null;
  actualWindowSeconds?: number | null;
  maxEvents?: number | null;
  originalEventCount?: number;
  selectedEventCount?: number;
  omittedEventCount?: number;
  selectedByTime?: boolean;
  crashAt?: string | null;
  crashTimestampAlignment?: string | null;
  crashCutoffApplied?: boolean;
  startAt?: string | null;
  endAt?: string | null;
  requirePageAnchor?: boolean;
  allowColdActivityStart?: boolean;
  pageAnchor?: ReplayAnchor | null;
}

export interface ReplayStart {
  success?: boolean;
  mode?: "auto" | "current" | string;
  strategy?: string;
  anchorComponent?: string;
  initialComponent?: string;
  finalComponent?: string;
  taskPreserved?: boolean;
  activityMatched?: boolean;
  pageStateVerified?: boolean;
  reason?: string;
  error?: string;
}

export interface CrashEvidence {
  index: number;
  cycle: number;
  phase: number;
  detectedAt: string | null;
  exceptionType: string;
  message: string;
  location: string;
  source: string;
  signature: string;
  occurrenceCount: number;
  occurrences: Array<Record<string, unknown>>;
  crashLog: string;
  copiedInfo: string;
  pageComponent: string;
  pageType: "app_crash_page" | "runtime_page" | "unknown" | string;
  pageCapturedAt: string | null;
  copyStatus: string;
  copyNote: string;
  screenshotArtifact: string | null;
  reproducible: boolean;
  replay: {
    available: boolean;
    reason: string;
    eventCount: number;
    originalEventCount?: number;
    selectedEventCount?: number;
    omittedEventCount?: number;
    windowSeconds?: number | null;
    actualWindowSeconds?: number | null;
    pageAnchor?: ReplayAnchor | null;
    replayPlan?: ReplayPlan;
    eventSource?: string;
    note?: string;
    automaticAttempts: ReplayAttempt[];
    manualAttempts: ReplayAttempt[];
  };
}

export interface Report {
  id: string;
  testId: string;
  runId: string;
  app: string;
  packageName: string;
  device: string;
  manufacturer: string;
  model: string;
  androidVersion: string;
  sdk: string;
  screenSize: string;
  deviceSerial: string;
  startedAt: string | null;
  finishedAt: string | null;
  durationSeconds: number;
  pressureTargetSeconds: number;
  pressurePercent: number;
  cycles: number;
  expectedCycles: number;
  passedCycles: number;
  failedCycles: number;
  interruptedCycles: number;
  operations: number;
  crashes: number;
  stalls: number;
  fallbackRecoveries: number;
  stallRecoveryFailures: number;
  issues: number;
  status: "running" | "passed" | "failed" | "stopped" | "paused" | "needs_manual_resume" | "recovering" | "waiting_for_device" | "resuming";
  rawStatus: string;
  resumeState?: string | null;
  resumeAttempts?: number;
  remainingSeconds?: number;
  lastHeartbeat?: string | null;
  fastbotAlive?: boolean | null;
  operationStalled?: boolean;
  currentCycle?: Record<string, unknown> | null;
  lastError?: string | null;
  permissionFailureReason?: string | null;
  artifacts: string[];
  reportAvailable?: boolean;
  checkpointAvailable?: boolean;
  checkpointPath?: string;
  foregroundComponent?: string | null;
  fastbotPid?: string | null;
  remoteLogPath?: string | null;
  remoteLogOffset?: number;
  apkFingerprint?: string | null;
  canResume?: boolean;
  canEnd?: boolean;
  manualEnd?: boolean;
  cycleResults?: CycleResult[];
  cyclesDetail?: Array<Record<string, unknown>>;
  crashDetail?: CrashEvidence[];
  issueDetail?: IssueEvidence[];
  recoveryDetail?: IssueEvidence[];
  collectionWarnings?: string[];
  errors?: string[];
}

export interface TestReport {
  id: string;
  testId: string;
  runId: string;
  app: string;
  apkName: string;
  packageName: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: TestReportStatus;
  rawStatus: string;
  deviceCount: number;
  reportedDevices: number;
  passedDevices: number;
  failedDevices: number;
  runningDevices: number;
  stoppedDevices: number;
  pressureElapsedSeconds: number;
  pressureTargetSeconds: number;
  pressurePercent: number;
  recordedCycles: number;
  expectedCycles: number;
  operations: number;
  crashes: number;
  stalls: number;
  fallbackRecoveries: number;
  stallRecoveryFailures: number;
  issues: number;
  devices: Report[];
  errors?: string[];
  resumeState?: string | null;
  resumeAttempts?: number;
  remainingSeconds?: number;
  lastHeartbeat?: string | null;
  manualEnd?: boolean;
}

export interface ReplayResult {
  id: string;
  reportId: string;
  crashIndex: number;
  crashSignature: string;
  deviceSerial: string;
  startedAt: string;
  finishedAt: string;
  status: "reproduced" | "not_reproduced" | "failed";
  executed: boolean;
  eventCount: number;
  expectedSignature: string;
  actualSignature: string;
  sameCrash: boolean;
  error: string;
  failedEventId?: string | number | null;
  skippedEventCount?: number;
  foregroundRecoveryCount?: number;
  skippedEvents?: Array<{ event_id?: string | number | null; reason?: string }>;
  transport?: string;
  elapsedSeconds?: number;
  startMode?: "auto" | "current" | string;
  replayPlan?: ReplayPlan;
  replayStart?: ReplayStart;
  trajectoryPath?: string;
  failedEvent?: Record<string, unknown> | null;
}

export interface ReplayProgress {
  status: "idle" | "preparing" | "running" | "completed" | "failed" | string;
  executed: number;
  total: number;
  skipped: number;
  updatedAt?: string;
  message?: string;
  error?: string;
  sameCrash?: boolean;
  foregroundRecoveryCount?: number;
  foregroundComponent?: string;
  startMode?: "auto" | "current" | string;
  replayStart?: ReplayStart;
  replayPlan?: ReplayPlan;
}

export interface ApkUpload {
  name: string;
  path: string;
  size: number;
  sha256: string;
  packageName: string;
}
