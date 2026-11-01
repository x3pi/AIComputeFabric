import argparse, os, sys
from .llm import LLM
from .audit import Audit
from .loop import Agent

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="", help="vd http://10.42.0.1:18090/v1")
    p.add_argument("--model", default="")
    p.add_argument("--key", default=os.environ.get("WAGENT_KEY", ""))
    p.add_argument("--sandbox", default=r"C:\wagent\sandbox")
    p.add_argument("--audit", default=r"C:\wagent\audit.jsonl")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--approve", choices=["ask", "deny", "allow"], default="deny")
    p.add_argument("--approvals-dir", default=r"C:\wagent\approvals")
    p.add_argument("--stop-file", default=r"C:\wagent\STOP")
    p.add_argument("--max-seconds", type=int, default=600)
    p.add_argument("--web", type=int, default=0, help="chay UI web tren 127.0.0.1:PORT")
    p.add_argument("task", nargs="?", default="")
    a = p.parse_args()
    os.makedirs(a.sandbox, exist_ok=True)
    if a.web:
        from .web import serve
        from .skills.uia import UIA
        def make_agent(cfg, log, approver, stop):
            return Agent(LLM(cfg["url"], cfg.get("model", ""), cfg.get("key", "")), UIA(), Audit(a.audit),
                         a.sandbox, a.max_steps, log=log, approver=approver, stop_event=stop, max_seconds=a.max_seconds)
        srv = serve(make_agent, port=a.web)
        print("UI: http://127.0.0.1:%d/?t=%s" % (a.web, srv.token), flush=True)
        srv.serve_forever()
        return
    if not a.task or not a.url:
        p.error("can nhiem vu va --url (hoac dung --web)")
    from .skills.uia import UIA
    from .approver import FileApprover, DenyAll, AllowAll
    ap = {"ask": lambda: FileApprover(a.approvals_dir), "deny": DenyAll, "allow": AllowAll}[a.approve]()
    st, sm = Agent(LLM(a.url, a.model, a.key), UIA(), Audit(a.audit), a.sandbox, a.max_steps,
                   approver=ap, stop_file=a.stop_file, max_seconds=a.max_seconds).run(a.task)
    print("KET THUC:", st, "-", sm)
    sys.exit(0 if st == "done" else 1)

if __name__ == "__main__":
    main()
