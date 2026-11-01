import argparse, os, sys
from .llm import LLM
from .audit import Audit
from .loop import Agent

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="vd http://10.42.0.1:18090/v1")
    p.add_argument("--model", default="")
    p.add_argument("--key", default=os.environ.get("WAGENT_KEY", ""))
    p.add_argument("--sandbox", default=r"C:\wagent\sandbox")
    p.add_argument("--audit", default=r"C:\wagent\audit.jsonl")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--approve", choices=["ask", "deny", "allow"], default="deny")
    p.add_argument("--approvals-dir", default=r"C:\wagent\approvals")
    p.add_argument("--stop-file", default=r"C:\wagent\STOP")
    p.add_argument("--max-seconds", type=int, default=600)
    p.add_argument("task")
    a = p.parse_args()
    os.makedirs(a.sandbox, exist_ok=True)
    from .skills.uia import UIA
    from .approver import FileApprover, DenyAll, AllowAll
    ap = {"ask": lambda: FileApprover(a.approvals_dir), "deny": DenyAll, "allow": AllowAll}[a.approve]()
    st, sm = Agent(LLM(a.url, a.model, a.key), UIA(), Audit(a.audit), a.sandbox, a.max_steps,
                   approver=ap, stop_file=a.stop_file, max_seconds=a.max_seconds).run(a.task)
    print("KET THUC:", st, "-", sm)
    sys.exit(0 if st == "done" else 1)

if __name__ == "__main__":
    main()
