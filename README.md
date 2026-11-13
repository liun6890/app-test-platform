# App 测试平台

面向 **Android 本地 APK 多设备稳定性压测** 的一体化平台：在一台 Windows 主机上并行驱动多台真机，
完成安装、准备、压测、崩溃检测、断点续跑、报告归档，并提供一个本地 Web 控制台。

> 仅用于测试**本机已有 APK** 和**本机 ADB 已连接设备**。不会从网络下载 APK、不会执行 `adb connect`、
> 不会向远程服务分发文件，也不包含静默安装或权限绕过逻辑。

---

## 一、核心能力

| 能力 | 说明 |
|---|---|
| 多设备并行 | 每台设备一个独立 Python 子进程，互不影响；单机实测 7 台并行 |
| 压测引擎 | 内置单文件 Fastbot（`tools/fastbot_android15_patch/fastbot-runtime.apk`），也支持系统 Monkey |
| 准备场景 | 安装/覆盖安装、清数据、登录、造数、导航、审核页、隐私政策页处理 |
| 崩溃检测 | logcat 实时解析 `FATAL EXCEPTION` / `ANR in` / `Fatal signal`，并抓取崩溃页与证据 |
| 崩溃复现 | 基于轨迹回放重跑崩溃路径，输出复现结论 |
| 断点续跑 | `report.json` + `checkpoint.json` + 恢复声明（claim），进程被杀后可续跑 |
| 卡页检测 | 截图指纹 + 页面/进程状态判定“卡住”，可观察或自动恢复 |
| adb 自愈 | 后台看门狗检测 adb server 崩溃并自动重启、设备骤降自动 reconnect |
| 安装治理 | 自动点击厂商安装弹窗（继续安装/允许），绝不点击拒绝/取消 |
| 输入法治理 | 兼容厂商输入法残留标志；压测前一次性收起键盘（不做循环检测） |
| 心跳与观测 | 操作数、最后操作时间、前台组件、Fastbot 存活状态持续落盘 |
| Web 控制台 | 任务、设备、报告、证据浏览，支持 APK 上传、安装确认、单设备恢复/结束 |
| 数据治理 | 审计日志、按天数/容量自动清理、日志轮转 |

---

## 二、系统架构

### 运行模型

```
Web 控制台 (React + TS, Vite)
        │  HTTP /api
        ▼
本地后端 (stdlib ThreadingHTTPServer, 127.0.0.1:8765)
        │  subprocess.Popen
        ▼
CLI 主进程 (cli.py run)
        │  spawn：每台设备一个 worker
        ├── worker A ── adb + uiautomator2 + Fastbot ── 设备 1
        ├── worker B ── adb + uiautomator2 + Fastbot ── 设备 2
        └── worker N ── ...
                │
                ▼
        runtime-local/runs/<job-id>/
          reports/<job>_<serial>/
            report.json / checkpoint.json / fastbot.log / logcat.log / evidence/
```

- 所有设备共享一个 adb server（默认 5037）。
- worker 之间通过磁盘上的 report / checkpoint / claim 文件协作，不共享内存。
- 后端只做编排、进度聚合和产物读取，不直接操作设备。

### 目录结构

```
app_press_test/
├── main.py                 # 压测主流程：准备、压测、证据、恢复、崩溃复现
├── cli.py                  # 命令行入口
├── core/                   # 设备、执行、状态、检查点、保留策略等核心模块
├── scripts/                # 场景脚本（准备场景、Fastbot 补丁辅助）
├── config/                 # 58 套配置模板（settings.json 为默认）
├── frontend/
│   ├── server.py           # 控制台后端（REST API）
│   ├── console_store.py    # 任务持久化与缓存
│   ├── console_scenario.py # 通用 APK 模式的场景/审核策略生成
│   └── src/                # React 控制台
├── tools/fastbot_android15_patch/  # 内置 Fastbot 运行包与补丁工具
└── tests/                  # 后端单元测试（484 个）
```

---

## 三、环境要求

- Windows 10/11（主用环境）、Linux 亦可
- Python 3.8+（推荐 3.9）
- Node.js 18+ 与 pnpm/npm（仅构建/开发前端时需要）
- Android Platform Tools（`adb` 在 `PATH` 中，建议 37.0.1 及以上）
- 手机开启开发者选项与 USB 调试，并已在手机上授权本机
- 本机已有待测 APK，且对 APK 与设备拥有合法测试权限
- 设备系统：Android 5.0–16（厂商定制系统已适配小米/华为/荣耀/vivo/OPPO）

---

## 四、安装

```powershell
cd app_press_test

# 后端依赖
python -m pip install -r requirements.txt

# 前端依赖（仅构建/开发时）
cd frontend
npm install
cd ..
```

把内置 Fastbot 运行包推送到每台设备（首次或换机后）：

```powershell
python cli.py sync-fastbot --devices <序列号1>,<序列号2>
```

---

## 五、快速开始

### 5.1 命令行

```powershell
# 查看本机已连接设备
python cli.py devices

# 完整压测（设备列表可省略，默认取配置中的 devices）
python cli.py run --config config/settings.json --devices <序列号1>,<序列号2>

# 只安装 APK 并执行准备场景
python cli.py prepare --config config/settings.json --device <序列号>

# 对已准备好的应用只跑 Fastbot
python cli.py fastbot --config config/settings.json --device <序列号> --duration 30

# 安全停止并校验设备端已无 Fastbot/Monkey
python cli.py stop --devices <序列号1>,<序列号2>

# 查看最新报告（--json 输出完整内容）
python cli.py report --latest

# 按保留策略清理历史产物
python cli.py cleanup --max-age-days 7 --max-total-gb 50
```

### 5.2 Web 控制台

```powershell
# 1) 启动后端（默认 127.0.0.1:8765）
cd frontend
python server.py --host 127.0.0.1 --port 8765

# 2) 开发模式启动前端（另开一个终端）
npm run dev          # http://127.0.0.1:5173 ，/api 代理到 8765

# 3) 或构建静态文件，由后端直接提供
npm run build        # 生成 dist/，刷新页面即可
```

Windows 下也可直接运行 `frontend/start.ps1`（按脚本内说明配置 Python 路径）。

> 如果后端由别的 Python 解释器启动、而 worker 需要用另一个解释器（例如安装在 E: 盘时权限受限），
> 设置环境变量 `APP_PRESS_TEST_RUNTIME_PYTHON=<python.exe 路径>`。

控制台主要操作：

1. 上传 APK（自动解析包名）；
2. 选择设备、时长、周期、并发数；
3. 启动任务 → 安装（必要时在设备上点“继续安装”，平台也会自动点）；
4. 观察进度、操作数、日志、证据；
5. 设备异常时可单台「恢复」或「结束」，不影响同批其他设备。

---

## 六、配置说明

`config/settings.json` 是默认配置，`config/` 下另有 58 套模板。关键段落：

| 配置段 | 关键项 | 说明 |
|---|---|---|
| `app` | `package_name`、`apk_path` | 被测包名与 APK 路径（控制台运行时由上传结果覆盖） |
| `test` | `fastbot_duration_minutes`、`max_cycles`、`fastbot_throttle_ms`、`stop_on_crash`、`clear_app_data_each_cycle` | 压测时长、轮数、操作间隔、崩溃是否停、每轮是否清数据 |
| `parallel` | `max_workers` | 并行设备数（上限 32，建议 ≤ 8） |
| `resume` | `max_attempts`、`checkpoint_interval_seconds`、`wait_for_device_seconds` | 恢复重试次数、检查点间隔、等待设备回连时间 |
| `screenshot` | `enabled`、`interval_seconds` | 过程截图存盘（关闭后仍保留卡页检测采样与失败证据） |
| `stall_watchdog` | `enabled`、`mode`、`sample_interval_seconds`、`unchanged_seconds`、`max_recoveries_per_cycle` | 卡页检测：默认每 30 秒采样、300 秒无变化才判卡、每轮最多恢复 1 次 |
| `fastbot` | `classpath`、`main_class`、`expected_monkeyq_md5`、`extra_args`、`event_percentages` | 引擎路径、入口类、完整性校验、随机事件比例 |
| `devices` | `[{serial, alias}]` | 默认设备列表 |
| `retention` | `max_age_days`、`max_total_gb` | 产物保留策略 |
| `paths` / `logging` | `reports_dir`、`log_dir`、`level` | 产物与日志位置 |

审核门 / 隐私页策略由控制台按“通用 APK 模式”生成：

- 启动页有审核门 → 点「审核中」→「确定」；
- 有隐私政策 → 点同意；
- 都没有 → 直接进入主页；
- 非首次启动不再重复等待启动页。

---

## 七、一次压测的生命周期

1. **安装**：按需 `adb install`，自动点击厂商安装弹窗（继续安装/允许）。
2. **准备**：清数据（可选）→ 启动 → 处理审核门/隐私页 → 登录/导航 → 校验前台稳定。
3. **压测**：停止 uiautomator2 Watcher → 启动 Fastbot → 并行采集 logcat / 截图 / 操作数。
4. **检测**：崩溃、ANR、无响应、卡页、前台丢失、引擎退出。
5. **恢复**：按策略重开 App / 重跑准备 / 重启压测；每步写入 checkpoint。
6. **收尾**：停止引擎 → 收集设备端日志 → 生成 report.json 与证据 → 释放设备。
7. **续跑**：任务异常中断后，可用原 run-id 继续，未完成设备自动恢复。

---

## 八、稳定性机制

| 机制 | 行为 |
|---|---|
| 检查点 | 每 10 秒写 `checkpoint.json`（进度、操作数、前台组件、引擎存活） |
| 心跳 | 从设备端 Fastbot 日志的**新增字节**统计操作数，避免全量扫描 |
| 卡页检测 | 30 秒采样截图指纹，300 秒无变化才判定卡住；可 observe / recover_once / recover |
| 前台守卫 | 0.5 秒轮询前台组件，App 被弹窗顶掉时恢复 |
| adb 看门狗 | 每 10 秒探测 adb，连续失败自动重启；设备数骤降自动 `adb reconnect` |
| 安装弹窗 | 识别 MIUI/HyperOS/vivo/HONOR 安装界面，只点“继续安装”类按钮 |
| 任务收尾 | 超过计划时长 + 10 分钟仍有设备待人工处理时，自动标记 `completed_with_gaps` |
| 日志轮转 | 单任务日志超过 20 MB 自动轮转 |
| 数据保留 | 启动时与手动 `cleanup` 按天数/容量清理历史产物 |

---

## 九、产物与数据

运行数据全部保存在本机，**不会上传到任何远程服务**：

```
frontend/runtime-local/
├── uploads/                  # 上传的 APK
├── runs/<job-id>/
│   ├── console.log           # 任务总日志
│   ├── config.json           # 本次任务生效配置
│   └── reports/<job>_<serial>/
│       ├── report.json       # 设备报告（状态、统计、崩溃、覆盖）
│       ├── checkpoint.json   # 断点续跑检查点
│       ├── fastbot.log       # 压测引擎日志
│       ├── logcat.log        # 设备日志
│       └── evidence/         # 崩溃截图、UI dump、轨迹等证据
└── audit.jsonl               # 操作审计（启动/停止/恢复/安装确认等）
```

`.gitignore` 已排除 `runtime-local/`、`reports/`、`logs/`、`dist/`、`*.apk` 等运行时数据与构建产物，
仓库中不包含任何测试数据或日志。

---

## 十、测试

```powershell
# 后端单元测试（484 个）
python -m unittest discover -s tests

# 控制台后端测试（89 个）
cd frontend
python -m unittest tests.test_server

# 前端测试（node:test，14 个）
npm test
```

---

## 十一、常见问题

### 设备反复掉线 / 全部离线

1. `python cli.py devices` 确认 adb 是否正常；
2. 检查是否同时存在多个 adb server（只保留默认 5037）；
3. 若 `adb.exe` 崩溃（事件日志 `0xc000001d`），看门狗会自动重启，也可手动 `adb start-server`；
4. 设备未重新枚举时，重新插拔 USB 或重启 adb server。

### 安装失败 `INSTALL_FAILED_USER_RESTRICTED`

小米/HyperOS 需要手动打开 **开发者选项 → USB 安装**（可能要求登录小米账号）；vivo/HONOR 需允许
“USB 安装/未知来源安装”。平台已自动点击安装页的“继续安装”，但系统级开关必须人工开启一次。

### 安装页倒计时自动拒绝

平台会优先点击“继续安装/允许”，不会点击“拒绝/取消”。若仍被拒绝，检查上述系统开关并重试。

### 键盘遮挡页面

平台已移除持续输入法检测（厂商输入法会残留可见标志导致误判），只在开始压测前做一次收起键盘；
若某 App 仍被键盘遮挡，检查是否系统级第三方输入法弹出了悬浮窗。

### 任务长时间停在“需人工恢复”

超过计划时长 + 10 分钟后会自动结束为 `completed_with_gaps`；也可在控制台单台「结束」或「恢复」。

### 控制台很慢

后端对设备探测、报告摘要、任务证据做了缓存；前端空闲时 10 秒、运行中 4 秒轮询。
如设备很多，可适当增大 `stall_watchdog.sample_interval_seconds`、关闭过程截图。

---

## 十二、REST API（节选）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/devices` | 设备列表与状态 |
| GET | `/api/runs` | 任务列表 |
| POST | `/api/runs` | 启动任务 |
| GET | `/api/runs/<id>` | 任务详情与设备结果 |
| POST | `/api/apks` | 上传 APK（返回包名） |
| POST | `/api/runs/<id>/stop` | 停止任务 |
| POST | `/api/runs/<id>/confirm-install` | 确认安装完成 |
| POST | `/api/jobs/<id>/devices/<serial>/resume` | 单设备恢复 |
| POST | `/api/jobs/<id>/devices/<serial>/end` | 单设备结束 |
| POST | `/api/reports/<id>/replay` | 崩溃复现 |

---

## 十三、已知限制

- 单机运行，不支持多主机分布式调度；
- 一个 adb server 承载全部设备（多 server 拆分受 USB 独占限制，未实现）；
- 卡页检测基于截图，视觉上长期不变的页面会周期性触发恢复；
- `adb.exe` 偶发崩溃（`0xc000001d`）只能靠看门狗自愈，根因尚未定位；
- 报告为本地 JSON/HTML，未接入外部质量平台。

---

## 十四、使用声明

本项目仅供获得授权的测试人员对**自有 APK** 和**自有设备**进行稳定性测试。
请遵守当地法律法规与设备厂商条款，不要用于未授权的应用或设备。
