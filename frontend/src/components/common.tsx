import { Activity, AlertTriangle, LoaderCircle, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";
import type { DeviceStatus } from "../types";
import { statusMeta } from "../lib/labels";

export function StatusBadge({ status }: { status: DeviceStatus }) {
  const meta = statusMeta[status] || { label: status, className: "status-preparing" };
  return <span className={`status-badge ${meta.className}`}><span className="status-dot" />{meta.label}</span>;
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <button className={`toggle ${checked ? "is-on" : ""}`} type="button" role="switch" aria-checked={checked} aria-label={label} onClick={onChange}>
      <span />
    </button>
  );
}

export function ServiceBanner({ message, connected, onRetry, refreshing }: { message: string; connected: boolean; onRetry: () => void; refreshing: boolean }) {
  const Icon = connected ? AlertTriangle : WifiOff;
  return (
    <div className="service-banner">
      <Icon size={20} />
      <div><strong>{connected ? "本地数据刷新失败" : "本地服务不可用"}</strong><span>{message}</span></div>
      <button className="secondary-button" type="button" onClick={onRetry} disabled={refreshing}><RefreshCw size={16} className={refreshing ? "spin" : ""} />重试</button>
    </div>
  );
}

export function PageLoading() {
  return <div className="page-loading"><LoaderCircle size={27} className="spin" /><strong>正在读取本地状态</strong></div>;
}

export function EmptyState({ icon: Icon, title, note, action }: { icon: typeof Activity; title: string; note: string; action?: ReactNode }) {
  return <div className="empty-state"><Icon size={30} /><strong>{title}</strong><span>{note}</span>{action}</div>;
}
