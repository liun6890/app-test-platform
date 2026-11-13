import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BatteryMedium,
  Bell,
  Bug,
  Camera,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleStop,
  Clock3,
  CloudOff,
  Copy,
  Download,
  ExternalLink,
  FileArchive,
  FileJson,
  Gauge,
  HardDrive,
  Info,
  LayoutDashboard,
  ListFilter,
  LoaderCircle,
  Menu,
  MonitorSmartphone,
  MoreHorizontal,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  Smartphone,
  TerminalSquare,
  Trash2,
  Upload,
  Wifi,
  WifiOff,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import { ApiError, api } from "./api";
import { formatDate, formatDuration, formatMinutes, localizeRuntimeText, messageOf, serviceIsReachable } from "./lib/format";
import { navItems, pageTitles, phaseLabel, reportStatusLabel, runStatusLabel, statusMeta } from "./lib/labels";
import { MAX_SAFE_WORKERS, configFromTemplate, isActiveRun, preflightFailureDetails, runProgress, taskDevicesForRun, validRunParameters } from "./lib/runs";
import { EmptyState, PageLoading, ServiceBanner, StatusBadge, Toggle } from "./components/common";
import { ConfigPage, DevicesPage, Overview, ReportStatus, ReportsPage, artifactLabels, copyToClipboard } from "./pages/pages";
import { cycleStatusLabel, deviceDiagnosis, evidenceExcerpt, failedCycleStatuses, issueDisplay, runningDeviceCount, stopReasonLabel, testDiagnosis } from "./lib/diagnosis";
import type { Diagnosis, DiagnosisTone } from "./lib/diagnosis";
import { TestReportDialog } from "./pages/report-detail";
import { RunsPage } from "./pages/runs";
import type { ConfigTemplate, CrashEvidence, Device, DeviceStatus, Draft, IssueEvidence, PreflightResult, Report, ReplayProgress, Run, TestConfig, TestReport, View } from "./types";

const initialConfig: TestConfig = {
  configFile: "settings.json",
  workflowMode: "generic",
  packageName: "",
  apkPackageName: "",
  apkName: "",
  apkPath: "",
  duration: 10,
  cycles: 1,
  throttle: 250,
  workers: 1,
  screenshot: true,
  stallWatchdog: true,
  stallMode: "recover_once",
  clearAppData: true,
  stopOnCrash: false,
  fastbotClasspath: "",
  fastbotMd5: "",
  fastbotExtraArgs: [],
  scenarioTarget: "",
  navigationSteps: 0,
  startupPage: "",
  auditGateEveryLaunch: true,
};

const DRAFT_CACHE_KEY = "fastbot-console-draft-v2";
type ToastTone = "success" | "error" | "info";
type ReplayDisplay = { text: string; tone: ToastTone; startMode?: string; strategy?: string };

function readCachedDraft(): Draft | null {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(DRAFT_CACHE_KEY) || "null");
    if (!parsed || typeof parsed !== "object") return null;
    const value = parsed as { selectedDevices?: unknown; config?: unknown };
    if (!Array.isArray(value.selectedDevices) || !value.config || typeof value.config !== "object") return null;
    return {
      selectedDevices: [...new Set(value.selectedDevices.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim()))],
      config: value.config as Partial<TestConfig>,
    };
  } catch {
    return null;
  }
}

function cacheDraft(draft: Pick<Draft, "selectedDevices" | "config">) {
  try {
    window.localStorage.setItem(DRAFT_CACHE_KEY, JSON.stringify(draft));
  } catch {
    // Browser storage is only a local fallback; the server draft remains authoritative.
  }
}

function App() {
  const [view, setView] = useState<View>("overview");
  const [mobileNav, setMobileNav] = useState(false);
  const [connected, setConnected] = useState(false);
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [testReports, setTestReports] = useState<TestReport[]>([]);
  const [selectedDevices, setSelectedDevices] = useState<string[]>([]);
  const [config, setConfig] = useState<TestConfig>(initialConfig);
  const [configTemplates, setConfigTemplates] = useState<ConfigTemplate[]>([]);
  const [preflight, setPreflight] = useState<PreflightResult | null>(null);
  const [draftReady, setDraftReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [toast, setToast] = useState<{ tone: ToastTone; text: string } | null>(null);
  const [reportQuery, setReportQuery] = useState("");
  const [reportStatus, setReportStatus] = useState("all");
  const [activeTestReport, setActiveTestReport] = useState<TestReport | null>(null);
  const [deletingReportId, setDeletingReportId] = useState<string | null>(null);
  const pollingRef = useRef(false);
  // Bumped whenever an operator action changes server state. An older poll that
  // is still in flight compares this value and discards its stale response, so
  // clicking "stop" cannot be overwritten by a poll that started before it.
  const dataGenerationRef = useRef(0);
  const draftTimerRef = useRef<number | null>(null);
  const selectedDevicesRef = useRef<string[]>([]);
  const configRef = useRef<TestConfig>(initialConfig);
  const bootstrapGenerationRef = useRef(0);

  const showToast = useCallback((text: string, tone: ToastTone = "success") => {
    setToast({ text, tone });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const refreshData = useCallback(async (withReports = false, manual = false) => {
    if (pollingRef.current) return;
    const generation = dataGenerationRef.current;
    pollingRef.current = true;
    if (manual) setRefreshing(true);
    try {
      const [deviceResult, runResult, reportResult, testReportResult] = await Promise.all([
        api.devices(),
        api.runs(true),
        withReports ? api.reports() : Promise.resolve(null),
        withReports ? api.testReports() : Promise.resolve(null),
      ]);
      if (dataGenerationRef.current !== generation) {
        // A stop/start/resume landed while this poll was in flight; its data is
        // already stale and must not overwrite the newer state.
        return;
      }
      setDevices(deviceResult.devices);
      setRuns(runResult.runs);
      const activeDeviceSerials = new Set(
        runResult.runs.filter(isActiveRun).flatMap((run) => run.devices),
      );
      if (selectedDevicesRef.current.some((serial) => activeDeviceSerials.has(serial))) {
        setPreflight(null);
      }
      if (reportResult) setReports(reportResult.reports);
      if (testReportResult) setTestReports(testReportResult.tests);
      setConnected(true);
      setServiceError(null);
      if (manual) showToast("本地状态已刷新");
    } catch (error) {
      setConnected(serviceIsReachable(error));
      setServiceError(messageOf(error));
      if (manual) showToast(messageOf(error), "error");
    } finally {
      pollingRef.current = false;
      setRefreshing(false);
      setLoading(false);
    }
  }, [showToast]);

  const invalidateData = useCallback(() => {
    // Any completed mutation makes every in-flight poll response obsolete.
    dataGenerationRef.current += 1;
  }, []);

  const persistDraft = useCallback((nextDevices: string[], nextConfig: TestConfig, immediately = false) => {
    const snapshot = { selectedDevices: nextDevices, config: nextConfig };
    cacheDraft(snapshot);
    if (!draftReady) return;
    if (draftTimerRef.current != null) window.clearTimeout(draftTimerRef.current);
    const save = () => {
      void api.saveDraft(snapshot).catch((error: unknown) => setServiceError(messageOf(error)));
    };
    if (immediately) {
      save();
      return;
    }
    draftTimerRef.current = window.setTimeout(save, 250);
  }, [draftReady]);

  function applyConfig(nextConfig: TestConfig, immediately = false) {
    const normalizedConfig = {
      ...nextConfig,
      workers: Math.min(MAX_SAFE_WORKERS, Math.max(1, Number(nextConfig.workers) || 1)),
    };
    configRef.current = normalizedConfig;
    setConfig(normalizedConfig);
    setPreflight(null);
    persistDraft(selectedDevicesRef.current, normalizedConfig, immediately);
  }

  function applySelectedDevices(nextDevices: string[], immediately = false) {
    const uniqueDevices = [...new Set(nextDevices)];
    selectedDevicesRef.current = uniqueDevices;
    setSelectedDevices(uniqueDevices);
    setPreflight(null);
    persistDraft(uniqueDevices, configRef.current, immediately);
  }

  const loadInitialState = useCallback(async () => {
    const generation = ++bootstrapGenerationRef.current;
    setLoading(true);
    try {
      const [health, defaults, templates, serverDraft] = await Promise.all([api.health(), api.config(), api.configs(), api.draft()]);
      if (generation !== bootstrapGenerationRef.current) return;
      const cachedDraft = readCachedDraft();
      const draft: Draft = {
        selectedDevices: cachedDraft?.selectedDevices || serverDraft.selectedDevices || [],
        config: { ...(serverDraft.config || {}), ...(cachedDraft?.config || {}) },
      };
      setConnected(health.ok && health.engine === "fastbot");
      setConfigTemplates(templates.configs);
      const defaultTemplate = templates.configs.find((item) => item.name === defaults.configFile)
        || templates.configs.find((item) => item.packageName === defaults.packageName)
        || templates.configs[0];
      const draftConfig = draft.config || {};
      const selectedTemplate = templates.configs.find((item) => item.name === draftConfig.configFile)
        || defaultTemplate;
      const bootConfig = { ...initialConfig, ...defaults, ...draftConfig, apkName: "", apkPath: "" };
      const hasUploadedApk = Boolean(draftConfig.apkPath && draftConfig.apkPackageName);
      const useGenericFlow = draftConfig.workflowMode === "generic"
        || Boolean(hasUploadedApk && selectedTemplate && draftConfig.apkPackageName !== selectedTemplate.packageName);
      const templateConfig = selectedTemplate
        ? configFromTemplate(selectedTemplate, { ...bootConfig, ...draftConfig }, useGenericFlow)
        : bootConfig;
      const validStartPages = useGenericFlow
        ? ["audit_gate", "app_home"]
        : (selectedTemplate?.startupPages || []).map((item) => item.id);
      const restoredConfig: TestConfig = {
        ...templateConfig,
        ...draftConfig,
        configFile: templateConfig.configFile,
        workflowMode: templateConfig.workflowMode,
        packageName: hasUploadedApk ? String(draftConfig.apkPackageName) : templateConfig.packageName,
        fastbotClasspath: templateConfig.fastbotClasspath,
        fastbotMd5: templateConfig.fastbotMd5,
        fastbotExtraArgs: templateConfig.fastbotExtraArgs,
        scenarioTarget: templateConfig.scenarioTarget,
        navigationSteps: templateConfig.navigationSteps,
        startupPage: useGenericFlow
          ? validStartPages.includes(String(draftConfig.startupPage))
          ? String(draftConfig.startupPage)
          : "audit_gate"
          : validStartPages.includes(String(draftConfig.startupPage))
          ? String(draftConfig.startupPage)
          : templateConfig.startupPage,
      };
      // A generic uploaded APK must start with the shared stall recovery
      // guard even when an older template or draft stored it as disabled.
      // The monitor toggle remains available for an explicit operator
      // change after this initial normalization.
      if (useGenericFlow && !Object.prototype.hasOwnProperty.call(draftConfig, "stallWatchdog")) {
        restoredConfig.stallWatchdog = true;
        restoredConfig.stallMode = "recover_once";
      }
      const restoredDevices = Array.isArray(draft.selectedDevices) ? draft.selectedDevices : [];
      configRef.current = restoredConfig;
      selectedDevicesRef.current = restoredDevices;
      setConfig(restoredConfig);
      setSelectedDevices(restoredDevices);
      setDraftReady(true);
      await refreshData(true);
    } catch (error) {
      if (generation !== bootstrapGenerationRef.current) return;
      setConnected(serviceIsReachable(error));
      setServiceError(messageOf(error));
      setLoading(false);
    }
  }, [refreshData]);

  useEffect(() => {
    void loadInitialState();
    return () => {
      bootstrapGenerationRef.current += 1;
    };
  }, [loadInitialState]);

  // A run that is starting/stopping still changes state every moment, so the
  // fast cadence covers the whole non-terminal lifecycle, not just "running".
  const pollingBusy = useMemo(
    () =>
      runs.some(isActiveRun) ||
      devices.some((device) =>
        ["running", "preparing", "installing", "stopping", "recovering", "waiting_for_device", "resuming"].includes(device.status),
      ),
    [runs, devices],
  );

  useEffect(() => {
    // Polling paused while the tab is hidden: a background console tab must not
    // keep the backend busy, and the next visible poll refreshes immediately.
    const tick = () => {
      if (document.hidden) return;
      void refreshData(view === "reports");
    };
    // 2.5s while work is in flight so progress/logs feel live; 8s when idle so an
    // open console does not keep the backend and ADB busy for nothing.
    const interval = pollingBusy ? 4000 : 10000;
    const timer = window.setInterval(tick, interval);
    const onVisible = () => {
      if (!document.hidden) void refreshData(view === "reports");
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refreshData, view, pollingBusy]);

  useEffect(() => () => {
    if (draftTimerRef.current != null) window.clearTimeout(draftTimerRef.current);
  }, []);

  const activeRuns = runs.filter(isActiveRun);
  const activeRun = activeRuns[0] || null;
  const onlineDevices = devices.filter((device) => device.adbState === "device");

  const filteredTestReports = useMemo(() => {
    const keyword = reportQuery.trim().toLowerCase();
    return testReports.filter((report) => {
      const devicesText = report.devices.map((device) => `${device.device} ${device.deviceSerial}`).join(" ");
      const matchesQuery = !keyword || `${report.testId} ${report.runId} ${report.app} ${report.packageName} ${devicesText}`.toLowerCase().includes(keyword);
      const matchesStatus = reportStatus === "all" || report.status === reportStatus;
      return matchesQuery && matchesStatus;
    });
  }, [testReports, reportQuery, reportStatus]);

  function navigate(next: View) {
    persistDraft(selectedDevicesRef.current, configRef.current, true);
    setView(next);
    setMobileNav(false);
    if (next === "reports") void refreshData(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function toggleDevice(serial: string) {
    const current = selectedDevicesRef.current;
    applySelectedDevices(current.includes(serial) ? current.filter((item) => item !== serial) : [...current, serial]);
  }

  function selectReadyDevices() {
    const readySerials = devices.filter((device) => device.ready && !device.jobId).map((device) => device.serial);
    applySelectedDevices([...selectedDevicesRef.current, ...readySerials]);
  }

  function clearDeviceSelection() {
    applySelectedDevices([]);
  }

  function configureSelectedDevices() {
    const nextConfig = {
      ...configRef.current,
      workers: Math.min(MAX_SAFE_WORKERS, Math.max(1, selectedDevicesRef.current.length)),
    };
    applyConfig(nextConfig, true);
    navigate("config");
  }

  async function syncFastbot() {
    if (!selectedDevices.length) return showToast("请先选择就绪设备", "error");
    setBusyAction("sync");
    try {
      const result = await api.syncFastbot(selectedDevices);
      if (!result.ok) throw new Error(result.output || "压测组件同步失败");
      showToast(`已向 ${selectedDevices.length} 台设备同步压测组件`);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function uploadApk(file: File) {
    setBusyAction("upload");
    try {
      const uploaded = await api.uploadApk(file);
      const matchingTemplate = configTemplates.find((item) => item.packageName === uploaded.packageName);
      const current = configRef.current;
      const template = matchingTemplate
        || configTemplates.find((item) => item.name === current.configFile)
        || configTemplates[0];
      const uploadedConfig = { ...current, apkPackageName: uploaded.packageName };
      const next = template ? configFromTemplate(template, uploadedConfig, !matchingTemplate) : uploadedConfig;
      applyConfig({ ...next, packageName: uploaded.packageName, workflowMode: matchingTemplate ? "matched" : "generic", apkName: uploaded.name, apkPath: uploaded.path, apkPackageName: uploaded.packageName }, true);
      showToast(matchingTemplate
        ? `${uploaded.name} 上传完成，已匹配项目流程：${matchingTemplate.name}`
        : `${uploaded.name} 上传完成，已启用新包通用智能压测模式`);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function startRun() {
    const runConfig = configRef.current;
    const runDevices = selectedDevicesRef.current;
    if (!validRunParameters(runConfig)) {
      return showToast("时长、轮数、事件间隔和并发设备必须填写有效整数", "error");
    }
    if (!runConfig.apkPath) return showToast("请先上传本地安装包", "error");
    if (!runConfig.packageName.trim()) return showToast("未读取到安装包包名，请重新上传安装包", "error");
    if (!runDevices.length) {
      navigate("devices");
      return showToast("请先选择就绪设备", "error");
    }
    const unavailable = runDevices.filter((serial) => {
      const device = devices.find((item) => item.serial === serial);
      return !device || !device.ready || Boolean(device.jobId);
    });
    if (unavailable.length) {
      navigate("devices");
      return showToast(`以下已选设备当前不可启动：${unavailable.join("、")}`, "error");
    }
    setBusyAction("start");
    try {
      persistDraft(runDevices, runConfig, true);
      const checked = await api.preflightRun(runConfig, runDevices);
      setPreflight(checked);
      if (!checked.success) throw new Error("启动前预检未通过，压测未启动");
      const run = await api.startRun(runConfig, runDevices);
      invalidateData();
      setPreflight(null);
      setRuns((current) => [run, ...current]);
      navigate("runs");
      showToast(run.status === "awaiting_install"
        ? `任务 ${run.id} 已提交，请先在设备上完成安装包安装`
        : `任务 ${run.id} 已提交，正在准备测试场景`);
      await refreshData(false);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function stopRun(run: Run) {
    const confirmed = window.confirm(
      `确认停止任务 ${run.id}？\n\n将中断该任务下所有设备当前的压测进度，已产生的报告会保留，但本轮不能继续。`,
    );
    if (!confirmed) return;
    setBusyAction(`stop-${run.id}`);
    try {
      const result = await api.stopRun(run.id);
      invalidateData();
      const failed = result.results.filter((item) => !item.stopped);
      showToast(
        failed.length
          ? `任务已停止；${failed.length} 台设备的远端进程未能二次确认，请检查设备连接`
          : "任务已停止",
        failed.length ? "info" : "success",
      );
      await refreshData(true);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function confirmInstall(run: Run) {
    setBusyAction(`confirm-install-${run.id}`);
    try {
      const next = await api.confirmInstall(run.id);
      setRuns((current) => current.map((item) => item.id === next.id ? next : item));
      showToast("安装结果已确认，框架继续执行清数据和场景准备");
      await refreshData(false);
    } catch (error) {
      await refreshData(false);
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function deleteTestReport(report: TestReport) {
    if (report.status === "running") {
      showToast("测试仍在运行，停止并完整收尾后才能删除报告", "error");
      return;
    }
    const firstConfirmed = window.confirm(
      `确认删除测试 ${report.testId} 的本次报告？\n\n将删除该测试在本机磁盘中的报告、日志、截图等产物，不会删除安装包或其他测试。`,
    );
    if (!firstConfirmed) return;
    const secondConfirmed = window.confirm(
      `再次确认：${report.testId} 的测试产物删除后无法恢复。确定继续？`,
    );
    if (!secondConfirmed) return;

    setDeletingReportId(report.id);
    try {
      try {
        await api.deleteTestReport(report.id);
        invalidateData();
      } catch (error) {
        // The report list can outlive a service restart or a legacy metadata
        // migration. Retry with the stable batch id when the old hash no
        // longer resolves, so a stale row can still be removed.
        if (!(error instanceof ApiError) || error.status !== 404 || !report.testId || report.testId === report.id) {
          throw error;
        }
        await api.deleteTestReport(report.testId);
      }
      setTestReports((current) => current.filter((item) => item.id !== report.id));
      setReports((current) => current.filter((item) => item.testId !== report.testId));
      setActiveTestReport((current) => current?.id === report.id ? null : current);
      showToast(`已删除测试报告 ${report.testId}`);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setDeletingReportId(null);
    }
  }

  async function stopDevice(serial: string) {
    const device = devices.find((item) => item.serial === serial);
    const confirmed = window.confirm(
      `确认暂停 ${device?.alias || serial} 的压测？\n\n该设备会立即停止输入并释放，可在需要时再"继续"；不会影响同一任务的其他设备。`,
    );
    if (!confirmed) return;
    setBusyAction(`stop-${serial}`);
    try {
      const result = await api.stopDevices([serial]);
      const item = result.results[0];
      if (!item?.stopped) throw new Error(item?.error || "设备压测暂停失败");
      invalidateData();
      showToast(`${serial} 的压测已暂停`);
      await refreshData(true);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  async function resumeDevice(jobId: string, serial: string) {
    const action = `resume-${serial}`;
    if (busyAction === action) return;
    setBusyAction(action);
    try {
      const next = await api.resumeJobDevice(jobId, serial);
      invalidateData();
      setRuns((current) => {
        const found = current.some((item) => item.id === next.id);
        return found ? current.map((item) => item.id === next.id ? next : item) : [next, ...current];
      });
      showToast(`${serial} 已请求继续运行`);
      await refreshData(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        showToast("该设备正在继续运行，请勿重复操作", "info");
      } else {
        showToast(messageOf(error), "error");
      }
    } finally {
      setBusyAction(null);
    }
  }

  async function endDevice(jobId: string, serial: string) {
    const action = `end-${serial}`;
    if (busyAction === action) return;
    const device = devices.find((item) => item.serial === serial);
    const confirmed = window.confirm(
      `确认手动结束 ${device?.alias || serial} 的当前测试？\n\n结束后不能再继续这台设备在旧任务中的剩余进度，但不会影响同一任务里的其他设备；该设备会立即释放，可用于下一个测试任务。`,
    );
    if (!confirmed) return;
    setBusyAction(action);
    try {
      const result = await api.endJobDevice(jobId, serial);
      invalidateData();
      if (!result.manualEnd && !result.ended) {
        showToast(`${serial} 已在请求期间完成，设备已释放`, "info");
      } else {
        showToast(`${serial} 的当前测试已手动结束，可用于新任务`);
      }
      await refreshData(true);
    } catch (error) {
      showToast(messageOf(error), "error");
    } finally {
      setBusyAction(null);
    }
  }

  const title = pageTitles[view];

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileNav ? "is-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark"><Zap size={20} strokeWidth={2.5} /></div>
          <div><strong>App 测试平台</strong><span>本机测试环境</span></div>
          <button className="icon-button sidebar-close" type="button" onClick={() => setMobileNav(false)} aria-label="关闭导航"><X size={19} /></button>
        </div>
        <nav className="main-nav" aria-label="主导航">
          <span className="nav-label">工作区</span>
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button type="button" className={`nav-item ${view === item.id ? "is-active" : ""}`} onClick={() => navigate(item.id)} key={item.id}>
                <Icon size={19} /><span>{item.label}</span>
                {item.id === "runs" && activeRuns.length > 0 && <b>{activeRuns.length}</b>}
              </button>
            );
          })}
        </nav>
        <div className="sidebar-system">
          <div className="system-line">
            <span className={connected ? "pulse-dot" : "pulse-dot is-offline"} />
            <div><strong>本地服务</strong><small>{connected ? "压测服务已连接" : "连接断开"}</small></div>
          </div>
          <span className="version">v1.0</span>
        </div>
      </aside>
      {mobileNav && <button className="nav-backdrop" type="button" onClick={() => setMobileNav(false)} aria-label="关闭导航" />}

      <main className="main-area">
        <header className="topbar">
          <button className="icon-button menu-button" type="button" onClick={() => setMobileNav(true)} aria-label="打开导航"><Menu size={21} /></button>
          <div className="page-heading"><h1>{title.title}</h1><span>{title.meta}</span></div>
          <div className="topbar-actions">
            <button className="icon-button notification-button" type="button" aria-label="服务状态" title={connected ? "本地服务正常" : "本地服务断开"}>{connected ? <Wifi size={18} /> : <WifiOff size={18} />}</button>
            <div className="operator"><span>压</span><div><strong>压测服务</strong><small>本机工作区</small></div><ChevronDown size={15} /></div>
          </div>
        </header>

        <div className="page-content">
          {serviceError && <ServiceBanner message={serviceError} connected={connected} onRetry={() => void loadInitialState()} refreshing={refreshing || loading} />}
          {loading ? <PageLoading /> : (
            <>
              {view === "overview" && <Overview devices={devices} runs={runs} reports={reports} onNavigate={navigate} onStop={stopDevice} onResume={resumeDevice} onEnd={endDevice} busyAction={busyAction} />}
              {view === "devices" && <DevicesPage devices={devices} selected={selectedDevices} refreshing={refreshing} busyAction={busyAction} onToggle={toggleDevice} onSelectReady={selectReadyDevices} onClear={clearDeviceSelection} onRefresh={() => void refreshData(true, true)} onSync={() => void syncFastbot()} onConfigure={configureSelectedDevices} onStop={(serial) => void stopDevice(serial)} onResume={resumeDevice} onEnd={endDevice} />}
              {view === "config" && <ConfigPage config={config} templates={configTemplates} devices={devices} selectedDevices={selectedDevices} preflight={preflight} setConfig={applyConfig} uploading={busyAction === "upload"} starting={busyAction === "start"} onUpload={uploadApk} onStart={() => void startRun()} onManageDevices={() => navigate("devices")} />}
              {view === "runs" && <RunsPage runs={runs} devices={devices} busyAction={busyAction} onStop={(run) => void stopRun(run)} onResume={resumeDevice} onEnd={endDevice} onConfirmInstall={(run) => void confirmInstall(run)} onNavigate={navigate} />}
              {view === "reports" && <ReportsPage items={filteredTestReports} query={reportQuery} status={reportStatus} onQuery={setReportQuery} onStatus={setReportStatus} onOpen={setActiveTestReport} onDelete={(report) => void deleteTestReport(report)} deletingReportId={deletingReportId} onRefresh={() => void refreshData(true, true)} refreshing={refreshing} />}
            </>
          )}
        </div>
      </main>

      {toast && <div className={`toast ${toast.tone === "error" ? "is-error" : toast.tone === "info" ? "is-info" : ""}`}>{toast.tone === "error" ? <XCircle size={18} /> : toast.tone === "info" ? <Info size={18} /> : <CheckCircle2 size={18} />}{toast.text}</div>}
      {activeTestReport && <TestReportDialog report={activeTestReport} onClose={() => setActiveTestReport(null)} onToast={showToast} onResume={resumeDevice} onEnd={endDevice} busyAction={busyAction} />}
    </div>
  );
}

export default App;
