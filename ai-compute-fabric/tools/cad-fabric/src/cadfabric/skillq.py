"""Questions like "what skills do you have?" are answered by the harness, not guessed by a model.
  - Asked about Qwen / CadFabric: the list comes from the skill and lesson stores (what is usable, what waits for approval).
  - Asked about the advisor ("cố vấn / Codex / Claude có skill gì"): the advisor selected in the Advisor box is asked, with a fixed prompt, to list ITS OWN skills
    (Codex lists the skills Codex has, Claude the skills Claude has). Nothing from the project is sent. Only the selected advisor is asked: naming another one
    would send data to a service the user has not confirmed, so the answer then says to select it first."""
import re
from . import advisors, lessons as lessons_mod, planners, skills as skills_mod

SKILLS_PROMPT = ("You are being asked about your own environment, not about CAD. List the agent skills (SKILL.md skills, including those that come from plugins) that are available to you in this session, "
                 "taken from your own list of skills. One per line as `name - one-line description`. Do not invent any. If you have none, answer exactly: (no skills). Do not run commands or read files. "
                 "Answer in Vietnamese except for the skill names.")


def _n(t):
    return lessons_mod._norm(t)


def detect(text):
    """-> None, or (target, named) where target is 'qwen' | 'advisor' and named is 'codex' | 'claude' | ''. Only short questions that mention skills."""
    t = " " + _n(text) + " "
    if len(text) > 160 or "$" in text or not re.search(r"\b(skills?|ky nang)\b", t):
        return None
    if not re.search(r"\b(gi|nao|liet ke|danh sach|list|what|which|show|co nhung|co khong|bao nhieu|ke ra|cho biet|cho xem)\b", t):
        return None
    if re.search(r"\b(tao|dung|ve|khoan|bo|cat|thiet ke|build|make|create)\b", t) and not re.search(r"\b(liet ke|danh sach|list|ke ra)\b", t):
        return None
    named = "codex" if re.search(r"\bcodex\b", t) else "claude" if re.search(r"\bclaude\b", t) else "agy" if re.search(r"\b(agy|antigravity)\b", t) else ""
    advisor = bool(named) or bool(re.search(r"\b(co van|advisor)\b", t))
    return ("advisor" if advisor else "qwen"), named


def qwen_answer(data_dir, project):
    ss = skills_mod.list_for(data_dir, project)
    ok = [s for s in ss if s["status"] == "approved"]
    auto = [s for s in ok if s.get("implicit", True)]
    only = [s for s in ok if not s.get("implicit", True)]
    wait = [s for s in ss if s["status"] == "pending"]
    ls = lessons_mod.list_for(data_dir, project)
    la = [x for x in ls if x["status"] == "approved"]
    lp = [x for x in ls if x["status"] == "pending"]
    out = ["**Skill của CadFabric (Qwen dùng):**"]
    if auto:
        out.append("Dùng được, Qwen tự chọn khi hợp (hoặc bạn gõ `$tên`):")
        out += ["- `%s` — %s" % (s["name"], s["description"][:140]) for s in auto]
    if only:
        out.append("Dùng được khi bạn gõ `$tên`:")
        out += ["- `$%s` — %s" % (s["name"], s["description"][:140]) for s in only]
    if wait:
        out.append("Đang chờ bạn duyệt (chưa dùng được, xem ở tab Skill): " + ", ".join("`%s`" % s["name"] for s in wait))
    if not ss:
        out.append("Chưa cài skill nào. Tab **Skill** cài được từ link GitHub hoặc thư mục `.agents/skills`.")
    out.append("")
    out.append("**Bài học:** %d đã duyệt (đang dùng), %d chờ duyệt. Gõ `#` để chọn bài học cụ thể." % (len(la), len(lp)))
    out.append("**Khả năng có sẵn:** dựng và đo chi tiết bằng CadQuery, đọc tệp trong thư mục dự án, lưu STEP, mở trong CAD hoặc Blender, đọc lại sổ làm việc (`recall`).")
    out.append("_Cố vấn (Codex/Claude) có skill riêng của nó, hỏi bằng cách chọn cố vấn rồi hỏi \"cố vấn có skill gì\"._")
    return "\n".join(out)


def advisor_kind(cfg, planner):
    """-> (kind, label): kind in 'qwen' (no outside advisor), 'codex', 'claude', 'agy', 'api', 'other'."""
    if planner in ("off", "nothink", "think", "", None):
        return "qwen", "Qwen"
    if planner in ("codex", "codex-relay"):
        return "codex", "Codex"
    if planner == "agy":
        return "agy", "Antigravity"
    a = advisors.find(cfg, planner)
    if not a:
        return "other", str(planner)
    if a.get("type") == "api":
        return "api", a.get("label") or planner
    pre = a.get("preset")
    return (pre if pre in ("codex", "claude", "agy") else "other"), a.get("label") or planner


def advisor_answer(cfg, planner, named=""):
    """-> (text, error). Asks the SELECTED advisor to list its own skills."""
    kind, label = advisor_kind(cfg, planner)
    if kind == "qwen":
        return "Hiện chưa chọn cố vấn bên ngoài (đang dùng Qwen). Chọn Codex, Claude hoặc Antigravity ở ô **Cố vấn** rồi hỏi lại để cố vấn đó liệt kê skill của nó.", ""
    if named and named != kind:
        return ("Bạn hỏi về %s nhưng ô Cố vấn đang chọn %s. Để tránh gửi dữ liệu tới dịch vụ bạn chưa xác nhận, hãy chọn %s ở ô **Cố vấn** rồi hỏi lại." % (named.capitalize(), label, named.capitalize())), ""
    if kind == "api":
        return "Cố vấn **%s** là API chỉ nhận văn bản, không có skill." % label, ""
    if planner == "codex-relay":
        return "Cố vấn Codex qua máy khác (relay) chưa hỗ trợ liệt kê skill. Chọn Codex CLI chạy trên máy này.", ""
    if planner == "codex":
        text, err = planners.ask_codex(SKILLS_PROMPT, cfg, raw=True)
    elif planner == "agy":
        text, err = advisors.ask({"id": "agy", "type": "cli", "preset": "agy"}, SKILLS_PROMPT, cfg, raw=True, skills=True)
    else:
        text, err = advisors.ask(advisors.find(cfg, planner), SKILLS_PROMPT, cfg, raw=True, skills=True)
    if err:
        return "", err
    return "**Skill của cố vấn %s** (của chính nó, không phải của CadFabric; Qwen không dùng chúng):\n\n%s" % (label, text.strip()), ""
