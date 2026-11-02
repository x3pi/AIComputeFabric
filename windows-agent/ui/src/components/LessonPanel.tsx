import { useState } from "react";
import type { Lesson } from "../lib/schema";

type Props = {
  canSave: boolean;
  used?: string | null;
  lessons: Lesson[];
  onSave: (title: string) => void;
  onDelete: (id: string) => void;
  disabled?: boolean;
};

/** Lưu lần chạy vừa thành công thành bài học; xem/xoá bài học đã có. Bài học chỉ là gợi ý cho lần sau. */
export function LessonPanel({ canSave, used, lessons, onSave, onDelete, disabled }: Props) {
  const [title, setTitle] = useState("");
  return (
    <section aria-label="Bài học" className="space-y-3 rounded-xl border border-line bg-surface p-5">
      <h2 className="font-semibold">Bài học</h2>
      {used && <p className="text-sm text-muted">Lần chạy này đã dùng bài học: <b>{used}</b></p>}
      {canSave && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => { e.preventDefault(); onSave(title.trim()); setTitle(""); }}
        >
          <div className="min-w-[14rem] flex-1">
            <label htmlFor="lesson-title" className="mb-1 block text-sm font-medium">Tên bài học</label>
            <input id="lesson-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="vd: Đọc tiêu đề báo VnExpress"
              className="w-full rounded-lg border border-line bg-bg px-3 py-2 text-sm placeholder:text-muted" />
          </div>
          <button type="submit" disabled={disabled} className="rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">Lưu thành bài học</button>
        </form>
      )}
      {lessons.length === 0 ? (
        <p className="text-sm text-muted">Chưa có bài học nào. Chạy một nhiệm vụ thành công rồi bấm "Lưu thành bài học".</p>
      ) : (
        <ul className="divide-y divide-line text-sm">
          {lessons.map((l) => (
            <li key={l.id} className="flex items-center justify-between gap-3 py-2">
              <span><b>{l.title}</b> <span className="text-muted">· {l.steps} bước</span></span>
              <button type="button" onClick={() => onDelete(l.id)} aria-label={`Xoá ${l.title}`} className="rounded-lg border border-line px-3 py-1 text-xs">Xoá</button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
