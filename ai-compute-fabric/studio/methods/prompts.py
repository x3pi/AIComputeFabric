"""Prompt engineering and few-shot: versioned, immutable templates, a per-tenant default, a live test box and an A/B
comparison of the same questions with and without the examples."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional

import psycopg
import psycopg.types.json

from studio.config import Settings
from studio.db import Db, check_tenant
from studio.errors import bad, conflict, not_found, quota
from studio.llm import ChatClient
from studio.registry import MethodInfo
from studio.web import Request, Router

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
MAX_SYSTEM, MAX_EXAMPLES, MAX_EXAMPLE_CHARS, MAX_TOTAL = 8000, 20, 2000, 24000
MAX_COMPARE_QUESTIONS = 20


def validate_template(system, examples, note="") -> tuple:
    if not isinstance(system, str) or not system.strip() or len(system) > MAX_SYSTEM:
        raise bad(f"system prompt must be 1-{MAX_SYSTEM} characters", "bad_system")
    if not isinstance(examples, list) or len(examples) > MAX_EXAMPLES:
        raise bad(f"at most {MAX_EXAMPLES} examples", "bad_examples")
    out, total = [], len(system)
    for i, ex in enumerate(examples):
        if not isinstance(ex, dict) or not isinstance(ex.get("question"), str) or not isinstance(ex.get("answer"), str):
            raise bad(f"example {i + 1}: needs 'question' and 'answer' strings", "bad_examples")
        q, a = ex["question"].strip(), ex["answer"].strip()
        if not q or not a or len(q) > MAX_EXAMPLE_CHARS or len(a) > MAX_EXAMPLE_CHARS:
            raise bad(f"example {i + 1}: question and answer must be 1-{MAX_EXAMPLE_CHARS} characters", "bad_examples")
        total += len(q) + len(a)
        out.append({"question": q, "answer": a})
    if total > MAX_TOTAL:
        raise bad(f"template is longer than {MAX_TOTAL} characters in total (it is sent with every request)", "too_long")
    if not isinstance(note, str) or len(note) > 500:
        raise bad("note must be at most 500 characters")
    return system.strip(), out, note.strip()


def build_messages(system: str, examples: list, question: str, with_examples: bool = True) -> list:
    msgs = [{"role": "system", "content": system}]
    if with_examples:
        for ex in examples:
            msgs += [{"role": "user", "content": ex["question"]}, {"role": "assistant", "content": ex["answer"]}]
    msgs.append({"role": "user", "content": question})
    return msgs


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def score(answer: str, expected: Optional[str], expect_json: bool) -> dict:
    """What can be said objectively about an answer: does it contain the expected text, and is it valid JSON if asked."""
    out: dict = {}
    if expected:
        out["contains_expected"] = _norm(expected) in _norm(answer)
        out["exact"] = _norm(expected) == _norm(answer)
    if expect_json:
        txt = answer.strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt)
        try:
            json.loads(txt)
            out["valid_json"] = True
        except ValueError:
            out["valid_json"] = False
    return out


class PromptsMethod:
    info = MethodInfo(
        id="prompts", title="Prompt & Few-shot", status="available",
        summary="Viết chỉ dẫn hệ thống nghiêm ngặt và 3–5 ví dụ mẫu (câu hỏi → câu trả lời chuẩn). Mỗi lần sửa tạo một phiên bản mới, có thể quay lại; đặt một mẫu làm mặc định cho tenant và so sánh có/không ví dụ trên cùng bộ câu hỏi.",
        when_to_use="Thử nhanh định dạng đầu ra (luôn trả JSON, giọng điệu chăm sóc khách hàng) trước khi nghĩ tới huấn luyện.",
        limits=("Ví dụ chiếm cửa sổ ngữ cảnh ở mỗi lượt hỏi.", "Với hội thoại rất dài model có thể quên chỉ dẫn.", "So sánh A/B chỉ đo khớp chuỗi và JSON hợp lệ, không đo chất lượng văn phong."),
        ui={"icon": "message"})

    def __init__(self, settings: Settings, db: Db, llm: ChatClient):
        self.s, self.db, self.llm = settings, db, llm

    def mount(self, r: Router):
        b = "/v1/tenants/{tenant}"
        r.add("GET", b + "/prompts", self.list_items)
        r.add("POST", b + "/prompts", self.create)
        r.add("POST", b + "/prompts/test", self.test)
        r.add("POST", b + "/prompts/compare", self.compare)
        r.add("GET", b + "/prompt-default", self.get_default)
        r.add("PUT", b + "/prompt-default", self.set_default)
        r.add("GET", b + "/prompts/{name}", self.versions)
        r.add("GET", b + "/prompts/{name}/{version}", self.version)
        r.add("DELETE", b + "/prompts/{name}", self.delete)

    # -- data access ----------------------------------------------------------------------------
    def _get(self, c, t: str, name: str, version: Optional[int]):
        if version is None:
            row = c.execute("SELECT * FROM studio_prompts WHERE name=%s ORDER BY version DESC LIMIT 1", (name,)).fetchone()
        else:
            row = c.execute("SELECT * FROM studio_prompts WHERE name=%s AND version=%s", (name, version)).fetchone()
        return row

    @staticmethod
    def _pub(row) -> dict:
        return {"name": row["name"], "version": row["version"], "system": row["system"], "examples": row["examples"], "note": row["note"],
                "created_by": row["created_by"], "created_at": row["created_at"].isoformat()}

    def resolve(self, tenant: str, name: Optional[str] = None, version: Optional[int] = None) -> Optional[dict]:
        """The template to use for a request: the named one (latest version unless given) or the tenant default, else None."""
        with self.db.tenant(tenant) as c:
            if name is None:
                d = c.execute("SELECT name, version FROM studio_prompt_defaults").fetchone()
                if d is None:
                    return None
                name, version = d["name"], d["version"]
            row = self._get(c, tenant, name, int(version) if version is not None else None)
        if row is None:
            raise not_found("prompt template")
        return self._pub(row)

    # -- routes ---------------------------------------------------------------------------------
    def list_items(self, req: Request):
        t = check_tenant(req.params["tenant"])
        with self.db.tenant(t) as c:
            rows = c.execute("SELECT name, max(version) AS latest, count(*)::int AS versions, max(created_at) AS updated FROM studio_prompts GROUP BY name ORDER BY name").fetchall()
            d = c.execute("SELECT name, version FROM studio_prompt_defaults").fetchone()
        return {"templates": [{"name": r["name"], "latest": r["latest"], "versions": r["versions"], "updated_at": r["updated"].isoformat(),
                               "is_default": bool(d and d["name"] == r["name"])} for r in rows], "default": d and {"name": d["name"], "version": d["version"]}}

    def create(self, req: Request):
        req.need("operator")
        t = check_tenant(req.params["tenant"])
        name = req.str_param("name", maxlen=64).strip()
        if not NAME.match(name):
            raise bad("name: 1-64 characters, letters, digits, space, '.', '_' or '-'", "bad_name")
        system, examples, note = validate_template(req.body.get("system"), req.body.get("examples", []), req.body.get("note", ""))
        for attempt in range(3):            # two concurrent saves of the same name race for the next version number
            try:
                with self.db.tenant(t) as c:
                    n_names = c.execute("SELECT count(DISTINCT name)::int AS n FROM studio_prompts").fetchone()["n"]
                    cur = c.execute("SELECT coalesce(max(version), 0)::int AS v FROM studio_prompts WHERE name=%s", (name,)).fetchone()["v"]
                    if cur == 0 and n_names >= self.s.quotas.max_prompt_templates:
                        raise quota(f"at most {self.s.quotas.max_prompt_templates} templates per tenant")
                    if cur >= self.s.quotas.max_prompt_versions:
                        raise quota(f"at most {self.s.quotas.max_prompt_versions} versions per template")
                    c.execute("INSERT INTO studio_prompts(tenant_id, name, version, system, examples, note, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                              (t, name, cur + 1, system, psycopg.types.json.Jsonb(examples), note, req.user))
                return 201, {"name": name, "version": cur + 1}
            except psycopg.errors.UniqueViolation:
                continue
        raise conflict("could not allocate a version number, try again")

    def versions(self, req: Request):
        t = check_tenant(req.params["tenant"])
        with self.db.tenant(t) as c:
            rows = c.execute("SELECT * FROM studio_prompts WHERE name=%s ORDER BY version DESC", (req.params["name"],)).fetchall()
        if not rows:
            raise not_found("prompt template")
        return {"name": req.params["name"], "versions": [self._pub(r) for r in rows]}

    def version(self, req: Request):
        t = check_tenant(req.params["tenant"])
        try:
            v = int(req.params["version"])
        except ValueError:
            raise bad("version must be an integer") from None
        with self.db.tenant(t) as c:
            row = self._get(c, t, req.params["name"], v)
        if row is None:
            raise not_found("prompt version")
        return self._pub(row)

    def get_default(self, req: Request):
        t = check_tenant(req.params["tenant"])
        return {"default": self.resolve(t)}

    def set_default(self, req: Request):
        req.need("operator")
        t = check_tenant(req.params["tenant"])
        name = req.str_param("name", maxlen=64)
        try:
            version = int(req.body.get("version"))  # type: ignore[arg-type]  # None -> TypeError, handled below
        except (TypeError, ValueError):
            raise bad("version must be an integer") from None
        with self.db.tenant(t) as c:
            if self._get(c, t, name, version) is None:
                raise not_found("prompt version")
            c.execute("INSERT INTO studio_prompt_defaults(tenant_id, name, version, set_by) VALUES (%s,%s,%s,%s) "
                      "ON CONFLICT (tenant_id) DO UPDATE SET name=EXCLUDED.name, version=EXCLUDED.version, set_by=EXCLUDED.set_by, set_at=now()", (t, name, version, req.user))
        return {"default": {"name": name, "version": version}}

    def delete(self, req: Request):
        req.need("admin")
        t = check_tenant(req.params["tenant"])
        with self.db.tenant(t) as c:
            n = c.execute("DELETE FROM studio_prompts WHERE name=%s", (req.params["name"],)).rowcount
            c.execute("DELETE FROM studio_prompt_defaults WHERE name=%s", (req.params["name"],))
        if n == 0:
            raise not_found("prompt template")
        return {"deleted": req.params["name"], "versions": n}

    def _template_from(self, req: Request, t: str) -> dict:
        """Either a saved template (name[, version]) or an ad-hoc one in the request body (not saved)."""
        if req.body.get("system") is not None:
            system, examples, _ = validate_template(req.body.get("system"), req.body.get("examples", []))
            return {"system": system, "examples": examples, "name": None, "version": None}
        name = req.body.get("name")
        tpl = self.resolve(t, name, req.body.get("version"))
        if tpl is None:
            raise bad("give a template name, an ad-hoc 'system' prompt, or set a tenant default first", "no_template")
        return tpl

    def test(self, req: Request):
        t = check_tenant(req.params["tenant"])
        q = req.str_param("question", maxlen=4000).strip()
        if not q:
            raise bad("question is empty")
        tpl = self._template_from(req, t)
        with_ex = bool(req.body.get("with_examples", True))
        ans = self.llm.chat(build_messages(tpl["system"], tpl["examples"], q, with_ex), max_tokens=req.int_param("max_tokens", 300, 1, 2000))
        return {"answer": ans, "used": {"name": tpl["name"], "version": tpl["version"], "with_examples": with_ex, "examples": len(tpl["examples"]) if with_ex else 0}}

    def compare(self, req: Request):
        t = check_tenant(req.params["tenant"])
        qs = req.body.get("questions")
        if not isinstance(qs, list) or not 1 <= len(qs) <= MAX_COMPARE_QUESTIONS:
            raise bad(f"questions: a list of 1-{MAX_COMPARE_QUESTIONS} items", "bad_questions")
        tpl = self._template_from(req, t)
        if not tpl["examples"]:
            raise bad("this template has no examples, so there is nothing to compare", "no_examples")
        expect_json = bool(req.body.get("expect_json", False))
        rows, agg = [], {"zero_shot": {"n": 0, "contains_expected": 0, "valid_json": 0, "scored": 0}, "few_shot": {"n": 0, "contains_expected": 0, "valid_json": 0, "scored": 0}}
        for item in qs:
            q = item if isinstance(item, str) else (item or {}).get("question")
            exp = None if isinstance(item, str) else (item or {}).get("expected")
            if not isinstance(q, str) or not q.strip():
                raise bad("every question needs text", "bad_questions")
            row = {"question": q, "expected": exp}
            for key, with_ex in (("zero_shot", False), ("few_shot", True)):
                ans = self.llm.chat(build_messages(tpl["system"], tpl["examples"], q, with_ex), max_tokens=req.int_param("max_tokens", 300, 1, 2000))
                sc = score(ans, exp, expect_json)
                row[key] = {"answer": ans, **sc}
                a = agg[key]
                a["n"] += 1
                a["contains_expected"] += bool(sc.get("contains_expected"))
                a["valid_json"] += bool(sc.get("valid_json"))
                a["scored"] += "contains_expected" in sc
            rows.append(row)
        return {"template": {"name": tpl["name"], "version": tpl["version"], "examples": len(tpl["examples"])}, "rows": rows, "summary": agg,
                "note": "scores are exact-substring matches against the expected text and JSON validity; they say nothing about wording quality"}
