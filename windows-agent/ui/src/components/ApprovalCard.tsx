import type { Approval } from "../lib/schema";

type Props = { items: Approval[]; onDecide: (id: string, allow: boolean) => void };

export function ApprovalCards({ items, onDecide }: Props) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Cần phê duyệt" className="space-y-3">
      {items.map((p) => (
        <div key={p.id} role="alertdialog" aria-labelledby={`ap-${p.id}`} className="rounded-xl border-2 border-warn bg-surface p-4">
          <h3 id={`ap-${p.id}`} className="font-semibold text-warn">Cần bạn phê duyệt</h3>
          <p className="mt-1 text-sm">{p.reason}</p>
          <pre className="mt-2 overflow-auto rounded-lg bg-bg p-2 font-mono text-xs">{p.action.tool} {JSON.stringify(p.action.args)}</pre>
          <div className="mt-3 flex gap-2">
            <button onClick={() => onDecide(p.id, true)} className="rounded-lg bg-ok px-4 py-2 text-sm font-semibold text-white">Cho phép</button>
            <button onClick={() => onDecide(p.id, false)} className="rounded-lg border border-bad px-4 py-2 text-sm font-semibold text-bad">Từ chối</button>
          </div>
        </div>
      ))}
    </section>
  );
}
