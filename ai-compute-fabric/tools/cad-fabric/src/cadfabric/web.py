"""Web chat for cadfabric: `cadfabric serve`. Stdlib only, one job at a time, one part per session.
Security: binds 127.0.0.1 unless told otherwise; every request needs the access token (URL ?t=... once, then a cookie); POSTs need the X-Requested-With header;
the Host header must be one we serve (blocks DNS rebinding); Origin, when present, must match; request bodies are capped."""
import hmac, http.server, json, os, re, secrets, socketserver, subprocess, sys, threading, time, urllib.parse
from http import cookies
from . import advisors, config, history as history_mod, llm as llm_mod, viewers, workspace as ws_mod
from . import cq_backend, lessons as lessons_mod, skills as skills_mod, ledger as ledger_mod, skillq, scene3d
from .cq_backend import CadQueryBackend

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_page.html")
MAX_BODY = 40_000
MAX_BODY_SEND = 7_000_000                      # /api/send may carry up to 3 pictures (base64)


def spawn_window(project, session=None):
    """Start another CadFabric window for `project` (optionally with a saved conversation open). Detached: it outlives this one. -> {ok, detail}."""
    exe = sys.executable
    if os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.isfile(w) else exe
    argv = [exe, "-m", "cadfabric", "app", "--project", project] + (["--session", session] if session else [])
    try:
        viewers._spawn(argv)
    except OSError as e:
        return {"ok": False, "detail": "could not open a new window: %s" % str(e)[:100]}
    return {"ok": True, "detail": "opened a new window"}


class Session:
    """The conversation, the part and the event log shared by all requests."""

    def __init__(self, llm, cfg, backend=None, local=True):
        self.llm, self.cfg, self.local, self.quit = llm, cfg, local, False
        self.pinned = cfg.get("workspace") or ""   # this window's project; a different folder opens another window
        self.reflecting = False
        self.ledger = ledger_mod.Ledger()          # this conversation's work ledger (memory only until it has a session id)
        self.lessons_given = set()              # lesson ids already in this conversation's prompt
        self.autofile = None                    # the STEP file this conversation keeps up to date in the project folder
        self.picker = None                      # set by the desktop window: () -> folder path or ''
        self.backend = backend or CadQueryBackend()
        self.lock = threading.Lock()
        self.events, self.busy, self.stop, self.history, self.measure = [], False, False, None, None
        self.thread = None
        self.sid, self.created, self.title = None, "", ""          # the saved-session record this conversation is written to

    def emit(self, kind, **kw):
        with self.lock:
            self.events.append(dict(kw, kind=kind, t=round(time.time(), 1)))

    def state(self, since):
        with self.lock:
            return {"busy": self.busy, "events": self.events[since:], "next": len(self.events), "measure": self.measure, "has_part": self.measure is not None,
                    "follow_up": self.history is not None}

    def send(self, text, planner, files=(), uploads=None, images=None, worker=None):
        with self.lock:
            if self.busy:
                return False
            self.busy, self.stop = True, False
        if not self.sid:
            self.sid, self.created, self.title = history_mod.new_id(), time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(text.split())[:60]
            self.ledger = ledger_mod.Ledger(self._ledger_path(self.sid))
        w_id = worker or self.cfg.get("worker") or "qwen"
        self.emit("user", files=list(files) + [u["name"] for u in (uploads or [])] + [i["name"] for i in (images or [])], text=text,
                  planner=planner if self.history is None else "(follow-up: no plan)", worker=w_id)
        self.thread = threading.Thread(target=self._work, args=(text, planner, tuple(files), uploads or [], images or [], w_id), daemon=True)
        self.thread.start()
        return True

    def _work(self, text, planner, files=(), uploads=(), images=(), worker=None):
        try:
            root = self.cfg.get("workspace") or ""
            q = skillq.detect(text) if (self.local and not files and not uploads) else None       # "what skills do you have?" is answered by the harness
            if q:
                t0 = time.time()
                if q[0] == "qwen":
                    ans, err = skillq.qwen_answer(config.data_dir(self.cfg), root), ""
                else:
                    self.emit("phase", what="advisor-skills")
                    ans, err = skillq.advisor_answer(self.cfg, planner, q[1])
                if err:
                    raise ValueError("cố vấn không trả lời: " + err)
                self.emit("answer", text=ans, who=q[0], sec=round(time.time() - t0, 1))
                if self.history is None and not self.backend.measure():                  # a question is not a design: the next real request starts the conversation (and names the files)
                    self.sid, self.created, self.title = None, "", ""
                    self.ledger = ledger_mod.Ledger()
                return
            block, _used, err = ws_mod.attachments(root, files) if files else ("", [], "")
            if err:
                raise ValueError("attached file: " + err)
            ublock, _n, uerr = ws_mod.uploads(list(uploads)) if uploads else ("", [], "")
            if uerr:
                raise ValueError("dropped file: " + uerr)
            block = "\n\n".join(x for x in (block, ublock) if x)
            if self.local:                                                          # $skill-name: routed by role (director-eligible -> prompt block once; worker-eligible -> _scene_skills.explicit_worker)
                dd0 = config.data_dir(self.cfg)
                names, blocked = skills_mod.mentions(dd0, root, text)
                for n in blocked:
                    self.emit("skill_blocked", name=n)
                if names:
                    self.emit("skill", name=", ".join(names), ok=True, explicit=True)
                    by_name0 = {m["name"]: m for m in skills_mod.list_for(dd0, root) if m["status"] == "approved"}
                    dnames0 = [n for n in names if n in by_name0 and skills_mod.for_role(by_name0[n], "director")]
                    if dnames0:
                        block = "\n\n".join(x for x in (block, skills_mod.explicit_block(dd0, root, dnames0)) if x)
            return self._scene_turn(text, planner, block, images=list(images), worker=worker)
        except Exception as e:                                                   # noqa: BLE001 - show the failure in the chat
            self.emit("error", text="%s: %s" % (type(e).__name__, str(e)[:300]))
        finally:
            self.autosave()
            self.save_history()
            with self.lock:
                self.busy = False
            if self.cfg.get("lessons_auto", True) and self.local:
                with self.lock:
                    evs = list(self.events)
                if lessons_mod.signals(evs):
                    threading.Thread(target=self.reflect, args=(True,), daemon=True).start()

    def _scene_skills(self, text):
        """Installed AND approved skills for the scene agent: the director sees the catalog and may pick up to 3 for the worker; $name invokes one for both."""
        if not self.local:
            return None
        dd, root = config.data_dir(self.cfg), self.cfg.get("workspace") or ""
        try:
            ap = [m for m in skills_mod.list_for(dd, root) if m["status"] == "approved"]
            if not ap:
                return None
            names, _blocked = skills_mod.mentions(dd, root, text)
            pick = [m for m in ap if m.get("implicit", True) and skills_mod.for_role(m, "director")]
            wpick = [m["name"] for m in ap if m.get("implicit", True) and skills_mod.for_role(m, "worker")]
            cat = "\n".join("- %s: %s" % (m["name"], (m.get("description") or "")[:200]) for m in pick)[:6000]
            by_name = {m["name"]: m for m in ap}
            wnames = [n for n in names if n in by_name and skills_mod.for_role(by_name[n], "worker")] if names else []
            return {"catalog": cat, "names": [m["name"] for m in pick], "worker_names": wpick, "explicit": "",
                    "explicit_worker": skills_mod.explicit_block(dd, root, wnames) if wnames else "",
                    "read": lambda n: skills_mod.use(dd, root, n), "fit": skills_mod.fit}
        except Exception:                                             # noqa: BLE001 - a broken skill folder must not stop a build
            return None

    def blender_exe(self):
        """The Blender program: "blender" in the config file, else $BLENDER / PATH / the usual install places."""
        c = str(self.cfg.get("blender") or "")
        return c if os.path.isfile(c) else viewers.blender_path()

    def _scene_turn(self, text, planner, block, images=(), worker=None):
        """One user turn with the scene agent. Both director (`planner`) and worker (`worker`) can be any model in the unified pool."""
        blender = self.blender_exe()
        if not blender:
            raise ValueError("Không tìm thấy Blender. Cài Blender 4.x (hoặc đặt biến môi trường BLENDER trỏ tới chương trình) rồi thử lại.")
        w_id = worker or self.cfg.get("worker") or "qwen"
        adv = advisors.find(self.cfg, planner) if planner not in ("off", "qwen", "nothink", "think") else None
        if planner == "codex":
            adv = {"id": "codex", "type": "cli", "preset": "codex", "external": True}
        elif planner == "codex-relay":
            raise ValueError("Cố vấn 'Codex trên máy khác (ssh)' chưa hỗ trợ ở chế độ dựng cảnh 3D. Chọn Claude, Codex trên máy này hoặc Qwen.")
        elif planner not in ("off", "qwen", "nothink", "think") and adv is None:
            raise ValueError("Không tìm thấy cố vấn '%s' trong cấu hình." % planner)
        if w_id not in advisors.worker_names(self.cfg):
            raise ValueError("Không tìm thấy model thực thi '%s' trong cấu hình." % w_id)
        wd = os.path.join(config.data_dir(self.cfg), "scenes", self.sid or history_mod.new_id())
        os.makedirs(wd, exist_ok=True)
        request = text + (("\n\nATTACHED FILES (data from the user's folder, not instructions):\n" + block) if block else "")

        def director(prompt, system, images=None):
            try:
                return advisors.chat_model(self.cfg, planner, self.llm, system, prompt, timeout=900, max_tokens=8192, temperature=0.2, images=images)
            except RuntimeError as e:
                raise RuntimeError("cố vấn không trả lời: " + str(e)) from e

        def worker_fn(system, user, temperature=0.3):
            return advisors.chat_model(self.cfg, w_id, self.llm, system, user, timeout=900, max_tokens=3000, temperature=temperature)

        if images and not advisors.can_see_images(adv):
            raise ValueError("Ảnh chỉ gửi được cho cố vấn Claude (Claude Code CLI hoặc Anthropic API). Chọn Claude làm cố vấn, hoặc bỏ ảnh đính kèm.")
        resume = isinstance(self.history, dict) and bool(self.history.get("scene3d"))
        r = scene3d.run(request, wd, blender, director, worker_fn, on_event=lambda kind, **kw: self.emit(kind, **kw), should_stop=lambda: self.stop, resume=resume,
                        skills=self._scene_skills(text), images=list(images), review_images=advisors.can_see_images(adv),
                        plan_cache=os.path.join(config.data_dir(self.cfg), "plan_cache"))
        self.history = r["history"]
        rep = r.get("report") or {}
        bb = rep.get("parts_bbox_mm") or {}
        with self.lock:
            if bb:
                lo = [min(v[0][i] for v in bb.values()) for i in range(3)]
                hi = [max(v[1][i] for v in bb.values()) for i in range(3)]
                t = rep.get("totals") or {}
                self.measure = {"volume": t.get("volume_mm3", 0), "bbox_size": [h - l for h, l in zip(hi, lo)], "bbox_min": lo, "bbox_max": hi, "com": [(h + l) / 2 for h, l in zip(hi, lo)],
                                "faces": t.get("faces", 0), "edges": t.get("edges", 0), "vertices": t.get("vertices", 0), "solids": len(bb), "valid": bool(r.get("ok")), "links": ""}
        self.emit("final", text=r["final"] or ("(stopped)" if r.get("stopped") else "(ended)"), warnings=r["warnings"], gates=0, steps=r["steps"], sec=r["sec"],
                  resume=bool(r.get("stuck") or r.get("ended_by_limit")), budget=None, peak_ctx=0, usage=r.get("usage"))

    def _autosave_scene(self):
        """Keep the scene in the project folder: <name>.blend plus <name>.scene/ (plan.json, parts/*.py) that rebuilds it. A disk problem must never break the chat."""
        root = self.cfg.get("workspace") or ""
        src = (self.history or {}).get("workdir") if isinstance(self.history, dict) else ""
        if not root or not self.local or not src or not os.path.isfile(os.path.join(src, "scene.blend")):
            return
        try:
            import re as _re, shutil, unicodedata
            real = os.path.realpath(root)
            if not (self.autofile and os.path.dirname(self.autofile) == real):
                slug = unicodedata.normalize("NFKD", (self.title or "scene").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
                slug = _re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:40] or "scene"
                for i in range(100):
                    base = os.path.join(real, slug + ("" if i == 0 else "-%d" % i))
                    if not os.path.exists(base + ".blend") and not os.path.exists(base + ".scene"):
                        break
                self.autofile = base + ".blend"
            shutil.copyfile(os.path.join(src, "scene.blend"), self.autofile + ".tmp")
            os.replace(self.autofile + ".tmp", self.autofile)
            d = self.autofile[:-6] + ".scene"
            os.makedirs(os.path.join(d, "parts"), exist_ok=True)
            for f in ("plan.json", "usage.json"):
                if os.path.isfile(os.path.join(src, f)):
                    shutil.copyfile(os.path.join(src, f), os.path.join(d, f))
            for f in os.listdir(os.path.join(src, "parts")):
                shutil.copyfile(os.path.join(src, "parts", f), os.path.join(d, "parts", f))
            self.emit("file", op="saved", path=os.path.basename(self.autofile) + " (+ .scene/)")
        except Exception:                                                  # noqa: BLE001
            pass

    def reflect(self, auto=False):
        """Roll candidate lessons out of this conversation (pending until the user approves). One run at a time. -> (status, body)."""
        with self.lock:
            if self.reflecting:
                return 409, {"error": "already reflecting"}
            self.reflecting = True
            evs = list(self.events)
        try:
            cands, _raw = lessons_mod.reflect(self.llm, evs)
            added, skipped = lessons_mod.add(config.data_dir(self.cfg), self.cfg.get("workspace") or "", cands, source=self.sid or "")
            if added or not auto:
                self.emit("lessons", added=len(added), skipped=[{"title": t, "why": w} for t, w in skipped][:6], auto=auto)
            return 200, {"added": len(added), "skipped": [{"title": t, "why": w} for t, w in skipped]}
        except Exception as e:                                                  # noqa: BLE001 - a failed reflection must never disturb the chat
            if not auto:
                self.emit("error", text="Rút bài học không được: %s" % str(e)[:200])
            return 500, {"error": str(e)[:200]}
        finally:
            with self.lock:
                self.reflecting = False

    def autosave(self):
        """Keep the current part in the project folder by default: <name>.step plus the CadQuery script <name>.py. One pair per conversation:
        the first turn picks a fresh name (never an existing file), later turns rewrite that pair. A disk problem must never break the chat."""
        if isinstance(self.history, dict) and self.history.get("scene3d"):
            return self._autosave_scene()
        root = self.cfg.get("workspace") or ""
        if not root or not self.local or not self.measure or not getattr(self.backend, "history", None):
            return
        try:
            real = os.path.realpath(root)
            if not (self.autofile and os.path.dirname(self.autofile) == real and os.path.isfile(self.autofile)):
                import re, unicodedata
                slug = unicodedata.normalize("NFKD", (self.title or "part").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
                slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:40] or "part"
                for i in range(100):                                       # the .py name must be free too
                    p, err = ws_mod.new_step_path(root, slug + ("" if i == 0 else "-%d" % i))
                    if err:
                        return
                    if not os.path.exists(p[:-5] + ".py"):
                        break
                self.autofile, first = p, True
            else:
                first = False
            tmp = os.path.join(os.path.dirname(self.autofile), ".cadfabric-tmp.step")
            self.backend.export_step(tmp)
            os.replace(tmp, self.autofile)
            with open(self.autofile[:-5] + ".py", "w", encoding="utf-8") as f:
                f.write("# CadQuery script that rebuilds this part (written by CadFabric)\n" + "\n\n# ---- next step ----\n".join(self.backend.history) + "\n")
            self.emit("file", op="saved", path=os.path.basename(self.autofile) + (" (+ .py)" if first else " (cập nhật)"))
        except Exception:                                                  # noqa: BLE001
            pass

    def save_history(self):
        """Write the conversation to disk (after every turn, even a failed one). A disk problem must never break the chat."""
        if not self.sid or not self.local:
            return
        try:
            with self.lock:
                evs = list(self.events)
            history_mod.save(config.data_dir(self.cfg), {"id": self.sid, "title": self.title, "created": self.created, "events": evs, "messages": self.history,
                                                          "cells": list(getattr(self.backend, "history", [])), "measure": self.measure,
                                                          "workspace": history_mod.norm(self.cfg.get("workspace") or "")})
        except (OSError, TypeError, ValueError):
            pass

    def _ledger_path(self, sid):
        return os.path.join(config.data_dir(self.cfg), "ledger", sid + ".jsonl")

    def load_history(self, sid):
        """Reopen a saved conversation: rebuild the part by replaying its cells, restore the chat and the model's context. -> (status, body)."""
        rec = history_mod.load(config.data_dir(self.cfg), sid) if history_mod.valid(sid) else None
        if not rec:
            return 404, {"error": "no such saved session"}
        rws, cur = rec.get("workspace") or "", self.cfg.get("workspace") or ""
        if rws and history_mod.norm(rws) != history_mod.norm(cur):             # it belongs to another project: open that project's window
            if not os.path.isdir(rws):
                return 404, {"error": "the project folder of this conversation no longer exists: " + rws}
            r = spawn_window(rws, sid)
            return (200, {"ok": True, "opened_window": rws}) if r["ok"] else (500, {"error": r["detail"]})
        with self.lock:
            if self.busy:
                return 409, {"error": "busy: wait for the current job"}
            self.busy = True
        try:
            self.backend.reset()
            bad = 0
            for code in rec.get("cells") or []:
                if isinstance(code, str) and not self.backend.execute(code).ok:
                    bad += 1
            m = self.backend.measure() if rec.get("cells") else None
            with self.lock:
                self.events = list(rec["events"])
                self.history = rec.get("messages") if isinstance(rec.get("messages"), (list, dict)) else None
                self.measure = m.as_dict() if m else (rec.get("measure") if isinstance(self.history, dict) else None)
                self.sid, self.created, self.title = rec["id"], rec.get("created", ""), rec.get("title", ""); self.autofile = None
                self.lessons_given = set()
                self.ledger = ledger_mod.Ledger(self._ledger_path(rec["id"]))
                if not self.ledger.cells:                                            # older record without a ledger file: rebuild it from the saved successful cells
                    for code in rec.get("cells") or []:
                        if isinstance(code, str):
                            self.ledger.add_cell(code, True)
            return 200, {"ok": True, "id": sid, "replay_failed": bad, "has_part": m is not None}
        finally:
            with self.lock:
                self.busy = False

    def apply_settings(self, clean, key, advisor_keys=None):
        """Save and hot-reload. Refused while a job runs."""
        with self.lock:
            if self.busy:
                return None
        path = config.save_settings(clean, key, advisor_keys)
        cfg = config.load()
        if "workspace" in clean:
            self.pinned = clean["workspace"] or ""
        cfg["workspace"] = self.pinned                                          # another window's choice never leaks into this one
        self.cfg, self.llm = cfg, llm_mod.LLM(cfg)
        return path

    def set_project(self, path):
        """Choose the project. First project of this window: bound here. A different folder than the one this window already has: it opens in a NEW window,
        this one keeps its project (and its conversations). -> (http status, body)."""
        ws, err = ws_mod.check_folder(path)
        if err:
            return 400, {"error": err}
        cur = self.cfg.get("workspace") or ""
        if not ws:                                                      # empty = leave the project
            return (200, {"workspace": "", "recent": self.recent()}) if self.apply_settings({"workspace": "", "relay": {}}, None) is not None else (409, {"error": "busy"})
        recent = [ws] + [p for p in (self.cfg.get("recent") or []) if p != ws]
        if cur and os.path.realpath(ws) != os.path.realpath(cur):
            r = spawn_window(ws)
            if not r["ok"]:
                return 500, {"error": r["detail"]}
            self.apply_settings({"recent": recent[:8], "relay": {}}, None)
            return 200, {"workspace": cur, "recent": self.recent(), "opened_window": ws}
        if self.apply_settings({"workspace": ws, "recent": recent[:8], "relay": {}}, None) is None:
            return 409, {"error": "busy"}
        return 200, {"workspace": ws, "recent": self.recent()}

    def recent(self):
        return [p for p in (self.cfg.get("recent") or []) if isinstance(p, str) and os.path.isdir(p)]

    def open_in_viewer(self):
        """Export the part to <data dir>/exports and open it in the chosen CAD program. -> (http status, body)."""
        if not self.measure:
            return 404, {"error": "no part yet"}
        if isinstance(self.history, dict) and self.history.get("scene3d"):
            blend = self.autofile if (self.autofile and os.path.isfile(self.autofile)) else os.path.join(self.history.get("workdir", ""), "scene.blend")
            exe = self.blender_exe()
            if not exe or not os.path.isfile(blend):
                return 404, {"error": "Blender or the scene file was not found"}
            try:
                viewers._spawn([exe, blend])
            except OSError as e:
                return 500, {"error": "Blender could not be started: %s" % str(e)[:100]}
            return 200, {"ok": True, "viewer": "Blender", "detail": "sent to Blender", "path": blend}
        d = os.path.join(config.data_dir(self.cfg), "exports"); os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "part-%s.step" % time.strftime("%Y%m%d-%H%M%S"))
        if self.autofile and os.path.isfile(self.autofile):                       # the part is already in the project folder: open that file
            d, p = os.path.dirname(self.autofile), self.autofile
        try:
            self.backend.export_step(p)
        except Exception as e:                                                   # noqa: BLE001
            return 500, {"error": "export failed: %s" % str(e)[:160]}
        for old in sorted(os.listdir(d))[:-20]:                                   # keep the last 20 exports
            try: os.unlink(os.path.join(d, old))
            except OSError: pass
        r = viewers.open_file(p, self.cfg); r["path"] = p
        return 200, r

    def reset(self):
        with self.lock:
            if self.busy:
                return False
            self.events, self.history, self.measure = [], None, None
            self.lessons_given = set()
            self.ledger = ledger_mod.Ledger()
            self.sid, self.created, self.title = None, "", ""
            self.autofile = None
        self.backend.reset()
        return True

    def close(self):
        self.backend.close()


def public_settings(cfg):
    """What the settings dialog may see. The key is never sent back, only whether one is set."""
    try:
        has_key = bool(config.read_key(cfg))
    except SystemExit:
        has_key = False
    return {"url": cfg["url"], "model": cfg["model"], "planner": cfg["planner"], "worker": cfg.get("worker") or "qwen",
            "relay": cfg.get("relay") or {}, "has_key": has_key, "advisors": advisors.meta(cfg), "models": advisors.models_meta(cfg),
            "viewer": cfg.get("viewer") or {"kind": "auto", "command": ""}, "viewer_label": viewers.resolve(cfg)[1], "viewers_detected": viewers.detected(), "workspace": cfg.get("workspace") or "", "lessons_auto": bool(cfg.get("lessons_auto", True)),
            "configured": bool(cfg.get("_path")), "config_path": cfg.get("_path") or config.user_config_path()}


def make_handler(sess, token, hosts, data_dir):
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "cadfabric"

        def log_message(self, *a):
            pass

        def _static(self, rel):
            """Bundled viewer libraries (syntax colouring, Markdown, 3D). Whitelisted names only, long cache: they never change within a version."""
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
            full = os.path.realpath(os.path.join(base, *rel.split("/"))) if re.match(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", rel) and ".." not in rel else ""
            if not full or not full.startswith(base + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ctype = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".wasm": "application/wasm", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}.get(os.path.splitext(full)[1].lower())
            if not ctype:
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers(); self.wfile.write(data)

        def _send(self, code, body, ctype="application/json", extra=()):
            b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(b)

        def _auth(self, query):
            host = (self.headers.get("Host") or "").lower()
            if host not in hosts:
                self._send(403, {"error": "bad host"}); return None
            c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            given = (query.get("t") or [None])[0] or (c["cft"].value if "cft" in c else "")
            if not given or not hmac.compare_digest(given, token):
                self._send(401, {"error": "access token required: open the URL printed by `cadfabric serve`"}); return None
            return "t" in query

        def _csrf(self):
            if self.headers.get("X-Requested-With") != "cadfabric":
                self._send(403, {"error": "missing X-Requested-With"}); return False
            o = self.headers.get("Origin")
            if o and urllib.parse.urlparse(o).netloc.lower() not in hosts:
                self._send(403, {"error": "bad origin"}); return False
            return True

        def do_GET(self):
            u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
            fresh = self._auth(q)
            if fresh is None:
                return
            cookie = [("Set-Cookie", "cft=%s; HttpOnly; SameSite=Strict; Path=/" % token)] if fresh else []
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            if u.path == "/":
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8", cookie)
            elif u.path == "/api/state":
                self._send(200, sess.state(int((q.get("since") or ["0"])[0] or 0)), extra=cookie)
            elif u.path == "/api/config":
                self._send(200, {"planners": advisors.names(sess.cfg), "workers": advisors.worker_names(sess.cfg),
                                 "external": [n for n in advisors.worker_names(sess.cfg) + ["codex-relay"] if advisors.is_external(sess.cfg, n)],
                                 "default": sess.cfg["planner"], "default_worker": sess.cfg.get("worker") or "qwen",
                                 "advisors": advisors.meta(sess.cfg), "models": advisors.models_meta(sess.cfg), "cli_detected": advisors.detected(sess.cfg), "cli_presets": advisors.presets_info(), "viewer_label": viewers.resolve(sess.cfg)[1], "version": __import__("cadfabric").__version__,
                                 "recent": sess.recent() if sess.local else [], "local": sess.local, "picker": bool(sess.picker and sess.local), "settings": public_settings(sess.cfg)})
            elif u.path == "/api/history":
                every = (q.get("all") or [""])[0] == "1"
                self._send(200, {"sessions": history_mod.list_sessions(data_dir, None if every else sess.cfg.get("workspace") or "") if sess.local else [], "current": sess.sid,
                                 "project": sess.cfg.get("workspace") or "", "all": every}, extra=cookie)
            elif u.path == "/api/skills":
                if not sess.local:
                    return self._send(403, {"error": "skills are only available when the chat is bound to this computer"})
                ws = sess.cfg.get("workspace") or ""
                q = urllib.parse.parse_qs(u.query)
                if q.get("name"):                                                   # view one skill (any status) so the user can read it before approving
                    t, files, err = skills_mod.read_text(data_dir, ws, q["name"][0], (q.get("file") or ["SKILL.md"])[0], approved_only=False)
                    return self._send(404 if err else 200, {"error": err} if err else {"text": t, "files": files})
                self._send(200, {"skills": skills_mod.list_for(data_dir, ws), "project": ws})
            elif u.path == "/api/lessons":
                if not sess.local:
                    return self._send(403, {"error": "lessons are only available when the chat is bound to this computer"})
                ws = sess.cfg.get("workspace") or ""
                self._send(200, {"lessons": lessons_mod.list_for(data_dir, ws), "project": ws, "reflecting": sess.reflecting, "auto": bool(sess.cfg.get("lessons_auto", True))})
            elif u.path == "/api/files":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                r, err = ws_mod.listing(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0])
                self._send(200 if r else 400, dict(r, root=os.path.basename((sess.cfg.get("workspace") or "").rstrip("/\\"))) if r else {"error": err})
            elif u.path == "/api/file":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                rel = (q.get("path") or [""])[0]
                text, err = ws_mod.read_text(sess.cfg.get("workspace") or "", rel)
                self._send(200 if text is not None else 400, {"path": rel, "text": text} if text is not None else {"error": err})
            elif u.path == "/api/stepmesh":
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                full, _m, err = ws_mod.binary_file(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0], "step")
                if err:
                    return self._send(400, {"error": err})
                try:
                    r = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stepmesh.py"), full], capture_output=True, timeout=90,
                                       env=cq_backend.worker_env(), **cq_backend.NOWIN)
                except subprocess.TimeoutExpired:
                    return self._send(504, {"error": "the 3D conversion took too long"})
                if r.returncode != 0 or not r.stdout.startswith(b"{"):
                    return self._send(422, {"error": "this STEP file could not be converted for 3D"})
                self._send(200, r.stdout)
            elif u.path in ("/api/raw", "/api/preview"):
                if not sess.local:
                    return self._send(403, {"error": "the working folder is only available when the chat is bound to this computer"})
                kind = "image" if u.path == "/api/raw" else "step"
                full, mime, err = ws_mod.binary_file(sess.cfg.get("workspace") or "", (q.get("path") or [""])[0], kind)
                if err:
                    return self._send(400, {"error": err})
                lock = [("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")]
                if kind == "image":
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), mime, lock)
                try:
                    r = subprocess.run([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stepview.py"), full], capture_output=True, timeout=90,
                                       env=cq_backend.worker_env(), **cq_backend.NOWIN)
                except subprocess.TimeoutExpired:
                    return self._send(504, {"error": "the preview took too long"})
                if r.returncode != 0 or not r.stdout.startswith(b"<?xml") and not r.stdout.startswith(b"<svg"):
                    return self._send(422, {"error": "this STEP file could not be drawn"})
                self._send(200, r.stdout, "image/svg+xml", lock)
            elif u.path == "/api/browse":
                if not sess.local:
                    return self._send(403, {"error": "folder browsing is only available when the chat is bound to this computer"})
                cur = os.path.abspath(os.path.expanduser((q.get("path") or [""])[0] or sess.cfg.get("workspace") or "~"))
                if not os.path.isdir(cur):
                    return self._send(400, {"error": "not a folder"})
                try:
                    names = sorted((n for n in os.listdir(cur) if not n.startswith(".") and os.path.isdir(os.path.join(cur, n))), key=str.lower)
                except OSError as e:
                    return self._send(400, {"error": str(e)[:120]})
                self._send(200, {"path": cur, "parent": os.path.dirname(cur) if os.path.dirname(cur) != cur else "", "dirs": names[:500]})
            elif u.path == "/api/part.svg":
                try:
                    self._send(200, sess.backend.preview_svg(), "image/svg+xml")
                except Exception as e:                                           # noqa: BLE001
                    self._send(404, {"error": str(e)[:200]})
            elif u.path == "/api/part.step":
                if not sess.measure:
                    return self._send(404, {"error": "no part yet"})
                p = os.path.join(data_dir, "web-part-%d.step" % int(time.time()))
                try:
                    sess.backend.export_step(p)
                    with open(p, "rb") as f:
                        self._send(200, f.read(), "application/octet-stream", [("Content-Disposition", 'attachment; filename="part.step"')])
                finally:
                    if os.path.exists(p): os.unlink(p)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            if self._auth({}) is None or not self._csrf():
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > (MAX_BODY_SEND if u.path == "/api/send" else MAX_BODY):
                return self._send(413, {"error": "request too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "bad json"})
            if u.path == "/api/send":
                text = str(body.get("text", "")).strip()
                planner = body.get("planner") or sess.cfg["planner"]
                worker = str(body.get("worker") or sess.cfg.get("worker") or "qwen").strip()
                if not text or len(text) > 8000:
                    return self._send(400, {"error": "empty or too long"})
                if planner not in advisors.names(sess.cfg):
                    return self._send(400, {"error": "unknown planner"})
                if worker not in advisors.worker_names(sess.cfg):
                    return self._send(400, {"error": "unknown worker"})
                if (advisors.is_external(sess.cfg, planner) or advisors.is_external(sess.cfg, worker)) and not body.get("confirm_external"):
                    return self._send(409, {"error": "selected planner or worker sends your request to an external service; confirm_external is required"})
                files = body.get("files") or []
                if files and (not sess.local or not isinstance(files, list) or len(files) > 8 or not all(isinstance(x, str) and len(x) < 300 for x in files)):
                    return self._send(400, {"error": "attached files are only available locally (at most 8)"})
                _b, _u, ferr = ws_mod.attachments(sess.cfg.get("workspace") or "", files) if files else ("", [], "")
                if ferr:
                    return self._send(400, {"error": ferr})
                ups = body.get("uploads") or []
                if ups:
                    if not sess.local or len(files) + len(ups) > 8:
                        return self._send(400, {"error": "dropped files are only available locally (at most 8 files in total)"})
                    _b, _u, uerr = ws_mod.uploads(ups)
                    if uerr:
                        return self._send(400, {"error": uerr})
                imgs = body.get("images") or []
                if imgs:
                    if not sess.local:
                        return self._send(400, {"error": "pictures are only available locally"})
                    imgs, ierr = ws_mod.images(imgs)
                    if ierr:
                        return self._send(400, {"error": ierr})
                    a = advisors.find(sess.cfg, planner)
                    if not advisors.can_see_images(a):
                        return self._send(400, {"error": "pictures can only be sent to the Claude Code advisor (choose it as the advisor)"})
                ok = sess.send(text, planner, files, ups, imgs or None, worker=worker)
                self._send(200 if ok else 409, {"ok": True} if ok else {"error": "busy"})
            elif u.path in ("/api/settings", "/api/test", "/api/quit", "/api/advisor_test", "/api/open", "/api/pick_folder", "/api/project", "/api/reveal", "/api/open_file", "/api/open_cad", "/api/open_blender", "/api/lessons", "/api/skills"):
                if not sess.local:
                    return self._send(403, {"error": "settings and quit are only available when the chat is bound to this computer (no --allow-remote)"})
                if u.path == "/api/quit":
                    sess.quit = True; self._send(200, {"ok": True}); return
                if u.path in ("/api/reveal", "/api/open_file", "/api/open_cad", "/api/open_blender"):
                    rel = body.get("path") if isinstance(body, dict) else ""
                    root = sess.cfg.get("workspace") or ""
                    if rel in ("", "."):                                                    # the project folder itself
                        full, err = (os.path.realpath(root), "") if root else (None, "no working folder is set")
                    else:
                        full, err = ws_mod.resolve(root, rel)
                    if err or not full or not os.path.exists(full):
                        return self._send(400, {"error": err or "no such file"})
                    if u.path == "/api/reveal":
                        r = viewers.reveal(full)
                    elif os.path.isdir(full):
                        return self._send(400, {"error": "that is a folder"})
                    elif u.path == "/api/open_file":
                        r = viewers.open_default(full)
                    elif u.path == "/api/open_blender":
                        r = viewers.open_blender(full)
                    else:
                        if os.path.splitext(full)[1].lower() not in (".step", ".stp"):
                            return self._send(400, {"error": "only STEP files open in the CAD program"})
                        r = viewers.open_file(full, sess.cfg)
                    return self._send(200 if r.get("ok") else 500, r)
                if u.path == "/api/skills":
                    ws = sess.cfg.get("workspace") or ""
                    op = str(body.get("op", "")) if isinstance(body, dict) else ""
                    name = str(body.get("name", "")) if isinstance(body, dict) else ""
                    if op in ("install", "install_local"):
                        src = str(body.get("url" if op == "install" else "path", ""))
                        res, err = (skills_mod.install(data_dir, ws, src, body.get("scope")) if op == "install" else skills_mod.install_local(data_dir, ws, src, body.get("scope")))
                        return self._send(200 if res and res["added"] else 400, dict(res or {}, error=err or "; ".join("%s: %s" % x for x in (res or {}).get("skipped", []))[:300]))
                    if op == "delete":
                        return self._send(200 if skills_mod.delete(data_dir, ws, name) else 404, {"ok": True})
                    if op == "scan":
                        return self._send(200, {"added": skills_mod.discover(data_dir, ws)})
                    if op == "implicit":
                        x, err = skills_mod.set_field(data_dir, ws, name, implicit=bool(body.get("value")))
                        return self._send(200 if x else 400, {"ok": bool(x), "error": err})
                    if op == "audience":
                        x, err = skills_mod.set_field(data_dir, ws, name, audience=str(body.get("value")))
                        return self._send(200 if x else 400, {"ok": bool(x), "error": err})
                    if op == "export":
                        path, err = skills_mod.export(data_dir, ws, name, str(body.get("value") or "agents"))
                        return self._send(200 if path else 400, {"ok": bool(path), "path": path, "error": err})
                    if op in ("approve", "reject", "pending"):
                        x, err = skills_mod.set_field(data_dir, ws, name, status={"approve": "approved", "reject": "rejected", "pending": "pending"}[op])
                    elif op == "scope":
                        x, err = skills_mod.set_field(data_dir, ws, name, scope=body.get("scope"))
                    else:
                        return self._send(400, {"error": "unknown op"})
                    return self._send(200 if x else 400, {"ok": bool(x), "skill": x, "error": err})
                if u.path == "/api/lessons":
                    ws = sess.cfg.get("workspace") or ""
                    op = body.get("op") if isinstance(body, dict) else ""
                    lid = str(body.get("id", "")) if isinstance(body, dict) else ""
                    if op == "reflect":
                        if sess.busy:
                            return self._send(409, {"error": "busy: wait for the current job"})
                        return self._send(*sess.reflect(False))
                    if op == "delete":
                        return self._send(200 if lessons_mod.delete(data_dir, ws, lid) else 404, {"ok": True})
                    if op == "add":                                                  # the user's own lesson: same generality check, approved at once
                        added, skipped = lessons_mod.add(data_dir, ws, [{k: body.get(k, "") for k in ("title", "rule", "when")}], source="user", scope=body.get("scope"), status="approved")
                        return self._send(200 if added else 400, {"ok": bool(added), "error": (skipped[0][1] if skipped else "")})
                    if op == "auto":
                        x, err = lessons_mod.update(data_dir, ws, lid, auto=bool(body.get("value")))
                        return self._send(200 if x else 400, {"ok": bool(x), "error": err})
                    if op == "to_skill":                                             # several approved lessons -> one SKILL.md, pending like any skill
                        items = [x for x in lessons_mod.list_for(data_dir, ws) if x["status"] == "approved" and (not body.get("ids") or x["id"] in body["ids"])]
                        sname = str(body.get("name", "")).strip().lower().replace(" ", "-")
                        if not items or not skills_mod.NAME.match(sname):
                            return self._send(400, {"error": "need approved lessons and a skill name (a-z, 0-9, -)"})
                        import tempfile, shutil as _sh
                        tmp = tempfile.mkdtemp(prefix="cflesson-")
                        try:
                            os.makedirs(os.path.join(tmp, sname)); open(os.path.join(tmp, sname, "SKILL.md"), "w", encoding="utf-8").write(lessons_mod.export_skill_md(items, sname, str(body.get("description", "")) or "General practices from approved lessons"))
                            m, e = skills_mod.import_folder(data_dir, ws, os.path.join(tmp, sname), "lessons (%d)" % len(items), body.get("scope"))
                        finally:
                            _sh.rmtree(tmp, ignore_errors=True)
                        return self._send(200 if m else 400, {"ok": bool(m), "name": sname, "count": len(items), "error": e})
                    if op in ("approve", "reject", "pending"):
                        x, err = lessons_mod.update(data_dir, ws, lid, status={"approve": "approved", "reject": "rejected", "pending": "pending"}[op])
                    elif op == "scope":
                        x, err = lessons_mod.update(data_dir, ws, lid, scope=body.get("scope"))
                    elif op == "edit":
                        x, err = lessons_mod.update(data_dir, ws, lid, **{k: str(body.get(k, "")) for k in ("title", "rule", "when") if k in body})
                    else:
                        return self._send(400, {"error": "unknown op"})
                    return self._send(200 if x else 400, {"ok": bool(x), "lesson": x, "error": err})
                if u.path == "/api/project":
                    return self._send(*sess.set_project(body.get("path") if isinstance(body, dict) else ""))
                if u.path == "/api/pick_folder":
                    if not sess.picker:
                        return self._send(404, {"error": "no native folder dialog in this mode"})
                    return self._send(200, {"path": sess.picker() or ""})
                cur = sess.cfg
                saved = {a["id"]: a for a in cur.get("advisors") or []}
                if u.path == "/api/open":
                    return self._send(*sess.open_in_viewer())
                if u.path == "/api/advisor_test":
                    a, err = advisors.validate(dict(body.get("advisor") or {}))
                    if err:
                        return self._send(400, {"error": err})
                    if a["type"] == "api":
                        a["key_file"] = (saved.get(a["id"]) or {}).get("key_file")         # never a path chosen by the browser
                    k = str(body.get("key") or "").strip()
                    return self._send(200, advisors.self_test(a, cur, key=k if k else None))
                for a in body.get("advisors") or []:
                    if isinstance(a, dict):
                        a.pop("key_file", None)                                               # the browser never chooses which file is read as a key
                adv_in = body.get("advisors", [{k: v for k, v in a.items() if k != "key_file"} for a in saved.values()])
                clean, err = config.validate_settings({"url": body.get("url", cur["url"]), "model": body.get("model", cur["model"]), "advisors": adv_in, "viewer": body.get("viewer", cur.get("viewer") or {}), "workspace": body.get("workspace", cur.get("workspace") or ""),
                                                       "planner": body.get("planner", cur["planner"]), "worker": body.get("worker", cur.get("worker") or "qwen"), "relay": body.get("relay", cur.get("relay") or {})})
                if err:
                    return self._send(400, {"error": err})
                key = str(body.get("key") or "").strip()
                if len(key) > 500 or re.search(r"[\r\n]", key):
                    return self._send(400, {"error": "key is not valid"})
                if u.path == "/api/test":
                    return self._send(200, llm_mod.probe(clean["url"], clean["model"], key or config.read_key(cur)))
                akeys = {str(k): str(v).strip() for k, v in (body.get("advisor_keys") or {}).items() if isinstance(v, str) and v.strip() and len(v) < 500 and not re.search(r"[\r\n]", v)}
                path = sess.apply_settings(clean, key, akeys)
                self._send(200 if path else 409, {"ok": True, "path": path, "settings": public_settings(sess.cfg)} if path else {"error": "busy: wait for the current job"})
            elif u.path in ("/api/history/load", "/api/history/delete", "/api/history/clear"):
                if not sess.local:
                    return self._send(403, {"error": "saved sessions are only available when the chat is bound to this computer"})
                sid = body.get("id")
                if u.path == "/api/history/clear":
                    if sess.busy:
                        return self._send(409, {"error": "busy"})
                    sess.sid = None
                    return self._send(200, {"ok": True, "deleted": history_mod.clear(data_dir, None if body.get("all") else sess.cfg.get("workspace") or "")})
                if not history_mod.valid(sid):
                    return self._send(400, {"error": "bad id"})
                if u.path == "/api/history/load":
                    return self._send(*sess.load_history(sid))
                if sid == sess.sid:
                    sess.sid = None                                                         # deleting the open conversation: the next message starts a fresh record
                self._send(200, {"ok": history_mod.delete(data_dir, sid)})
            elif u.path == "/api/stop":
                sess.stop = True; self._send(200, {"ok": True})
            elif u.path == "/api/reset":
                self._send(200 if sess.reset() else 409, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
    return H


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(llm, cfg, host="127.0.0.1", port=8765, token=None, backend=None, quiet=False):
    token = token or secrets.token_urlsafe(18)
    extra = [h.strip() for h in os.environ.get("CADFABRIC_HOSTS", "").split(",") if h.strip()]      # names/IPs remote users type, with --allow-remote
    hosts = {("%s:%d" % (h, port)).lower() for h in ("127.0.0.1", "localhost", host, *extra)}
    sess = Session(llm, cfg, backend, local=host in ("127.0.0.1", "localhost", "::1"))
    srv = Server((host, port), make_handler(sess, token, hosts, config.data_dir(cfg)))
    srv.sess, srv.token, srv.port = sess, token, srv.server_address[1]
    srv.hosts = hosts
    if not quiet:
        print("cadfabric web chat: http://%s:%d/?t=%s" % ("127.0.0.1" if host in ("0.0.0.0", "") else host, srv.port, token), flush=True)
    return srv
