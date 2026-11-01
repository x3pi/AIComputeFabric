import { useEffect, useRef } from "react";
import { parseStepLine, toolRisk, type Risk } from "../lib/schema";

const RISK: Record<Risk, string> = { low: "border-line", medium: "border-warn/60", high: "border-bad" };

export function StepTimeline({ log }: { log: string[] }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "nearest" }); }, [log.length]);

  if (log.length === 0) {
    return <p className="rounded-lg border border-dashed border-line p-6 text-center text-sm text-muted">Chưa có bước nào. Nhập nhiệm vụ rồi bấm Chạy.</p>;
  }
  return (
    <ol className="space-y-2" aria-label="Các bước của agent">
      {log.map((line, i) => {
        const s = parseStepLine(line);
        if (!s) return <li key={i} className="rounded-lg border border-line p-3 font-mono text-xs text-muted">{line}</li>;
        const failed = /^(LOI|TU CHOI)/.test(s.result);
        return (
          <li key={i} className={`rounded-lg border-l-4 border border-line bg-surface p-3 ${RISK[toolRisk(s.tool)]}`}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-mono text-sm font-semibold">{s.n}. {s.tool}</span>
              <span className="truncate font-mono text-xs text-muted">{JSON.stringify(s.args)}</span>
            </div>
            <pre className={`mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words font-mono text-xs ${failed ? "text-bad" : "text-muted"}`}>{s.result}</pre>
          </li>
        );
      })}
      <div ref={end} />
    </ol>
  );
}
