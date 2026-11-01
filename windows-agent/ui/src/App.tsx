import { ApprovalCards } from "./components/ApprovalCard";
import { ConfigForm } from "./components/ConfigForm";
import { StatusBadge } from "./components/StatusBadge";
import { StepTimeline } from "./components/StepTimeline";
import { ThemeToggle } from "./components/ThemeToggle";
import { useAgent } from "./lib/useAgent";

export default function App() {
  const { state, error, busy, run, stop, approve } = useAgent();
  const running = state.status === "running";

  return (
    <div className="mx-auto flex min-h-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Wagent</h1>
          <p className="text-sm text-muted">Điều khiển Windows bằng model local — mọi hành động đều qua guard và nhật ký.</p>
        </div>
        <div className="flex items-center gap-3">
          <StatusBadge status={state.status} />
          <ThemeToggle />
        </div>
      </header>

      {error && <div role="alert" className="rounded-lg border border-bad bg-surface p-3 text-sm text-bad">{error}</div>}

      <section className="rounded-xl border border-line bg-surface p-5" aria-label="Cấu hình và nhiệm vụ">
        <ConfigForm disabled={running || busy} onRun={run} />
      </section>

      <ApprovalCards items={state.pending} onDecide={approve} />

      <section aria-label="Tiến trình" className="space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Tiến trình</h2>
          <button onClick={stop} disabled={!running} className="rounded-lg bg-bad px-4 py-1.5 text-sm font-semibold text-white disabled:opacity-40">DỪNG KHẨN CẤP</button>
        </div>
        <StepTimeline log={state.log} />
        {state.summary && !running && (
          <p className="rounded-lg border border-line bg-surface p-3 text-sm"><b>Tóm tắt của model:</b> {state.summary}
            <span className="block text-xs text-muted">Chỉ là lời model tự báo — hãy kiểm tra kết quả thật.</span></p>
        )}
      </section>
    </div>
  );
}
