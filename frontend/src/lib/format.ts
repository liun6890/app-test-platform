import { ApiError } from "../api";

export function localizeRuntimeText(value: unknown): string {
  return String(value || "")
    .replace(/device test ended manually from web console/gi, "已从网页控制台手动结束该设备测试")
    .replace(/pressure stopped by web console/gi, "用户在网页控制台停止了任务")
    .replace(/device resume is already in progress/gi, "设备正在继续运行，请勿重复操作")
    .replace(/device resume is already queued/gi, "设备已加入继续运行队列")
    .replace(/device already has a live worker/gi, "设备已有运行中的执行进程")
    .replace(/device transport is unavailable; resume will wait for reconnect/gi, "设备连接不可用，将在设备回连后继续")
    .replace(/device transport is temporarily unavailable/gi, "设备连接暂时不可用")
    .replace(/waiting for device reconnect/gi, "等待设备重新连接")
    .replace(/pressure engine preflight failed/gi, "压测引擎预检失败")
    .replace(/device control cleanup failed: Fastbot control was released but uiautomator2 could not be restored/gi, "设备控制清理失败：压测进程已释放，但自动化控制服务未恢复")
    .replace(/wait_for_device_timeout: device (\S+) was not reconnected within (\d+) seconds/gi, "等待设备回连超时：设备 $1 在 $2 秒内未重新连接")
    .replace(/ADB device transport is temporarily unavailable/gi, "设备调试连接暂时不可用")
    .replace(/Unable to stop Fastbot after the device disconnected/gi, "设备断开后无法停止压测进程")
    .replace(/uiautomator2 control was temporarily unavailable/gi, "自动化控制服务暂时不可用")
    .replace(/Fastbot runtime APK checksum mismatch on/gi, "设备端压测运行文件校验值不一致：")
    .replace(/previous device control could not be verified released/gi, "无法确认设备上的旧控制进程已释放")
    .replace(/previous Fastbot\/uiautomator2 control could not be verified released/gi, "无法确认设备上的旧压测控制进程已释放");
}

export function messageOf(error: unknown): string {
  return error instanceof Error ? localizeRuntimeText(error.message) : "操作失败";
}

export function serviceIsReachable(error: unknown): boolean {
  return !(error instanceof ApiError && error.status === 0);
}

export function formatDate(value: string | null | undefined, compact = false): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", compact
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
  ).format(date);
}

export function formatDuration(seconds: number): string {
  if (!seconds) return "0 分钟";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours) return `${hours} 小时 ${minutes} 分`;
  return `${Math.max(1, minutes)} 分钟`;
}

export function formatMinutes(minutes: number): string {
  if (minutes >= 60 && minutes % 60 === 0) return `${minutes / 60} 小时`;
  if (minutes >= 60) return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  return `${minutes} 分钟`;
}
