import type { ApkUpload, ConfigTemplate, Device, Draft, PreflightResult, ReplayProgress, ReplayResult, Report, Run, TestConfig, TestReport } from "./types";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const REQUEST_TIMEOUT_MS = 30000;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // A console request that never settles keeps the UI on stale data and makes
  // manual actions queue behind a hung poll. Bound every call and surface the
  // timeout as a normal ApiError.
  const controller = new AbortController();
  // ``globalThis`` keeps this module usable from the Node-based contract tests,
  // where ``window`` does not exist.
  const timer = globalThis.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(path, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new ApiError("本地服务响应超时，请稍后重试", 0);
    }
    throw new ApiError("无法连接本地服务，请运行 frontend/start.ps1", 0);
  } finally {
    globalThis.clearTimeout(timer);
  }
  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new ApiError(body?.error || `请求失败 (${response.status})`, response.status);
  }
  if (!contentType.includes("application/json")) {
    throw new ApiError("本地接口返回了网页，请重启本地控制台服务后重试", response.status);
  }
  if (body === null) {
    throw new ApiError("本地服务返回了空数据", response.status);
  }
  return body as T;
}

function jsonRequest(method: string, value: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(value),
  };
}

export const api = {
  health: () => request<{ ok: boolean; mode: string; engine: "fastbot"; time: string }>("/api/health"),
  config: () => request<TestConfig>("/api/config"),
  configs: () => request<{ configs: ConfigTemplate[] }>("/api/configs"),
  draft: () => request<Draft>("/api/draft"),
  saveDraft: (draft: Pick<Draft, "selectedDevices" | "config">) =>
    request<Draft>("/api/draft", jsonRequest("PUT", draft)),
  devices: () => request<{ devices: Device[]; refreshedAt: string }>("/api/devices"),
  runs: (includeLog = false) => request<{ runs: Run[] }>(`/api/runs${includeLog ? "?log=1" : ""}`),
  run: (id: string) => request<Run>(`/api/runs/${encodeURIComponent(id)}`),
  reports: () => request<{ reports: Report[] }>("/api/reports"),
  report: (id: string) => request<Report>(`/api/reports/${encodeURIComponent(id)}`),
  testReports: () => request<{ tests: TestReport[] }>("/api/test-reports"),
  testReport: (id: string) => request<TestReport>(`/api/test-reports/${encodeURIComponent(id)}`),
  deleteTestReport: (id: string) =>
    request<{ deleted: boolean; id: string; testId: string }>(
      `/api/test-reports/${encodeURIComponent(id)}`,
      { method: "DELETE" },
    ),
  uploadApk: (file: File) =>
    request<ApkUpload>("/api/apks", {
      method: "POST",
      headers: {
        "Content-Type": "application/vnd.android.package-archive",
        "X-Filename": encodeURIComponent(file.name),
      },
      body: file,
    }),
  startRun: (config: TestConfig, devices: string[]) =>
    request<Run>("/api/runs", jsonRequest("POST", { ...config, devices })),
  preflightRun: (config: TestConfig, devices: string[]) =>
    request<PreflightResult>("/api/runs/preflight", jsonRequest("POST", { ...config, devices })),
  stopRun: (id: string) =>
    request<{ results: Array<{ serial: string; stopped: boolean; error?: string }> }>(
      `/api/runs/${encodeURIComponent(id)}/stop`,
      jsonRequest("POST", {}),
    ),
  confirmInstall: (id: string) =>
    request<Run>(
      `/api/runs/${encodeURIComponent(id)}/confirm-install`,
      jsonRequest("POST", {}),
    ),
  stopDevices: (devices: string[]) =>
    request<{ results: Array<{ serial: string; stopped: boolean; error?: string }> }>(
      "/api/devices/stop",
      jsonRequest("POST", { devices }),
    ),
  resumeJobDevice: (jobId: string, serial: string) =>
    request<Run>(
      `/api/jobs/${encodeURIComponent(jobId)}/devices/${encodeURIComponent(serial)}/resume`,
      jsonRequest("POST", {}),
    ),
  endJobDevice: (jobId: string, serial: string) =>
    request<{ serial: string; ended: boolean; stopped: boolean; manualEnd: boolean; remoteStopped?: boolean; remoteError?: string | null }>(
      `/api/jobs/${encodeURIComponent(jobId)}/devices/${encodeURIComponent(serial)}/end`,
      jsonRequest("POST", {}),
    ),
  syncFastbot: (devices: string[]) =>
    request<{ ok: boolean; results: Array<{ device_serial: string; synced: boolean; error?: string }> | null; output: string }>(
      "/api/devices/sync-fastbot",
      jsonRequest("POST", { devices }),
    ),
  replayCrash: (reportId: string, crashIndex: number, startMode: "auto" | "current" = "auto") =>
    request<ReplayResult>(
      `/api/reports/${encodeURIComponent(reportId)}/replay`,
      jsonRequest("POST", { crashIndex, startMode }),
    ),
  replayProgress: (reportId: string, crashIndex: number) =>
    request<ReplayProgress>(
      `/api/reports/${encodeURIComponent(reportId)}/replay-progress?crashIndex=${encodeURIComponent(String(crashIndex))}`,
    ),
  artifactUrl: (reportId: string, name: string) =>
    `/api/reports/${encodeURIComponent(reportId)}/artifact?name=${encodeURIComponent(name)}`,
};
