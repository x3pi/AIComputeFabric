import type { RunStatus } from "../lib/schema";

const MAP: Record<RunStatus, { label: string; cls: string }> = {
  idle: { label: "Sẵn sàng", cls: "text-muted border-line" },
  running: { label: "Đang chạy", cls: "text-accent border-accent" },
  done: { label: "Hoàn thành", cls: "text-ok border-ok" },
  failed: { label: "Thất bại", cls: "text-bad border-bad" },
  stopped: { label: "Đã dừng", cls: "text-warn border-warn" },
  timeout: { label: "Hết giờ", cls: "text-warn border-warn" },
  max_steps: { label: "Hết số bước", cls: "text-warn border-warn" },
};

export function StatusBadge({ status }: { status: RunStatus }) {
  const m = MAP[status];
  return (
    <span role="status" aria-live="polite" className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium ${m.cls}`}>
      <span aria-hidden className={`h-2 w-2 rounded-full bg-current ${status === "running" ? "animate-pulse" : ""}`} />
      {m.label}
    </span>
  );
}
