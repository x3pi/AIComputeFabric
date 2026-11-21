"""cadfabric command line.
  cadfabric init            write a starter config (never writes a key)
  cadfabric doctor          check CadQuery, the worker, the gateway and the chosen planner
  cadfabric run "request"   build a part from a request; --step out.step exports it; --planner overrides the config
  cadfabric bench           run the benchmark tasks (--modes off,codex --reps 3 --tasks a,b --out file.jsonl)
  cadfabric tasks           list the benchmark tasks and their kernel-computed answers"""
import argparse, json, os, sys, time
from . import __version__, config


def cmd_init(a):
    path = a.path or os.path.join(config.config_dir(), "config.json")
    if os.path.exists(path) and not a.force:
        print("exists: %s (use --force to overwrite)" % path); return 1
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = {"url": a.url, "model": a.model, "key_file": a.key_file, "planner": a.planner,
           "relay": {"host": "", "workdir": "", "codex": "codex"}, "codex_bin": None, "data_dir": None}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2); f.write("\n")
    print("wrote %s\nSet key_file to a file that holds your API key (or export CADFABRIC_KEY). No key is stored in this file." % path)
    return 0


def cmd_doctor(a):
    ok = True
    def line(good, what, detail=""):
        nonlocal ok
        ok = ok and good
        print("%s %-22s %s" % ("OK  " if good else "FAIL", what, detail))
    print("cadfabric %s, python %s" % (__version__, sys.version.split()[0]))
    cfg = config.load()
    line(True, "config", cfg["_path"] or "(defaults; run `cadfabric init`)")
    try:
        import cadquery
        line(True, "cadquery", cadquery.__version__)
    except Exception as e:                                                  # noqa: BLE001
        line(False, "cadquery", "%s: %s -- pip install cadquery" % (type(e).__name__, e)); return 1
    from .cq_backend import CadQueryBackend
    be = CadQueryBackend(timeout=60)
    try:
        r = be.execute("result = cq.Workplane('XY').box(10, 10, 10)")
        line(bool(r.ok and r.measure and abs(r.measure.volume - 1000) < 1e-6), "worker + kernel", "box 10x10x10 -> %s" % (r.measure.text()[:40] if r.measure else r.error))
        r = be.execute("import os")
        line(not r.ok, "sandbox", "import of os is blocked" if not r.ok else "import os was NOT blocked")
    finally:
        be.close()
    from .llm import LLM
    try:
        m = LLM(cfg, timeout=120).chat([{"role": "user", "content": "Reply with the single word OK."}], max_tokens=20)
        line(True, "gateway", "%s -> %r" % (cfg["url"], m["content"][:20]))
    except Exception as e:                                                  # noqa: BLE001
        line(False, "gateway", str(e)[:160])
    from . import planners
    if cfg["planner"] == "codex":
        exe = planners.find_codex(cfg); line(bool(exe), "codex (local)", exe or "not found; install the Codex CLI and log in")
    elif cfg["planner"] == "codex-relay":
        t, e = planners.ask_relay("Reply with the single word OK.", cfg, timeout=90); line(bool(t), "codex relay", (t or e)[:100])
    else:
        line(True, "planner", cfg["planner"])
    return 0 if ok else 1


def cmd_run(a):
    from .cq_backend import CadQueryBackend
    from .llm import LLM
    from . import agent
    cfg = config.load()
    mode = a.planner or cfg["planner"]
    be, llm = CadQueryBackend(), LLM(cfg)
    try:
        r = agent.run(llm, be, " ".join(a.request), plan_mode=mode, log=print, cfg=cfg)
        print("\n" + (r["final"] or "(no final answer: step limit reached)"))
        m = r["measure"]
        if m:
            print("\nMEASURED: volume %.4f mm3, bbox %s, solids %d, faces %d, valid %s" % (m["volume"], [round(x, 3) for x in m["bbox_size"]], m["solids"], m["faces"], m["valid"]))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = a.step or (os.path.join(config.data_dir(cfg), "part-%s.step" % stamp) if m else None)
        if out and m:
            be.export_step(out); print("STEP: %s" % out)
        with open(os.path.join(config.data_dir(cfg), "run-%s.json" % stamp), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in r.items() if k != "trace"} | {"trace": r["trace"], "planner": mode}, f, ensure_ascii=False, indent=1)
        return 0 if m and not r["ended_by_limit"] else 2
    finally:
        be.close()


def cmd_tasks(a):
    from . import tasks
    from .cq_backend import CadQueryBackend
    be = CadQueryBackend()
    try:
        for t in tasks.TASKS:
            print("%-18s %12.4f mm3  tol %.2g%%  %s" % (t, tasks.exact(be, t), 100 * tasks.TASKS[t]["tol"], tasks.TASKS[t]["text"][:70]))
    finally:
        be.close()
    return 0


def cmd_bench(a):
    from .llm import LLM
    from . import bench, tasks
    cfg = config.load()
    ids = a.tasks.split(",") if a.tasks else list(tasks.TASKS)
    out = a.out or os.path.join(config.data_dir(cfg), "bench-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    bench.run_bench(LLM(cfg), a.modes.split(","), a.reps, ids, out, cfg=cfg)
    print("results: " + out)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cadfabric", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--path"); p.add_argument("--force", action="store_true")
    p.add_argument("--url", default=config.DEFAULTS["url"]); p.add_argument("--model", default=""); p.add_argument("--key-file", dest="key_file"); p.add_argument("--planner", default="nothink", choices=config.PLANNERS)
    p.set_defaults(fn=cmd_init)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("run"); p.add_argument("request", nargs="+"); p.add_argument("--planner", choices=config.PLANNERS); p.add_argument("--step"); p.set_defaults(fn=cmd_run)
    sub.add_parser("tasks").set_defaults(fn=cmd_tasks)
    p = sub.add_parser("bench"); p.add_argument("--modes", default="off,nothink"); p.add_argument("--reps", type=int, default=3); p.add_argument("--tasks"); p.add_argument("--out"); p.set_defaults(fn=cmd_bench)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
