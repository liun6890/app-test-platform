# App 测试平台

面向 **Android 本地 APK 多设备稳定性压测** 的一体化平台：在一台 Windows 主机上并行驱动多台真机，
自动完成安装、准备、压测、异常检测、断点续跑与报告归档，并提供本地 Web 控制台。

> 仅用于测试**本机已有 APK** 和**本机 ADB 已连接设备**。不会从网络下载 APK、不会执行 `adb connect`、
> 不会向远程服务分发文件，也不包含静默安装或权限绕过逻辑。

---

## 一、核心能力

| 能力 | 说明 |
|---|---|
| 多设备并行 | 每台设备一个独立 Python 子进程，互不影响；单机实测 7 台并行 |
| 压测引擎 | 内置单文件 Fastbot（`tools/fastbot_android15_patch/fastbot-runtime.apk`），也支持系统 Monkey |
| 安装自动化 | 自动点击厂商安装弹窗（含华为“两段式”确认），安装完成后自动确认并开始测试 |
| 准备场景 | 覆盖安装、清数据、登录、造数、导航、审核页、隐私政策页处理 |
| 崩溃检测 | logcat 实时解析 `FATAL EXCEPTION` / `ANR in` / `Fatal signal`，抓取崩溃页与证据 |
| 崩溃复现 | 基于轨迹回放重跑崩溃路径，输出复现结论 |
| 断点续跑 | `report.json` + `checkpoint.json` + 恢复声明（claim），进程被杀后可续跑 |
| 卡页检测 | 截图指纹判定“页面长时间无变化”，可观察或自动恢复（默认 300 秒阈值） |
| adb 自愈 | 看门狗检测 adb server 崩溃并自动重启；设备骤降自动 `adb reconnect` |
| 心跳与观测 | 从设备端日志增量统计操作数，持续落盘前台组件、引擎存活、最后操作时间 |
| Web 控制台 | 任务、设备、报告、证据浏览；APK 上传、安装确认、单设备恢复/结束 |
| 数据治理 | 审计日志、按天数/容量自动清理、任务日志轮转 |

---

## 二、系统架构

### 运行模型

```
Web 控制台 (React + TypeScript + Vite)
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

- 所有设备共享一个 adb server（默认端口 5037）；
- worker 通过磁盘上的 report / checkpoint / claim 文件协作，不共享内存；
- 后端只做编排、进度聚合与产物读取，不直接操作设备。

### 仓库结构

```
app_press_test/
├── main.py / cli.py / runtime_utils.py   入口与运行时工具
├── core/                                设备、执行、状态、检查点、保留策略等核心模块
├── frontend/                            控制台后端 (server.py) 与 React 前端 (src/)
├── scripts/                             场景脚本、adb 守护、触摸层等运维脚本
├── config/
│   ├── settings.json                    默认配置（唯一内置模板）
│   └── fastbot/                         Fastbot 规则文件 (max.config 等)
├── tools/fastbot_android15_patch/       内置 Fastbot 运行包与构建脚本
├── tests/                               后端单元测试
├── start-all.ps1 / stop-all.ps1         一键启停控制台（含 .cmd 包装）
└── README.md / LICENSE / CONTRIBUTING.md
```

---

## 三、环境要求

- Windows 10/11（主用环境），Linux 亦可
- Python 3.8+（推荐 3.9）
- Node.js 18+（仅构建/开发前端时需要）
- Android Platform Tools，`adb` 已加入 `PATH`（建议 37.0.1 及以上）
- 手机已开启开发者选项与 USB 调试，并已在本机授权
- 本机已有待测 APK，且对 APK 与设备拥有合法测试权限
- 设备系统：Android 5.0–16（已适配小米/红米、华为、荣耀、vivo、OPPO 等定制系统）

---

## 四、安装

```powershell
cd app_press_test

# 后端依赖
python -m pip install -r requirements.txt

# 前端依赖（仅构建或开发时）
cd frontend
npm install
cd ..
```

首次使用或更换设备后，把内置 Fastbot 运行包推送到设备：

```powershell
python cli.py sync-fastbot --devices <序列号1>,<序列号2>
```

---

## 五、快速开始

### 5.1 一键启停控制台

```powershell
# 启动后端 + 前端（默认 http://127.0.0.1:8765）
.\start-all.ps1

# 停止
.\stop-all.ps1
```

Windows 下也可直接双击 `start-all.cmd` / `stop-all.cmd`。

### 5.2 手动启动

```powershell
# 后端
cd frontend
python server.py --host 127.0.0.1 --port 8765

# 前端开发模式（另一个终端）
npm run dev          # http://127.0.0.1:5173 ，/api 代理到 8765

# 或构建静态文件，由后端直接提供
npm run build        # 生成 dist/，刷新页面即可
```

> 如果后端与 worker 需要使用不同的 Python 解释器，设置环境变量
> `APP_PRESS_TEST_RUNTIME_PYTHON=<python.exe 路径>`。

控制台典型流程：

1. 上传 APK（自动解析包名）；
2. 选择设备、时长、周期、并发数；
3. 启动任务 —— 平台自动安装、点击厂商安装页、校验 md5、确认并开始压测；
4. 观察进度、操作数、日志与证据；
5. 设备异常时可单台「恢复」或「结束」，不影响同批其他设备。

### 5.3 命令行

```powershell
# 查看本机已连接设备
python cli.py devices

# 完整压测（--devices 可省略，默认取配置中的 devices）
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

---

## 六、配置说明

`config/settings.json` 是唯一内置模板，控制台启动任务时会基于它生成任务级配置。
关键段落：

| 配置段 | 关键项 | 说明 |
|---|---|---|
| `app` | `package_name`、`apk_path` | 被测包名与 APK 路径（控制台运行时由上传结果覆盖） |
| `test` | `fastbot_duration_minutes`、`max_cycles`、`fastbot_throttle_ms`、`stop_on_crash`、`clear_app_data_each_cycle` | 压测时长、轮数、操作间隔、崩溃是否停、每轮是否清数据 |
| `parallel` | `max_workers` | 并行设备数（上限 32，建议 ≤ 8） |
| `resume` | `max_attempts`、`checkpoint_interval_seconds`、`wait_for_device_seconds` | 恢复重试次数、检查点间隔、等待设备回连时间 |
| `screenshot` | `enabled`、`interval_seconds` | 过程截图存盘（关闭后仍保留卡页采样与失败证据） |
| `stall_watchdog` | `enabled`、`mode`、`sample_interval_seconds`、`unchanged_seconds`、`max_recoveries_per_cycle` | 卡页检测：默认 30 秒采样、300 秒无变化判卡、每轮最多恢复 1 次 |
| `fastbot` | `classpath`、`main_class`、`expected_monkeyq_md5`、`extra_args`、`event_percentages` | 引擎路径、入口类、完整性校验、随机事件比例 |
| `devices` | `[{serial, alias}]` | 默认设备列表 |
| `retention` | `max_age_days`、`max_total_gb` | 产物保留策略 |
| `paths` / `logging` | `reports_dir`、`log_dir`、`level` | 产物与日志位置 |

启动页策略（控制台“通用 APK 模式”）：

- 有审核门 → 点「审核中」→「确定」；
- 有隐私政策 → 点同意；
- 都没有 → 直接进入主页；
- 非首次启动不再重复等待启动页。

---

## 七、一次压测的生命周期

1. **安装**：按需 `adb install`；自动识别并点击厂商安装页（vivo / 小米 / 华为 / 荣耀），
   安装完成后平台自动校验 md5 并确认，无需人工点击；
2. **准备**：清数据（可选）→ 启动 → 处理审核门 / 隐私页 → 登录 / 导航 → 校验前台稳定；
3. **压测**：停止 uiautomator2 Watcher → 启动 Fastbot → 并行采集 logcat、截图、操作数；
4. **检测**：崩溃、ANR、无响应、卡页、前台丢失、引擎退出；
5. **恢复**：按策略重开 App / 重跑准备 / 重启压测，每步写入 checkpoint；
6. **收尾**：停止引擎 → 收集设备端日志 → 生成 report.json 与证据 → 释放设备；
7. **续跑**：任务中断后可用原 run-id 继续，未完成设备自动恢复。

---

## 八、稳定性机制

| 机制 | 行为 |
|---|---|
| 检查点 | 每 10 秒写 `checkpoint.json`（进度、操作数、前台组件、引擎存活） |
| 增量心跳 | 只读取设备端日志**新增字节**统计操作数，避免全量扫描 |
| 卡页检测 | 30 秒采样，300 秒无变化判卡；模式 `observe` / `recover_once` / `recover` |
| 前台守卫 | 0.5 秒轮询前台组件，App 被弹窗顶掉时恢复 |
| adb 看门狗 | 每 10 秒探测 adb，连续失败自动重启；设备数骤降自动 `adb reconnect` |
| 安装弹窗 | 标签识别（非坐标），只点“继续安装/安装/允许”，绝不点“拒绝/取消” |
| 安装确认 | 校验所有设备 md5 后自动确认；失败或超时保留手动确认入口 |
| 安装页避让 | 安装器页面未退出时，准备阶段最多等待 90 秒，避免误判“未到前台” |
| 任务收尾 | 超过计划时长 + 10 分钟仍有设备待人工处理时，自动标记 `completed_with_gaps` |
| 日志轮转 | 单任务日志超过 20 MB 自动轮转 |
| 数据保留 | 启动时与手动 `cleanup` 按天数/容量清理历史产物 |

> 说明：键盘遮挡检测已移除。测试设备统一使用干净输入法，持续探测会因厂商输入法残留标志产生误判；
> 框架仅在开始压测前做一次“如键盘可见则收起”的处理。

---

## 九、产物与数据

运行数据全部保存在本机，**不会上传到任何远程服务**：

```
frontend/runtime-local/
├── uploads/                  # 上传的 APK
├── runs/<job-id>/
│   ├── console.log           # 任务总日志
│   ├── config.json           # 本次任务生效配置
│   ├── job.json              # 任务状态快照
│   └── reports/<job>_<serial>/
│       ├── report.json       # 设备报告（状态、统计、崩溃、覆盖）
│       ├── checkpoint.json   # 断点续跑检查点
│       ├── fastbot.log       # 压测引擎日志
│       ├── logcat.log        # 设备日志
│       └── evidence/         # 崩溃截图、UI dump、轨迹等证据
└── audit.jsonl               # 操作审计（启动/停止/恢复/安装确认等）
```

`.gitignore` 已排除 `runtime-local/`、`reports/`、`logs/`、`dist/`、`node_modules/`、`*.apk` 等运行时数据与构建产物。

---

## 十、测试

```powershell
# 后端单元测试（494 个）
python -m unittest discover -s tests

# 控制台后端测试（93 个）
cd frontend
python -m unittest tests.test_server

# 前端测试（node:test，14 个）
cd frontend
npm test
```

提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 十一、常见问题

### 设备反复掉线 / 全部离线

1. `python cli.py devices` 确认 adb 是否正常；
2. 检查是否同时存在多个 adb server（只保留默认 5037）；
3. 若 `adb.exe` 崩溃（事件日志 `0xc000001d`），看门狗会自动重启，也可手动 `adb start-server`；
4. 设备未重新枚举时，重新插拔 USB。

### 安装失败 `INSTALL_FAILED_USER_RESTRICTED`

小米/HyperOS 需要手动开启 **开发者选项 → USB 安装**（可能要求登录小米账号）；vivo/HONOR 需允许
“USB 安装/未知来源安装”。平台会自动点击安装页的“继续安装”，但系统级开关必须人工开启一次。

### 安装页倒计时自动拒绝

平台优先点击“继续安装/允许”，不会点击“拒绝/取消”。若仍被拒绝，检查上述系统开关并重试。

### 华为安装需要点两次

华为会先弹系统安装器、再弹应用市场风险确认页，平台会自动连续点击「继续安装」。
若出现**人机验证（Captcha）**，属于无法自动化的环节，会停下等待人工处理，不会乱点。

### 任务状态停留在“准备中”

已修复：报告文件出现后证据缓存会立即失效，任务状态会自动切换为 `running`。

### 任务长时间停在“需人工恢复”

超过计划时长 + 10 分钟后会自动结束为 `completed_with_gaps`；也可在控制台单台「结束」或「恢复」。

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
- 卡页检测基于截图，视觉上长期不变的页面会周期性触发恢复（默认 300 秒阈值可调）；
- `adb.exe` 偶发崩溃（`0xc000001d`）只能靠看门狗自愈，根因尚未定位；
- 厂商安装流程的“人机验证”页面无法自动化，需人工兜底；
- 报告为本地 JSON/HTML，未接入外部质量平台。

---

## 十四、许可与合规

- 本项目代码采用 [MIT License](LICENSE)；
- `tools/fastbot_android15_patch/` 为第三方 Fastbot 运行包及其补丁源码，版权归原项目所有，
  公开分发前请确认你拥有相应授权；
- 本项目仅供获得授权的测试人员对**自有 APK** 和**自有设备**进行稳定性测试，
  请遵守当地法律法规与设备厂商条款，不要用于未授权的应用或设备。
