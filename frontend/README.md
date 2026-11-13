# Fastbot Console

项目内独立的本地网页控制台。前端和服务端都位于本目录，不修改现有
`core/`、`main.py` 或 `cli.py`。网页生成的测试配置固定使用 Fastbot。

## 启动

推荐在项目根目录双击 `start-all.cmd`，它会同时启动 API 和 Vite 前端，
等待两端健康检查通过后自动打开 `http://127.0.0.1:5173/`。再次运行不会重复
创建服务。需要停止时双击 `stop-all.cmd`；运行日志位于
`frontend/runtime-local/launcher/`。

启动器检测到服务已运行时只打开现有页面，不会重启服务、操作 ADB 或改动
正在执行的测试。`stop-all.cmd` 在存在运行中任务时也会拒绝停止服务。

也可以在 PowerShell 中运行：

```powershell
.\start-all.ps1
```

下列方式会先构建前端，再由 Python 服务同时提供 API 和静态页面：

在 PowerShell 中运行：

```powershell
cd frontend
.\start.ps1
```

打开 `http://127.0.0.1:8765/`。

`start.ps1` 会先构建 React 页面，再启动本地 Python 服务。服务只监听
`127.0.0.1`，不会向网络上传 APK、设备数据或测试报告。

上传 APK、任务状态、运行日志、报告、截图和崩溃证据统一写入
`APP_PRESS_TEST_OUTPUT_ROOT`。Windows 默认目录为
`E:\app_press_test\frontend\runtime`。

## 使用顺序

1. 在“设备”页面确认 ADB 设备已授权且处于就绪状态。
2. 选择设备；需要更新安全版 JAR 时点击“同步 Fastbot”。
3. 在“测试配置”页面上传本地 APK；控制台会读取并校验 manifest 包名，匹配已有项目流程或切换到新包通用模式。
4. 设置时长、轮数、并发和监控选项，点击“启动 Fastbot”。启动前会检查设备、Fastbot 文件和 MD5。
5. 如果设备未安装目标 APK，控制台会把任务停在“等待人工安装”：在手机系统安装页手动点击“安装/继续安装”，然后回到“运行监控”点击“已完成安装，继续”。平台会用 `pm path` 再次确认，未检测到真实安装不会放行。
6. 安装确认后，框架按原有规则清除应用数据、处理审核页、执行场景导航，再启动 Fastbot。可在“运行监控”查看实时日志并停止任务。
7. 在“报告”查看设备报告、问题结论和崩溃证据，可复制或导出开发定位报告。

## 开发

分别启动 API 和 Vite：

```powershell
python server.py
npm run dev
```

Vite 地址为 `http://127.0.0.1:5173/`，`/api` 自动代理到 `8765`。

## 验证

```powershell
npm run build
python -m unittest discover -s tests -v
```
