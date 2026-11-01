import { useState } from "react";
import { RunRequest } from "../lib/schema";

type Props = { disabled: boolean; onRun: (r: RunRequest) => void };

const field = "w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm placeholder:text-muted";

export function ConfigForm({ disabled, onRun }: Props) {
  const [url, setUrl] = useState(() => localStorage.getItem("wagent_url") ?? "http://10.42.0.1:18090/v1");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [task, setTask] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const parsed = RunRequest.safeParse({ url, model: model || undefined, key: key || undefined, task });
    if (!parsed.success) {
      const out: Record<string, string> = {};
      for (const i of parsed.error.issues) out[String(i.path[0])] = i.message;
      setErrors(out);
      return;
    }
    setErrors({});
    try { localStorage.setItem("wagent_url", url); } catch { /* bỏ qua: storage bị chặn */ }
    onRun(parsed.data);
  };

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <div>
        <label htmlFor="url" className="mb-1 block text-sm font-medium">URL model (OpenAI-compatible)</label>
        <input id="url" className={field} value={url} onChange={(e) => setUrl(e.target.value)} aria-invalid={!!errors.url} aria-describedby={errors.url ? "url-err" : undefined} />
        {errors.url && <p id="url-err" className="mt-1 text-sm text-bad">{errors.url}</p>}
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor="model" className="mb-1 block text-sm font-medium">Model <span className="text-muted">(tuỳ chọn)</span></label>
          <input id="model" className={field} value={model} onChange={(e) => setModel(e.target.value)} />
        </div>
        <div>
          <label htmlFor="key" className="mb-1 block text-sm font-medium">API key <span className="text-muted">(chỉ lưu trong bộ nhớ)</span></label>
          <input id="key" type="password" autoComplete="off" className={field} value={key} onChange={(e) => setKey(e.target.value)} />
        </div>
      </div>
      <div>
        <label htmlFor="task" className="mb-1 block text-sm font-medium">Nhiệm vụ</label>
        <textarea id="task" rows={3} className={field} placeholder="Ví dụ: Phân loại file trong thư mục in/ theo đuôi vào out/…" value={task} onChange={(e) => setTask(e.target.value)} aria-invalid={!!errors.task} />
        {errors.task && <p className="mt-1 text-sm text-bad">{errors.task}</p>}
      </div>
      <button type="submit" disabled={disabled} className="rounded-lg bg-accent px-5 py-2 text-sm font-semibold text-white transition-opacity disabled:cursor-not-allowed disabled:opacity-50">
        {disabled ? "Đang chạy…" : "Chạy"}
      </button>
    </form>
  );
}
