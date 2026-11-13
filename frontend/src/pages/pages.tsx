import { useCallback, useMemo, useRef, useState } from "react";
import {
  Activity, AlertTriangle, BarChart3, BatteryMedium, Bug, Camera, Check, CheckCircle2,
  ChevronDown, ChevronRight, CircleStop, Clock3, CloudOff, Copy, Download, ExternalLink,
  FileArchive, FileJson, Gauge, HardDrive, Info, ListFilter, LoaderCircle, MonitorSmartphone,
  MoreHorizontal, Play, RefreshCw, RotateCcw, Search, Settings2, ShieldCheck, Smartphone,
  TerminalSquare, Trash2, Upload, Wifi, WifiOff, X, XCircle, Zap,
} from "lucide-react";
import { api } from "../api";
import { EmptyState, StatusBadge, Toggle } from "../components/common";
import { formatDate, formatDuration, formatMinutes, localizeRuntimeText, messageOf } from "../lib/format";
import { phaseLabel, runStatusLabel } from "../lib/labels";
import { runningDeviceCount, testDiagnosis } from "../lib/diagnosis";
import { MAX_SAFE_WORKERS, isActiveRun, runProgress, taskDevicesForRun, configFromTemplate, preflightFailureDetails, validRunParameters } from "../lib/runs";
import type {
  ConfigTemplate, Device, DeviceStatus, PreflightResult, Report, Run, TestConfig, TestReport, View,
} from "../types";

export function Overview({ devices, runs, reports, onNavigate, onStop, onResume, onEnd, busyAction }: { devices: Device[]; runs: Run[]; reports: Report[]; onNavigate: (view: View) => void; onStop: (serial: string) => void; onResume: (jobId: string, serial: string) => void; onEnd: (jobId: string, serial: string) => void; busyAction: string | null }) {
  const online = devices.filter((device) => device.adbState === "device").length;
  const active = runs.filter(isActiveRun);
  const activeRun = active[0] || null;
  const today = new Date().toDateString();
  const todayReports = reports.filter((report) => report.finishedAt && new Date(report.finishedAt).toDateString() === today);
  const crashes = todayReports.reduce((sum, report) => sum + report.crashes, 0);
  const seconds = todayReports.reduce((sum, report) => sum + report.durationSeconds, 0);
  const runDevices = activeRun ? taskDevicesForRun(activeRun, devices) : [];
  const logLines = (activeRun?.log || "").split(/\r?\n/).filter(Boolean).slice(-5).reverse();

  return (
    <>
      <section className="metric-strip" aria-label="运行摘要">
        <div className="metric"><span className="metric-icon mint"><Smartphone size={20} /></span><div><small>在线设备</small><strong>{online}<em> / {devices.length}</em></strong></div><span className="metric-trend">当前连接状态</span></div>
        <div className="metric"><span className="metric-icon amber"><Activity size={20} /></span><div><small>运行中的任务</small><strong>{active.length}</strong></div><span className="metric-trend">后台压测任务</span></div>
        <div className="metric"><span className="metric-icon coral"><AlertTriangle size={20} /></span><div><small>今日崩溃</small><strong>{crashes}</strong></div><span className={`metric-trend ${crashes ? "risk" : "good"}`}>{crashes ? "需要复核" : "未检测到崩溃"}</span></div>
        <div className="metric"><span className="metric-icon blue"><Clock3 size={20} /></span><div><small>今日压力时长</small><strong>{Math.round(seconds / 360) / 10}<em> 小时</em></strong></div><span className="metric-trend">{todayReports.length} 份设备报告</span></div>
      </section>

      <div className="content-grid overview-grid">
        <section className="panel run-panel">
          {activeRun ? (
            <>
              <div className="panel-heading"><div><span className="eyebrow"><span className="live-dot" />正在运行</span><h2>{activeRun.apkName || activeRun.packageName}</h2></div><button className="text-button" type="button" onClick={() => onNavigate("runs")}>查看详情</button></div>
              <div className="run-summary"><div><small>任务编号</small><strong>{activeRun.id}</strong></div><div><small>目标包名</small><strong>{activeRun.packageName}</strong></div><div><small>开始时间</small><strong>{formatDate(activeRun.startedAt, true)}</strong></div><div><small>测试策略</small><strong>智能压测 · {activeRun.cycles} 轮</strong></div></div>
              <div className="device-run-list">
                {runDevices.map((device) => <DeviceRunRow device={device} targetCycles={activeRun.cycles} busy={busyAction === `stop-${device.serial}`} resumeBusy={busyAction === `resume-${device.serial}`} endBusy={busyAction === `end-${device.serial}`} onStop={() => onStop(device.serial)} onResume={() => device.jobId && onResume(device.jobId, device.serial)} onEnd={() => device.jobId && onEnd(device.jobId, device.serial)} key={device.serial} />)}
                {!runDevices.length && <div className="inline-empty"><LoaderCircle className="spin" size={19} />正在等待设备任务状态</div>}
              </div>
            </>
          ) : <EmptyState icon={Gauge} title="当前没有运行中的任务" note="上传安装包并选择设备后即可启动压测" action={<button className="primary-button" type="button" onClick={() => onNavigate("config")}><Play size={16} />新建测试</button>} />}
        </section>

        <section className="panel activity-panel">
          <div className="panel-heading"><div><span className="eyebrow">实时输出</span><h2>任务日志</h2></div><TerminalSquare size={19} /></div>
          {activeRun ? <div className="activity-list real-log-list">{logLines.length ? logLines.map((line, index) => <div className="activity-item info" key={`${index}-${line}`}><span className="activity-marker" /><div><p>{line}</p></div></div>) : <div className="inline-empty"><LoaderCircle className="spin" size={18} />等待日志输出</div>}</div> : <div className="quiet-empty"><TerminalSquare size={24} /><span>没有活动日志</span></div>}
          <button className="wide-quiet-button" type="button" onClick={() => onNavigate("runs")}>打开完整日志</button>
        </section>
      </div>

      <section className="panel recent-panel">
        <div className="panel-heading"><div><span className="eyebrow">最近完成</span><h2>测试结果</h2></div><button className="text-button" type="button" onClick={() => onNavigate("reports")}>全部报告</button></div>
        {reports.length ? <ReportTable items={reports.slice(0, 4)} onOpen={() => onNavigate("reports")} /> : <EmptyState icon={FileJson} title="还没有测试报告" note="完成首个压测任务后会在这里显示" />}
      </section>
    </>
  );
}

export function DeviceRunRow({ device, targetCycles, busy, resumeBusy, endBusy, onStop, onResume, onEnd }: { device: Device; targetCycles: number; busy: boolean; resumeBusy: boolean; endBusy: boolean; onStop: () => void; onResume: () => void; onEnd: () => void }) {
  return (
    <div className="device-run-row">
      <div className="device-identity compact"><span className="device-avatar"><Smartphone size={19} /></span><div><strong>{device.alias}</strong><small>{device.model} · 安卓 {device.android}</small></div></div>
      <div className="progress-cell"><div><span>第 {device.cycle || 1}/{targetCycles} 轮</span><b>{device.progress}%</b></div><div className="progress-track"><span style={{ width: `${device.progress}%` }} /></div><div className="resume-inline-status"><span>{device.remainingSeconds != null ? `剩余 ${Math.ceil(device.remainingSeconds)} 秒` : device.elapsed}</span>{device.resumeAttempts ? <small>已恢复 {device.resumeAttempts} 次</small> : null}{device.lastError ? <small title={localizeRuntimeText(device.lastError)}>{localizeRuntimeText(device.lastError)}</small> : null}</div></div>
      <span className="elapsed"><Clock3 size={14} />{device.elapsed}</span>
      <div className="device-action-group">
        {device.canResume && device.jobId ? <button className="icon-button small resume-button" type="button" onClick={onResume} disabled={resumeBusy} aria-label={`继续运行 ${device.alias}`} title="继续运行设备任务">{resumeBusy ? <LoaderCircle size={17} className="spin" /> : <Play size={16} fill="currentColor" />}</button> : null}
        {device.canEnd && device.jobId ? <button className="icon-button small manual-end-button" type="button" onClick={onEnd} disabled={endBusy} aria-label={`手动结束 ${device.alias}`} title="手动结束本设备测试（结束后不可继续）">{endBusy ? <LoaderCircle size={17} className="spin" /> : <XCircle size={18} />}</button> : null}
        {device.jobId ? <button className="icon-button small danger-ghost" type="button" onClick={onStop} disabled={busy} aria-label={`暂停 ${device.alias}`} title="暂停设备任务（可继续）">{busy ? <LoaderCircle size={17} className="spin" /> : <CircleStop size={18} />}</button> : null}
      </div>
    </div>
  );
}

export function DevicesPage({ devices, selected, refreshing, busyAction, onToggle, onSelectReady, onClear, onRefresh, onSync, onConfigure, onStop, onResume, onEnd }: { devices: Device[]; selected: string[]; refreshing: boolean; busyAction: string | null; onToggle: (serial: string) => void; onSelectReady: () => void; onClear: () => void; onRefresh: () => void; onSync: () => void; onConfigure: () => void; onStop: (serial: string) => void; onResume: (jobId: string, serial: string) => void; onEnd: (jobId: string, serial: string) => void }) {
  const selectedSet = new Set(selected);
  const readyForRun = (device: Device) => device.ready && !device.jobId;
  const selectable = (device: Device) => readyForRun(device) || selectedSet.has(device.serial);
  const unavailable = selected.filter((serial) => {
    const device = devices.find((item) => item.serial === serial);
    return !device || !readyForRun(device);
  });
  const readyCount = devices.filter(readyForRun).length;
  return (
    <section className="panel page-panel">
      <div className="section-toolbar">
        <div className="selection-summary"><span>{selected.length}</span><div><strong>已选择设备</strong><small>只有已授权、已解锁设备可加入测试</small></div></div>
        <div className="toolbar-actions">
          <button className="secondary-button" type="button" onClick={onRefresh} disabled={refreshing}><RefreshCw size={17} className={refreshing ? "spin" : ""} />刷新</button>
          <button className="secondary-button" type="button" onClick={onSync} disabled={!selected.length || busyAction === "sync"}>{busyAction === "sync" ? <LoaderCircle size={17} className="spin" /> : <Upload size={17} />}同步压测组件</button>
          <button className="primary-button" type="button" onClick={onConfigure} disabled={!selected.length}><Settings2 size={17} />配置测试</button>
        </div>
      </div>
      {!devices.length ? <EmptyState icon={WifiOff} title="没有发现调试设备" note="连接并授权设备后点击刷新" action={selected.length ? <button className="secondary-button" type="button" onClick={onClear}><X size={16} />清空已保存选择</button> : undefined} /> : (
        <>
          <div className="device-selection-actions">
            <button className="secondary-button" type="button" onClick={onSelectReady} disabled={!readyCount}><CheckCircle2 size={17} />全选就绪设备</button>
            <button className="secondary-button" type="button" onClick={onClear} disabled={!selected.length}><X size={17} />清空选择</button>
            <span>{readyCount} 台设备当前可选，选择会自动保存</span>
          </div>
          {selected.length > 0 && <div className={`selection-health ${unavailable.length ? "is-warning" : ""}`}>
            {unavailable.length ? <><AlertTriangle size={18} /><div><strong>{unavailable.length} 台已选设备当前不可启动</strong><span>{unavailable.join("、")}。已保留选择，请恢复设备后刷新，或手动取消选择。</span></div></> : <><CheckCircle2 size={18} /><div><strong>{selected.length} 台设备已就绪，可进入配置</strong><span>压测组件同步和启动前预检都会使用这份已保存的选择。</span></div></>}
          </div>}
          <div className="device-table-wrap">
            <table className="data-table device-table">
              <thead><tr><th className="check-column"><span className="sr-only">选择</span></th><th>设备</th><th>系统</th><th>连接状态</th><th>电量</th><th>当前任务</th><th><span className="sr-only">操作</span></th></tr></thead>
              <tbody>{devices.map((device) => <tr key={device.serial} className={selected.includes(device.serial) ? "is-selected" : ""}>
                <td><button className={`checkbox ${selected.includes(device.serial) ? "is-checked" : ""}`} type="button" disabled={!selectable(device)} onClick={() => onToggle(device.serial)} aria-label={`选择 ${device.alias}`}>{selected.includes(device.serial) && <Check size={14} />}</button></td>
                <td><div className="device-identity"><span className="device-avatar"><Smartphone size={20} /></span><div><strong>{device.alias}</strong><small>{device.serial}</small></div></div></td>
                <td><strong className="table-main">{device.brand} {device.model}</strong><small className="table-sub">安卓 {device.android} · 开发接口 {device.sdk}</small></td>
                <td><StatusBadge status={device.status} /></td>
                <td>{device.battery == null ? <span className="muted">—</span> : <span className={`battery ${device.battery < 40 ? "low" : ""}`}><BatteryMedium size={17} />{device.battery}%</span>}</td>
                <td>{device.jobId ? <div className="device-task-cell"><div className="mini-progress"><span><i style={{ width: `${device.progress}%` }} /></span><b>{device.progress}%</b></div><small className="device-task-meta">第 {device.cycle || 1} 轮{device.remainingSeconds != null ? ` · 剩余 ${Math.ceil(device.remainingSeconds)} 秒` : ""}</small>{device.resumeAttempts ? <small className="device-task-meta">已恢复 {device.resumeAttempts} 次</small> : null}{device.lastError ? <small className="device-task-error" title={localizeRuntimeText(device.lastError)}>{localizeRuntimeText(device.lastError)}</small> : null}</div> : <span className="muted">—</span>}</td>
                <td><div className="device-action-group">{device.canResume && device.jobId ? <button className="icon-button small resume-button" type="button" onClick={() => onResume(device.jobId as string, device.serial)} disabled={busyAction === `resume-${device.serial}`} title="继续运行设备任务" aria-label={`继续运行 ${device.alias}`}>{busyAction === `resume-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <Play size={16} fill="currentColor" />}</button> : null}{device.canEnd && device.jobId ? <button className="icon-button small manual-end-button" type="button" onClick={() => onEnd(device.jobId as string, device.serial)} disabled={busyAction === `end-${device.serial}`} title="手动结束本设备测试（结束后不可继续）" aria-label={`手动结束 ${device.alias}`}>{busyAction === `end-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <XCircle size={17} />}</button> : null}{device.jobId ? <button className="icon-button small danger-ghost" type="button" onClick={() => onStop(device.serial)} disabled={busyAction === `stop-${device.serial}`} title="暂停设备压测（可继续）" aria-label={`暂停 ${device.alias}`}>{busyAction === `stop-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <CircleStop size={17} />}</button> : <button className="icon-button small" type="button" title={`${device.screenSize}`} aria-label={`${device.alias} 屏幕信息`}><MoreHorizontal size={18} /></button>}</div></td>
              </tr>)}</tbody>
            </table>
          </div>
          <div className="device-mobile-list">{devices.map((device) => <div className={`device-mobile-item ${selected.includes(device.serial) ? "is-selected" : ""}`} key={device.serial}><button className="device-mobile-select" type="button" disabled={!selectable(device)} onClick={() => onToggle(device.serial)}><span className="device-avatar"><Smartphone size={20} /></span><span className="device-mobile-copy"><strong>{device.alias}</strong><small>{device.serial} · 安卓 {device.android}</small><span><StatusBadge status={device.status} /><em>{device.battery == null ? "--" : `${device.battery}%`}</em></span>{device.jobId ? <small className="device-task-meta">第 {device.cycle || 1} 轮{device.remainingSeconds != null ? ` · 剩余 ${Math.ceil(device.remainingSeconds)} 秒` : ""}{device.resumeAttempts ? ` · 已恢复 ${device.resumeAttempts} 次` : ""}</small> : null}{device.lastError ? <small className="device-task-error" title={localizeRuntimeText(device.lastError)}>{localizeRuntimeText(device.lastError)}</small> : null}</span><span className={`checkbox ${selected.includes(device.serial) ? "is-checked" : ""}`}>{selected.includes(device.serial) && <Check size={14} />}</span></button>{device.jobId ? <div className="device-mobile-actions">{device.canResume ? <button className="device-mobile-action resume-button" type="button" onClick={() => onResume(device.jobId as string, device.serial)} disabled={busyAction === `resume-${device.serial}`}><Play size={15} fill="currentColor" />继续</button> : null}{device.canEnd ? <button className="device-mobile-action manual-end-button" type="button" onClick={() => onEnd(device.jobId as string, device.serial)} disabled={busyAction === `end-${device.serial}`}>{busyAction === `end-${device.serial}` ? <LoaderCircle size={15} className="spin" /> : <XCircle size={15} />}结束本设备</button> : null}<button className="device-mobile-action" type="button" onClick={() => onStop(device.serial)} disabled={busyAction === `stop-${device.serial}`}>{busyAction === `stop-${device.serial}` ? <LoaderCircle size={15} className="spin" /> : <CircleStop size={15} />}暂停</button></div> : null}</div>)}</div>
        </>
      )}
    </section>
  );
}

export function ConfigPage({ config, templates, devices, selectedDevices, preflight, setConfig, uploading, starting, onUpload, onStart, onManageDevices }: { config: TestConfig; templates: ConfigTemplate[]; devices: Device[]; selectedDevices: string[]; preflight: PreflightResult | null; setConfig: (config: TestConfig) => void; uploading: boolean; starting: boolean; onUpload: (file: File) => Promise<void>; onStart: () => void; onManageDevices: () => void }) {
  const [tab, setTab] = useState<"basic" | "strategy" | "monitor">("basic");
  const update = <K extends keyof TestConfig>(key: K, value: TestConfig[K]) => setConfig({ ...config, [key]: value });
  const template = templates.find((item) => item.name === config.configFile);
  const startPages = config.workflowMode === "generic"
    ? [
      { id: "audit_gate" as const, label: "审核中", description: "启动当前安装包后，按框架勾选“审核中”并确认；没有该页面时自动跳过。" },
      { id: "app_home" as const, label: "应用首页", description: "启动当前安装包后验证前台页面，再进入智能压测。" },
    ]
    : (template?.startupPages || []);
  const selectedRows = selectedDevices.map((serial) => devices.find((device) => device.serial === serial) || { serial, alias: "未发现设备", ready: false, jobId: null });
  const unavailable = selectedRows.filter((device) => !device.ready || Boolean(device.jobId));
  const deviceCount = selectedDevices.length;
  const parametersValid = validRunParameters(config);
  const canStart = Boolean(parametersValid && config.apkPath && config.apkPackageName && config.packageName.trim() && selectedDevices.length && !unavailable.length && !starting && !uploading);
  return (
    <div className="config-layout">
      <section className="panel config-panel">
        <div className="segmented-tabs" role="tablist"><button type="button" className={tab === "basic" ? "is-active" : ""} onClick={() => setTab("basic")}>应用与引擎</button><button type="button" className={tab === "strategy" ? "is-active" : ""} onClick={() => setTab("strategy")}>执行策略</button><button type="button" className={tab === "monitor" ? "is-active" : ""} onClick={() => setTab("monitor")}>监控与证据</button></div>
        {tab === "basic" && <div className="form-section">
          <div className="form-heading"><h2>应用信息</h2><span>文件保存在本机</span></div>
          <label className="field-label" htmlFor="project-config">压测参数模板</label>
          <div className="input-shell"><FileJson size={17} /><select id="project-config" value={config.configFile} onChange={(event) => { const template = templates.find((item) => item.name === event.target.value); if (template) setConfig(configFromTemplate(template, config, Boolean(config.apkPackageName))); }}><option value="">选择已有压测参数</option>{templates.map((item) => <option value={item.name} key={item.name}>{item.name} · {item.packageName}</option>)}</select><ChevronDown size={15} /></div>
          <p className="field-note">已有包会复用对应页面流程；新包只继承已验证的压测运行依赖、文件校验值和运行参数。</p>
          <label className="field-label">安卓安装包</label>
          <label className={`file-picker ${uploading ? "is-uploading" : ""}`}><span className="file-type">{uploading ? <LoaderCircle size={21} className="spin" /> : <FileArchive size={21} />}</span><span><strong>{uploading ? "正在上传…" : config.apkName || "选择本地安装包"}</strong><small>{config.apkPath ? "已上传到前端本地运行目录" : "仅接受安卓安装包文件"}</small></span><input type="file" accept=".apk,application/vnd.android.package-archive" disabled={uploading} onChange={(event) => { const file = event.target.files?.[0]; if (file) void onUpload(file); event.currentTarget.value = ""; }} /><span className="secondary-button"><Upload size={16} />{config.apkPath ? "更换" : "上传"}</span></label>
          <label className="field-label" htmlFor="package-name">目标包名</label><div className="input-shell"><FileJson size={17} /><input id="package-name" value={config.packageName} readOnly placeholder="先选择项目配置" /></div>
          {config.apkPackageName && <p className="field-note success-note"><CheckCircle2 size={15} />安装包清单已确认：{config.apkPackageName}</p>}
          {config.apkPackageName && config.workflowMode === "generic" && config.auditGateEveryLaunch && <p className="field-note generic-note"><ShieldCheck size={15} />通用模式会在每次启动时按框架尝试“审核中”勾选与确认；若当前安装包没有审核页，会自动跳过后进入压测。</p>}
          <div className="form-heading section-divider"><h2>测试引擎</h2><span>固定引擎</span></div>
          <div className="engine-fixed"><span><Zap size={20} /></span><div><strong>智能压测引擎</strong><small>{config.fastbotClasspath || "请选择项目配置"}</small><small>{config.fastbotMd5 ? `文件校验值 ${config.fastbotMd5}` : "配置未提供文件校验值"}</small></div><span className="engine-lock"><ShieldCheck size={16} />已锁定</span></div>
          {config.scenarioTarget && <div className="config-evidence"><strong>{config.workflowMode === "generic" ? "模式" : "场景"}：{config.scenarioTarget}</strong><span>{config.navigationSteps} 个导航步骤 · {config.fastbotExtraArgs.length} 个压测安全参数</span></div>}
        </div>}
        {tab === "basic" && <div className="form-section startup-page-section">
          <div className="form-heading"><h2>框架启动路径</h2><span>直接对应已选配置文件的导航步骤</span></div>
          <div className="setting-list compact-settings"><div><span className="setting-icon"><ShieldCheck size={19} /></span><div><strong>每次启动必点“审核中”</strong><small>首次启动、每轮清数据、卡页恢复和前台重启都会处理；页面不存在时自动跳过。</small></div><Toggle checked={config.auditGateEveryLaunch} onChange={() => update("auditGateEveryLaunch", !config.auditGateEveryLaunch)} label="每次启动必点审核中" /></div></div>
          <span className="field-label">启动页面</span>
          {startPages.length ? <><div className="startup-page-options" role="radiogroup" aria-label="框架启动页面">{startPages.map((item) => <button className={config.startupPage === item.id ? "is-selected" : ""} type="button" role="radio" aria-checked={config.startupPage === item.id} onClick={() => update("startupPage", item.id)} key={item.id}><span className="startup-page-radio" /><MonitorSmartphone size={18} /><span><strong>{item.label}</strong><small>{item.id === "audit_gate" ? "先勾选审核中并确认" : "启动应用后验证前台页面"}</small></span><CheckCircle2 size={18} /></button>)}</div><p className="field-note success-note"><CheckCircle2 size={15} />{startPages.find((item) => item.id === config.startupPage)?.description}</p></> : <p className="field-note error-note"><AlertTriangle size={15} />当前框架没有声明可选择的启动页面。</p>}
          <div className={`launch-review ${unavailable.length ? "is-warning" : ""}`}>
            <div className="launch-review-heading"><div><span>启动前复核</span><strong>{deviceCount} 台设备 · {formatMinutes(config.duration)} · {config.throttle} 毫秒</strong></div><button className="text-button" type="button" onClick={onManageDevices}>管理设备</button></div>
            <p>框架配置：{config.configFile || "未选择"}。审核门禁：{config.auditGateEveryLaunch ? "每次启动处理" : "关闭"}。启动页面：{startPages.find((item) => item.id === config.startupPage)?.label || "未选择"}。每台执行 {config.cycles} 轮，最多并发 {Math.min(config.workers, Math.max(1, deviceCount))} 台。</p>
            {selectedRows.length ? <div className="selected-device-chips">{selectedRows.map((device) => <span className={!device.ready || device.jobId ? "is-unavailable" : ""} key={device.serial}><Smartphone size={13} />{device.alias || device.serial}<small>{device.serial}</small></span>)}</div> : <p className="launch-readiness-error"><AlertTriangle size={15} />尚未选择设备</p>}
            {unavailable.length ? <p className="launch-readiness-error"><AlertTriangle size={15} />以下设备暂不可启动：{unavailable.map((device) => device.serial).join("、")}</p> : null}
            {!parametersValid ? <p className="launch-readiness-error"><AlertTriangle size={15} />时长、轮数、事件间隔和并发设备必须填写有效整数</p> : null}
          </div>
        </div>}
        {tab === "strategy" && <div className="form-section">
          <div className="form-heading"><h2>执行参数</h2><span>每台设备</span></div>
          <div className="field-grid"><label className="field"><span>单轮时长</span><div className="number-input"><input type="number" min="1" max="1440" value={config.duration} onChange={(event) => update("duration", Number(event.target.value))} /><em>分钟</em></div></label><label className="field"><span>循环轮数</span><div className="number-input"><input type="number" min="1" max="100" value={config.cycles} onChange={(event) => update("cycles", Number(event.target.value))} /><em>轮</em></div></label><label className="field"><span>事件间隔</span><div className="number-input"><input type="number" min="0" max="60000" step="50" value={config.throttle} onChange={(event) => update("throttle", Number(event.target.value))} /><em>毫秒</em></div></label><label className="field"><span>并发设备</span><div className="number-input"><input type="number" min="1" max={MAX_SAFE_WORKERS} value={config.workers} onChange={(event) => update("workers", Math.min(MAX_SAFE_WORKERS, Math.max(1, Number(event.target.value))))} /><em>台</em></div></label></div>
          <div className="setting-list compact-settings"><div><span className="setting-icon"><HardDrive size={19} /></span><div><strong>每轮清除应用数据</strong><small>保证轮次之间的数据状态隔离</small></div><Toggle checked={config.clearAppData} onChange={() => update("clearAppData", !config.clearAppData)} label="每轮清除应用数据" /></div><div><span className="setting-icon"><CircleStop size={19} /></span><div><strong>崩溃后停止</strong><small>发现目标应用崩溃后不再执行后续轮次</small></div><Toggle checked={config.stopOnCrash} onChange={() => update("stopOnCrash", !config.stopOnCrash)} label="崩溃后停止" /></div></div>
        </div>}
        {tab === "monitor" && <div className="form-section">
          <div className="form-heading"><h2>证据采集</h2><span>本地报告目录</span></div>
          <div className="setting-list"><div><span className="setting-icon"><HardDrive size={19} /></span><div><strong>周期截图</strong><small>每 5 秒保存当前设备画面</small></div><Toggle checked={config.screenshot} onChange={() => update("screenshot", !config.screenshot)} label="周期截图" /></div><div><span className="setting-icon"><TerminalSquare size={19} /></span><div><strong>日志与页面证据</strong><small>固定采集系统日志、压测日志和关键截图；录屏已停用</small></div><span className="fixed-off"><CircleStop size={16} />录屏停用</span></div><div><span className="setting-icon"><Activity size={19} /></span><div><strong>卡页监控</strong><small>页面指纹与前台页面联合判断</small></div><Toggle checked={config.stallWatchdog} onChange={() => update("stallWatchdog", !config.stallWatchdog)} label="卡页监控" /></div></div>
          {config.stallWatchdog && <div className="recovery-mode"><span>命中卡页时</span><div className="segmented-control"><button type="button" className={config.stallMode === "observe" ? "is-active" : ""} onClick={() => update("stallMode", "observe")}>仅记录</button><button type="button" className={config.stallMode === "recover_once" ? "is-active" : ""} onClick={() => update("stallMode", "recover_once")}>恢复一次</button></div></div>}
        </div>}
      </section>
      <aside className="panel launch-summary"><span className="eyebrow">任务摘要</span><h2>{config.apkName || "尚未上传安装包"}</h2><dl><div><dt>测试引擎</dt><dd>智能压测</dd></div><div><dt>已选设备</dt><dd>{deviceCount} 台</dd></div><div><dt>单台预计时长</dt><dd>{config.duration * config.cycles} 分钟</dd></div><div><dt>并发数量</dt><dd>{Math.min(config.workers, Math.max(1, deviceCount))} 台</dd></div></dl>{preflight && <div className={`preflight-result ${preflight.success ? "is-ok" : "is-error"}`}><strong>{preflight.success ? "启动前预检通过" : "启动前预检未通过"}</strong><small>{preflight.success ? "设备和压测文件已验证" : "本次启动请求未提交，请处理以下检查项"}</small>{!preflight.success && preflightFailureDetails(preflight).map((detail) => <span key={detail}>{localizeRuntimeText(detail)}</span>)}</div>}<button className="primary-button launch-button" type="button" onClick={onStart} disabled={!canStart}>{starting ? <LoaderCircle size={18} className="spin" /> : <Play size={18} fill="currentColor" />}{starting ? "正在预检" : "启动压测"}</button><p className="summary-note"><CloudOff size={15} />安装包、设备和报告仅保存在本机</p></aside>
    </div>
  );
}

export function LegacyRunsPage({ runs, devices, busyAction, onStop, onResume, onEnd, onConfirmInstall, onNavigate }: { runs: Run[]; devices: Device[]; busyAction: string | null; onStop: (run: Run) => void; onResume: (jobId: string, serial: string) => void; onEnd: (jobId: string, serial: string) => void; onConfirmInstall: (run: Run) => void; onNavigate: (view: View) => void }) {
  const active = runs.find(isActiveRun) || null;
  const activeDevices = active ? devices.filter((device) => device.jobId === active.id) : [];
  const lines = (active?.log || "").split(/\r?\n/).filter(Boolean).slice(-100);
  if (!active && !runs.length) return <section className="panel page-panel"><EmptyState icon={Activity} title="还没有测试任务" note="选择设备并配置安装包后启动压测" action={<button className="primary-button" type="button" onClick={() => onNavigate("config")}><Play size={16} />新建测试</button>} /></section>;
  return (
    <>
      {active ? <><section className="run-hero"><div><span className="eyebrow"><span className="live-dot" />{phaseLabel(active)}</span><h2>{active.apkName}</h2><p>智能压测 · {active.devices.length} 台设备 · {active.cycles} 轮</p>{!active.evidence?.pressureStarted && active.status !== "stopping" && active.status !== "awaiting_install" && <p className="run-warning"><AlertTriangle size={15} />当前尚未检测到设备侧运行证据，未将此任务计为压测中。</p>}</div><div className="run-clock"><small>已运行</small><strong>{active.elapsed}</strong></div><button className="danger-button" type="button" onClick={() => onStop(active)} disabled={busyAction === `stop-${active.id}`} >{busyAction === `stop-${active.id}` ? <LoaderCircle size={17} className="spin" /> : <CircleStop size={17} />}停止全部</button></section>
        {active.status === "awaiting_install" && (() => { const installation = active.installation; const devices = installation ? Object.values(installation.devices) : []; const installedCount = devices.filter((item) => item.installed).length; const pendingDevices = installation ? installation.pendingDevices : active.devices; return <section className="install-gate panel"><span className="install-gate-icon"><Smartphone size={24} /></span><div className="install-gate-copy"><span className="eyebrow">需要人工操作</span><h2>请在设备上完成安装包安装</h2><p>系统安装页已经拉起。请逐台点击“安装”或“继续安装”，安装完成后回到这里确认。平台会校验包是否真实存在，通过后才会清除应用数据并继续场景流程。</p><p className="install-package-note">当前核验包名：<code>{active.packageName}</code></p><p className="install-progress-note">已检测到 {installedCount} / {active.devices.length} 台设备完成安装，仍需处理 {pendingDevices.length} 台</p><div className="install-device-chips">{pendingDevices.map((serial) => <span key={serial}><Smartphone size={13} />{serial}</span>)}</div></div><div className="install-gate-actions"><button className="primary-button" type="button" onClick={() => onConfirmInstall(active)} disabled={busyAction === `confirm-install-${active.id}`}>{busyAction === `confirm-install-${active.id}` ? <LoaderCircle size={17} className="spin" /> : <CheckCircle2 size={17} />}{busyAction === `confirm-install-${active.id}` ? "正在校验" : "已完成安装，继续"}</button><small>未安装完成时不会放行</small></div></section>; })()}
         <div className="run-device-grid">{activeDevices.map((device) => <section className="panel run-device-card" key={device.serial}><div className="run-card-top"><div className="device-identity"><span className="device-avatar"><Smartphone size={20} /></span><div><strong>{device.alias}</strong><small>{device.serial}</small></div></div><div className="run-card-actions"><StatusBadge status={device.status} />{device.canResume && device.jobId ? <button className="icon-button small resume-button" type="button" onClick={() => onResume(device.jobId as string, device.serial)} disabled={busyAction === `resume-${device.serial}`} title="继续运行设备任务" aria-label={`继续运行 ${device.alias}`}>{busyAction === `resume-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <Play size={16} fill="currentColor" />}</button> : null}{device.canEnd && device.jobId ? <button className="icon-button small manual-end-button" type="button" onClick={() => onEnd(device.jobId as string, device.serial)} disabled={busyAction === `end-${device.serial}`} title="手动结束本设备测试（结束后不可继续）" aria-label={`手动结束 ${device.alias}`}>{busyAction === `end-${device.serial}` ? <LoaderCircle size={17} className="spin" /> : <XCircle size={17} />}</button> : null}</div></div><div className="run-progress-number"><strong>{device.progress}</strong><span>%</span></div><div className="progress-track large"><span style={{ width: `${device.progress}%` }} /></div><div className="run-stats"><div><small>当前轮次</small><strong>{device.cycle || 1} / {active.cycles}</strong></div><div><small>剩余时间</small><strong>{device.remainingSeconds != null ? `${Math.ceil(device.remainingSeconds)} 秒` : device.elapsed}</strong></div><div><small>恢复次数</small><strong>{device.resumeAttempts || 0}</strong></div></div>{device.lastError ? <div className="run-device-error" title={localizeRuntimeText(device.lastError)}><AlertTriangle size={15} /><span>{localizeRuntimeText(device.lastError)}</span></div> : null}{device.waitingForDevice || device.status === "waiting_for_device" ? <div className="run-device-waiting"><CloudOff size={15} />等待设备重新连接</div> : null}</section>)}</div>
        <section className="panel log-panel"><div className="panel-heading"><div><span className="eyebrow">实时输出</span><h2>任务运行日志</h2></div><div className="log-filter"><Wifi size={15} />自动刷新</div></div>{active.evidence?.errors?.length ? <div className="run-error-summary"><AlertTriangle size={17} /><div><strong>{active.evidence.pressureStarted ? "运行异常" : "安装包安装失败或准备失败，未进入压测阶段"}</strong>{active.evidence.errors.map((item) => <small key={item}>{localizeRuntimeText(item)}</small>)}</div></div> : null}<div className="terminal-log">{lines.length ? lines.map((line, index) => <div className="raw-log-line" key={`${index}-${line}`}><time>{String(index + 1).padStart(3, "0")}</time><span className={/error|failed|exception/i.test(line) ? "warning" : "success"}>{/error|failed|exception/i.test(line) ? "错误" : "日志"}</span><p>{localizeRuntimeText(line)}</p></div>) : <div className="terminal-wait"><LoaderCircle size={16} className="spin" />等待任务输出</div>}</div></section></> : <section className="panel page-panel"><EmptyState icon={CheckCircle2} title="当前没有运行中的任务" note="最近任务已结束，可在下方查看状态" /></section>}
      <section className="panel recent-panel"><div className="panel-heading"><div><span className="eyebrow">本次服务会话</span><h2>任务记录</h2></div></div><div className="run-history">{runs.map((run) => <div key={run.id}><span className={`run-history-icon ${run.status}`} >{run.status === "completed" ? <CheckCircle2 size={18} /> : run.status === "failed" ? <XCircle size={18} /> : isActiveRun(run) ? <LoaderCircle size={18} className="spin" /> : <CircleStop size={18} />}</span><div><strong>{run.apkName || run.packageName}</strong><small>{run.id} · {formatDate(run.startedAt)}</small></div><span>{run.devices.length} 台 · {run.cycles} 轮</span><b>{runStatusLabel(run.status)}</b></div>)}</div></section>
    </>
  );
}

export function ReportsPage({ items, query, status, onQuery, onStatus, onOpen, onDelete, deletingReportId, onRefresh, refreshing }: { items: TestReport[]; query: string; status: string; onQuery: (value: string) => void; onStatus: (value: string) => void; onOpen: (report: TestReport) => void; onDelete: (report: TestReport) => void; deletingReportId: string | null; onRefresh: () => void; refreshing: boolean }) {
  const deviceCount = items.reduce((sum, item) => sum + item.deviceCount, 0);
  const crashCount = items.reduce((sum, item) => sum + item.crashes, 0);
  const abnormalTests = items.filter((item) => item.status !== "passed").length;
  const affectedDevices = items.reduce((sum, item) => sum + item.devices.filter((device) => device.status !== "passed" || device.crashes || device.stallRecoveryFailures || device.issues).length, 0);
  return <section className="panel page-panel reports-page">
    <div className="section-toolbar report-toolbar">
      <div className="search-input"><Search size={17} /><input value={query} onChange={(event) => onQuery(event.target.value)} placeholder="搜索测试编号、安装包、设备或包名" /></div>
      <label className="select-shell"><ListFilter size={16} /><select value={status} onChange={(event) => onStatus(event.target.value)}><option value="all">全部状态</option><option value="running">运行中</option><option value="incomplete">未完整结束</option><option value="passed">通过</option><option value="failed">失败</option><option value="stopped">已停止</option></select><ChevronDown size={15} /></label>
      <button className="secondary-button" type="button" onClick={onRefresh} disabled={refreshing}><RefreshCw size={17} className={refreshing ? "spin" : ""} />刷新报告</button>
    </div>
    {items.length ? <>
      <div className="report-overview-strip">
        <div><small>测试记录</small><strong>{items.length}</strong></div>
        <div><small>设备报告</small><strong>{deviceCount}</strong></div>
        <div className={abnormalTests || affectedDevices ? "has-risk" : ""}><small>异常测试 / 设备</small><strong>{abnormalTests} / {affectedDevices}</strong></div>
        <div className={crashCount ? "has-risk" : ""}><small>崩溃总数</small><strong>{crashCount}</strong></div>
      </div>
      <TestReportList items={items} onOpen={onOpen} onDelete={onDelete} deletingReportId={deletingReportId} />
    </> : <EmptyState icon={Search} title="没有匹配的测试记录" note={query || status !== "all" ? "调整搜索或状态筛选" : "完成压测任务后会按测试批次归档"} />}
    <div className="table-footer"><span>共 {items.length} 次测试，每次测试只占一条记录</span></div>
  </section>;
}

export function TestReportList({ items, onOpen, onDelete, deletingReportId }: { items: TestReport[]; onOpen: (report: TestReport) => void; onDelete: (report: TestReport) => void; deletingReportId: string | null }) {
  return <div className="test-report-list">{items.map((report) => { const diagnosis = testDiagnosis(report); const deleting = deletingReportId === report.id; const deleteDisabled = report.status === "running" || deleting; return <div className="test-report-row" key={report.id}>
    <button type="button" className="test-report-main" onClick={() => onOpen(report)} aria-label={`查看测试报告 ${report.testId}`}>
      <span className="test-report-date"><small>{report.finishedAt ? "完成时间" : "开始时间"}</small><strong>{formatDate(report.finishedAt || report.startedAt, true)}</strong></span>
      <span className="test-report-identity"><strong>{report.apkName || report.packageName}</strong><code>{report.testId}</code><small>{report.packageName}</small></span>
      <span className="test-report-devices"><strong>{report.reportedDevices} / {report.deviceCount} 台</strong><small>{report.passedDevices} 通过 · {report.failedDevices} 失败 · {runningDeviceCount(report)} 运行中</small><em className={`test-row-diagnosis ${diagnosis.tone}`}>{diagnosis.title}</em></span>
      <span className="test-report-pressure"><span><small>实际压测 {formatDuration(report.pressureElapsedSeconds)} / {formatDuration(report.pressureTargetSeconds)}</small><b>{report.pressurePercent}%</b></span><span className="progress-track"><i style={{ width: `${Math.min(100, report.pressurePercent)}%` }} /></span><small>{report.recordedCycles} / {report.expectedCycles} 轮有记录</small></span>
      <span className={`test-report-crashes ${report.crashes ? "has-crash" : ""}`}><Bug size={17} /><strong>{report.crashes}</strong><small>{report.crashes ? "个崩溃" : "无崩溃"}</small></span>
      <ReportStatus status={report.status} />
      <ChevronRight className="test-report-open" size={19} />
    </button>
    <button className="icon-button small test-report-delete" type="button" onClick={() => onDelete(report)} disabled={deleteDisabled} aria-label={`删除测试报告 ${report.testId}`} title={report.status === "running" ? "运行中的测试不能删除" : "删除本次测试报告"}>{deleting ? <LoaderCircle size={17} className="spin" /> : <Trash2 size={17} />}</button>
  </div>; })}</div>;
}

export function ReportTable({ items, onOpen }: { items: Report[]; onOpen: (report: Report) => void }) {
  return <div className="report-table-wrap"><table className="data-table report-table"><thead><tr><th>测试任务</th><th>设备</th><th>完成时间</th><th>压力时长</th><th>结果</th><th>异常</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{items.map((report) => { const hasRisk = Boolean(report.crashes || report.stallRecoveryFailures || report.issues); return <tr key={report.id} onClick={() => onOpen(report)}><td><strong className="table-main">{report.packageName}</strong><small className="table-sub">{report.runId}</small></td><td><strong className="table-main">{report.device}</strong><small className="table-sub">{report.deviceSerial}</small></td><td>{formatDate(report.finishedAt, true)}</td><td>{formatDuration(report.durationSeconds)}</td><td><ReportStatus status={report.status} manualEnd={report.manualEnd} /></td><td><span className={hasRisk ? "error-count" : "clean-count"}>{report.crashes ? `${report.crashes} 崩溃` : report.stallRecoveryFailures ? `${report.stallRecoveryFailures} 次保底恢复失败` : report.issues ? `${report.issues} 条需复核问题` : report.fallbackRecoveries ? `${report.fallbackRecoveries} 次保底恢复` : "无异常"}</span></td><td><button className="icon-button small" type="button" aria-label={`查看 ${report.runId}`}><MoreHorizontal size={18} /></button></td></tr>; })}</tbody></table><div className="report-mobile-list">{items.map((report) => <button type="button" className="report-mobile-item" key={report.id} onClick={() => onOpen(report)}><span><strong>{report.packageName}</strong><small>{report.runId} · {report.device}</small></span><span><ReportStatus status={report.status} manualEnd={report.manualEnd} /><small>{formatDate(report.finishedAt, true)}</small></span></button>)}</div></div>;
}

export function ReportStatus({ status, manualEnd = false }: { status: Report["status"] | TestReport["status"]; manualEnd?: boolean }) {
  if (manualEnd) return <span className="report-status stopped"><XCircle size={15} />已手动结束</span>;
  if (status === "passed") return <span className="report-status passed"><CheckCircle2 size={15} />通过</span>;
  if (status === "failed") return <span className="report-status failed"><XCircle size={15} />失败</span>;
  if (status === "running") return <span className="report-status passed"><LoaderCircle size={15} className="spin" />运行中</span>;
  if (status === "incomplete") return <span className="report-status incomplete"><AlertTriangle size={15} />未完整结束</span>;
  if (["recovering", "waiting_for_device", "resuming"].includes(String(status))) return <span className="report-status running"><LoaderCircle size={15} className="spin" />{runStatusLabel(String(status) as Run["status"])}</span>;
  if (["paused", "needs_manual_resume"].includes(String(status))) return <span className="report-status stopped"><AlertTriangle size={15} />{runStatusLabel(String(status) as Run["status"])}</span>;
  return <span className="report-status stopped"><CircleStop size={15} />已停止</span>;
}

export const artifactLabels: Record<string, { title: string; note: string; icon: typeof FileJson }> = {
  json: { title: "report.json", note: "结构化测试结果", icon: FileJson },
  html: { title: "report.html", note: "完整可视报告", icon: BarChart3 },
  fastbot: { title: "fastbot.log", note: "智能压测引擎日志", icon: TerminalSquare },
  logcat: { title: "logcat.log", note: "安卓系统日志", icon: TerminalSquare },
  screen_record: { title: "screen_record.mp4", note: "历史有效录屏（新任务已停用）", icon: MonitorSmartphone },
  manifest: { title: "artifact_manifest.json", note: "产物校验清单", icon: ShieldCheck },
};

export async function copyToClipboard(value: string): Promise<void> {
  const text = String(value || "");
  if (!text.trim()) throw new Error("没有可复制的崩溃定位信息");
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Clipboard API can exist but reject on local HTTP or a changed focus.
      // Continue with the user-gesture-compatible textarea fallback.
    }
  }
  const input = document.createElement("textarea");
  input.value = text;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = typeof document.execCommand === "function" && document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("浏览器未允许复制");
}
