import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

let vite;
let format;
let labels;
let runs;

test.before(async () => {
  const root = fileURLToPath(new URL("../", import.meta.url));
  vite = await createServer({
    root,
    configFile: false,
    server: { middlewareMode: true, hmr: false, watch: null },
    optimizeDeps: { noDiscovery: true },
    esbuild: { jsx: "automatic" },
  });
  format = await vite.ssrLoadModule("/src/lib/format.ts");
  labels = await vite.ssrLoadModule("/src/lib/labels.ts");
  runs = await vite.ssrLoadModule("/src/lib/runs.ts");
});

test.after(async () => { await vite?.close(); });

test("duration formatting keeps hours and rounds small values up", () => {
  assert.equal(format.formatDuration(0), "0 分钟");
  assert.equal(format.formatDuration(30), "1 分钟");
  assert.equal(format.formatDuration(3600 + 30 * 60), "1 小时 30 分");
});

test("minutes formatting switches to hours past a full hour", () => {
  assert.equal(format.formatMinutes(45), "45 分钟");
  assert.equal(format.formatMinutes(120), "2 小时");
  assert.equal(format.formatMinutes(150), "2 小时 30 分钟");
});

test("runtime messages are localized for the operator", () => {
  assert.match(format.localizeRuntimeText("waiting for device reconnect"), /等待设备重新连接/);
  assert.equal(format.messageOf(new Error("pressure stopped by web console")), "用户在网页控制台停止了任务");
});

test("run status labels cover every phase the console can show", () => {
  for (const status of ["queued", "preparing", "running", "recovering", "waiting_for_device", "resuming", "paused", "needs_manual_resume", "completed", "failed"]) {
    const label = labels.runStatusLabel(status);
    assert.ok(label && label !== "状态未知", `missing label for ${status}`);
  }
  assert.equal(labels.runStatusLabel("unknown-status"), "状态未知");
});

test("active runs exclude terminal tasks", () => {
  assert.equal(runs.isActiveRun({ status: "running" }), true);
  assert.equal(runs.isActiveRun({ status: "paused" }), true);
  assert.equal(runs.isActiveRun({ status: "completed" }), false);
  assert.equal(runs.isActiveRun({ status: "failed" }), false);
});

test("run progress averages the device progress snapshots", () => {
  const run = { devices: ["a", "b"], deviceProgress: { a: { progress: 40 }, b: { progress: 80 } } };
  assert.equal(runs.runProgress(run, []), 60);
  assert.equal(runs.runProgress({ devices: [], deviceProgress: {} }, []), 0);
});

test("parameter validation rejects out-of-range values", () => {
  const base = { duration: 60, cycles: 2, throttle: 250, workers: 2 };
  assert.equal(runs.validRunParameters(base), true);
  assert.equal(runs.validRunParameters({ ...base, duration: 0 }), false);
  assert.equal(runs.validRunParameters({ ...base, workers: 10 }), false);
  assert.equal(runs.validRunParameters({ ...base, duration: 1.5 }), false);
});
