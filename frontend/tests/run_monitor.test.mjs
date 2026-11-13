import test, { before, after } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createServer } from "vite";

let vite;
let ActiveRunMonitor;
let Overview;
let DeviceReportDetail;
let developerReportText;

before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  vite = await createServer({
    root,
    configFile: false,
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
    esbuild: { jsx: "automatic" },
  });
  // Components and helpers live in dedicated modules after the App.tsx split.
  const pages = await vite.ssrLoadModule("/src/pages/pages.tsx");
  const runs = await vite.ssrLoadModule("/src/pages/runs.tsx");
  const detail = await vite.ssrLoadModule("/src/pages/report-detail.tsx");
  ({ Overview } = pages);
  ({ ActiveRunMonitor } = runs);
  ({ DeviceReportDetail, developerReportText } = detail);
});

after(async () => { await vite?.close(); });

const actions = {
  busyAction: null, onStop() {}, onResume() {}, onEnd() {}, onConfirmInstall() {}, onNavigate() {},
};

const liveDevices = ["honor", "xiaomi"].map((serial) => ({
  serial, alias: serial, model: serial, brand: "Android", android: "14", sdk: "34",
  screenSize: "1080x2400", battery: 80, adbState: "device", ready: true,
  status: "ready", jobId: null, progress: 1, cycle: 1, elapsed: "--:--",
  reportStatus: "completed_with_errors", operations: 118,
  fastbotAlive: false, operationStalled: true, waitingForDevice: true,
}));

function run(id, serial, operations) {
  return {
    id, status: "running", apkName: "sample.apk", packageName: "com.example.sample",
    devices: [serial], duration: 240, cycles: 1, workers: 1, elapsed: "00:50:00",
    evidence: { pressureStarted: true },
    reports: [{ deviceSerial: serial, rawStatus: "running", fastbotAlive: true }],
    deviceProgress: { [serial]: {
      status: "running", progress: 22, cycle: 1, elapsedSeconds: 3000,
      remainingSeconds: 11400, operations, resumeAttempts: 2, lastError: null,
    } },
  };
}

for (const [id, serial, operations] of [["WEB-second", "honor", 34098], ["WEB-third", "xiaomi", 48177]]) {
  test(`${id} displays its device progress when the global device has no task owner`, () => {
    const html = renderToStaticMarkup(createElement(ActiveRunMonitor, {
      ...actions, run: run(id, serial, operations), devices: liveDevices,
    }));
    assert.ok(html.includes('class="panel run-device-card"'));
    assert.ok(html.includes(serial));
    assert.ok(html.includes(operations.toLocaleString()));
    assert.ok(html.includes("<strong>22</strong>"));
    assert.ok(!html.includes("正在等待设备任务状态"));
    assert.ok(!html.includes("操作心跳已停止"));
    assert.ok(!html.includes("等待设备重新连接"));
  });
}

test("the original task keeps its terminal result when a replacement owns the live device", () => {
  const original = run("WEB-original", "honor", 118);
  original.deviceProgress.honor = {
    ...original.deviceProgress.honor, status: "completed_with_errors", progress: 1,
    remainingSeconds: 0, operations: 118,
  };
  original.reports = [{ deviceSerial: "honor", rawStatus: "completed_with_errors" }];
  const html = renderToStaticMarkup(createElement(ActiveRunMonitor, {
    ...actions, run: original,
    devices: [{ ...liveDevices[0], jobId: "WEB-second", status: "running", operations: 34098 }],
  }));
  assert.ok(html.includes('class="panel run-device-card"'));
  assert.ok(html.includes("118"));
  assert.ok(!html.includes((34098).toLocaleString()));
  assert.ok(!html.includes("手动结束本设备测试"));
});

test("overview displays the selected task's device from its own snapshot", () => {
  const html = renderToStaticMarkup(createElement(Overview, {
    ...actions, runs: [run("WEB-third", "xiaomi", 48177)], devices: liveDevices, reports: [],
  }));
  assert.ok(html.includes('class="device-run-row"'));
  assert.ok(html.includes("xiaomi"));
  assert.ok(!html.includes("正在等待设备任务状态"));
});

test("permission failure is visible in the device report and its text export", () => {
  const reason = "权限处理超时：设备停在系统的“所有文件访问权限”设置页，未返回被测 App。";
  const device = {
    device: "HONOR JLH-AN00", deviceSerial: "honor", status: "failed",
    reportAvailable: true, permissionFailureReason: reason,
    durationSeconds: 6181, pressureTargetSeconds: 13620, pressurePercent: 45.4,
    cycles: 1, expectedCycles: 1, operations: 68344, crashes: 0, issues: 1,
    stalls: 18, fallbackRecoveries: 18, stallRecoveryFailures: 0, artifacts: [],
  };
  const html = renderToStaticMarkup(createElement(DeviceReportDetail, { device }));
  assert.match(html, /<small>失败原因<\/small>/);
  assert.ok(html.includes(reason));

  const exported = developerReportText({
    testId: "WEB-honor", packageName: "com.example.sample", status: "failed", devices: [device],
    deviceCount: 1, reportedDevices: 1, failedDevices: 1, operations: device.operations,
  });
  assert.ok(exported.includes(`失败原因：${reason}`));
});
