import { z } from "zod";

export const RunStatus = z.enum(["idle", "running", "done", "failed", "stopped", "timeout", "max_steps"]);
export type RunStatus = z.infer<typeof RunStatus>;

export const Approval = z.object({
  id: z.string(),
  reason: z.string(),
  action: z.object({ tool: z.string(), args: z.record(z.unknown()).default({}) }),
});
export type Approval = z.infer<typeof Approval>;

export const AgentEvent = z.discriminatedUnion("type", [
  z.object({ type: z.literal("step"), seq: z.number(), n: z.number(), tool: z.string(), args: z.record(z.unknown()), result: z.string() }),
  z.object({ type: z.literal("note"), seq: z.number(), text: z.string() }),
]);
export type AgentEvent = z.infer<typeof AgentEvent>;

export const State = z.object({
  status: RunStatus,
  summary: z.string().default(""),
  log: z.array(z.string()),
  pending: z.array(Approval),
  can_save_lesson: z.boolean().default(false),
  lesson_used: z.string().nullable().optional(),
});
export type State = z.infer<typeof State>;

export const Models = z.object({ models: z.array(z.string()) });
export type Models = z.infer<typeof Models>;

export const Lesson = z.object({ id: z.string(), title: z.string(), task: z.string().default(""), steps: z.number() });
export type Lesson = z.infer<typeof Lesson>;
export const Lessons = z.object({ lessons: z.array(Lesson) });

export const RunRequest = z.object({
  url: z.string().url("URL không hợp lệ (vd http://10.42.0.1:18090/v1)"),
  model: z.string().optional(),
  key: z.string().optional(),
  lesson: z.string().optional(),   // "" = tự động (chỉ khi rất giống), "none" = không dùng, hoặc id bài học
  task: z.string().trim().min(3, "Nhập nhiệm vụ (ít nhất 3 ký tự)"),
});
export type RunRequest = z.infer<typeof RunRequest>;

/** Phân tích một dòng log dạng `[n] {json} -> kết quả` thành bước có cấu trúc. */
export function parseStepLine(line: string): { n: number; tool: string; args: Record<string, unknown>; result: string } | null {
  const m = /^\[(\d+)\]\s+(\{.*?\})\s+->\s+([\s\S]*)$/.exec(line);
  if (!m) return null;
  try {
    const obj = JSON.parse(m[2] as string) as { tool?: unknown; args?: unknown };
    if (typeof obj.tool !== "string") return null;
    const args = obj.args && typeof obj.args === "object" ? (obj.args as Record<string, unknown>) : {};
    return { n: Number(m[1]), tool: obj.tool, args, result: (m[3] as string).trim() };
  } catch {
    return null;
  }
}

export type Risk = "low" | "medium" | "high";
const LOW = new Set(["observe", "read_text", "read_file", "list_dir", "open_app", "done"]);
export function toolRisk(tool: string): Risk {
  if (LOW.has(tool)) return "low";
  if (tool === "click" || tool === "set_text" || tool === "write_file" || tool === "move_file" || tool === "copy_file" || tool === "close_window") return "medium";
  return "high";
}
