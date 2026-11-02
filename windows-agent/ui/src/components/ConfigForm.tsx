import { useState } from "react";
import { api } from "../lib/api";
import { RunRequest } from "../lib/schema";

type Props = {
  disabled: boolean;
  onRun: (r: RunRequest) => void;
  /** Lấy danh sách model từ server; mặc định gọi /api/models của Wagent (tiêm vào khi test). */
  loadModels?: (url: string, key?: string) => Promise<string[]>;
};

const field = "w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm placeholder:text-muted";

export function ConfigForm({ disabled, onRun, loadModels = api.models }: Props) {
  const [url, setUrl] = useState(() => localStorage.getItem("wagent_url") ?? "http://10.42.0.1:18090/v1");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [task, setTask] = useState("");
  const [models, setModels] = useState<string[] | null>(null);
  const [mState, setMState] = useState<{ busy: boolean; error: string }>({ busy: false, error: "" });
  const [errors, setErrors] = useState<Record<string, string>>({});

  const fetchModels = async () => {
    setMState({ busy: true, error: "" });
    try {
      const list = await loadModels(url, key || undefined);
      setModels(list);
      setModel((m) => (list.includes(m) ? m : ""));
      setMState({ busy: false, error: list.length ? "" : "Server không báo model nào (có thể chỉ chạy một model mặc định)." });
    } catch (e) {
      setModels(null);
      setMState({ busy: false, error: e instanceof Error ? e.message : "Không tải được danh sách model" });
    }
  };

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
          <div className="flex gap-2">
            {models && models.length > 0 ? (
              <select id="model" className={field} value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="">(mặc định của server)</option>
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            ) : (
              <input id="model" className={field} value={model} onChange={(e) => setModel(e.target.value)} placeholder="để trống = mặc định" />
            )}
            <button type="button" onClick={fetchModels} disabled={mState.busy} className="shrink-0 rounded-lg border border-line px-3 py-2 text-sm disabled:opacity-50">
              {mState.busy ? "Đang tải…" : "Tải danh sách"}
            </button>
          </div>
          {mState.error && <p role="status" className="mt-1 text-sm text-bad">{mState.error}</p>}
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
