import { RunRequest, State } from "./schema";

const token = new URLSearchParams(location.search).get("t") ?? sessionStorage.getItem("wagent_t") ?? "";
if (token) sessionStorage.setItem("wagent_t", token);

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit, parse?: (x: unknown) => T): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "X-Token": token, "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = (await res.json()) as { error?: string };
      if (j.error) msg = j.error;
    } catch {
      /* body không phải JSON */
    }
    throw new ApiError(res.status, res.status === 403 ? "Sai hoặc thiếu token (mở lại đường dẫn có ?t=...)" : msg);
  }
  const data: unknown = await res.json();
  return parse ? parse(data) : (data as T);
}

export const api = {
  state: () => call("/api/state", undefined, (x) => State.parse(x)),
  run: (req: RunRequest) => call("/api/run", { method: "POST", body: JSON.stringify(req) }),
  stop: () => call("/api/stop", { method: "POST", body: "{}" }),
  approve: (id: string, allow: boolean) => call("/api/approve", { method: "POST", body: JSON.stringify({ id, allow }) }),
};
