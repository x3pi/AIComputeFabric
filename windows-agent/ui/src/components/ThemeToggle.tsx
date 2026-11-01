import { useEffect, useState } from "react";

type Theme = "auto" | "light" | "dark";
const NEXT: Record<Theme, Theme> = { auto: "light", light: "dark", dark: "auto" };
const LABEL: Record<Theme, string> = { auto: "Tự động", light: "Sáng", dark: "Tối" };

export function ThemeToggle() {
  const [t, setT] = useState<Theme>(() => {
    try { return (localStorage.getItem("wagent_theme") as Theme) || "auto"; } catch { return "auto"; }
  });
  useEffect(() => {
    if (t === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("wagent_theme", t); } catch { /* bỏ qua */ }
  }, [t]);
  return (
    <button onClick={() => setT(NEXT[t])} className="rounded-lg border border-line px-3 py-1.5 text-sm" aria-label={`Giao diện: ${LABEL[t]}. Bấm để đổi`}>
      Giao diện: {LABEL[t]}
    </button>
  );
}
