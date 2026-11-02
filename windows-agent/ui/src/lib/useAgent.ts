import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { Lesson, RunRequest, State } from "./schema";

const EMPTY: State = { status: "idle", summary: "", log: [], pending: [], can_save_lesson: false };

/** Poll trạng thái; dừng poll khi tab ẩn để tiết kiệm tài nguyên. */
export function useAgent(intervalMs = 1000) {
  const [state, setState] = useState<State>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [lessons, setLessons] = useState<Lesson[] | null>(null);   // null = tính năng bài học tắt/không tải được
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer: number | undefined;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const s = await api.state();
          if (alive.current) { setState(s); setError(null); }
        } catch (e) {
          if (alive.current) setError(e instanceof Error ? e.message : String(e));
        }
      }
      if (alive.current) timer = window.setTimeout(tick, intervalMs);
    };
    void tick();
    return () => { alive.current = false; window.clearTimeout(timer); };
  }, [intervalMs]);

  const loadLessons = useCallback(async () => {
    try { if (alive.current) setLessons(await api.lessons()); } catch { if (alive.current) setLessons(null); }
  }, []);
  useEffect(() => { void loadLessons(); }, [loadLessons, state.status]);   // tải lại khi trạng thái đổi (vd vừa xong nhiệm vụ)

  const guard = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); setError(null); }
    catch (e) { setError(e instanceof ApiError || e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  }, []);

  return {
    state, error, busy, lessons,
    run: (r: RunRequest) => guard(() => api.run(r)),
    stop: () => guard(() => api.stop()),
    saveLesson: (title: string) => guard(async () => { await api.saveLesson(title); await loadLessons(); }),
    deleteLesson: (id: string) => guard(async () => { await api.deleteLesson(id); await loadLessons(); }),
    approve: (id: string, allow: boolean) => guard(() => api.approve(id, allow)),
  };
}
