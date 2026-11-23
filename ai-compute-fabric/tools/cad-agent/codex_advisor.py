#!/usr/bin/env python3
"""Codex CLI as a TEXT-ONLY advisor, run inside a bubblewrap sandbox so it cannot read this machine's files.

Why a sandbox: `codex exec -s read-only` still lets the model READ any file the user can read; "do not read files" in the prompt is only a request.
Here the process sees: system libraries (read-only), an EMPTY working directory, an empty HOME, the network, and the one login file it needs.
No project data, no ssh keys, no other home files, no environment variables of the caller.

    python3 codex_advisor.py "question"            # prints the answer
    from codex_advisor import ask; text, err = ask("question")

Backends (first that works): `docker` (a throw-away alpine container: no mounts except the codex binary and the login file, no capabilities,
read-only root, memory/pid limits) or `bwrap` (bubblewrap; needs unprivileged user namespaces, which Ubuntu 24.04 blocks by default).
Linux only; needs the `codex` CLI already logged in. Python standard library only."""
import os, shutil, subprocess, sys, tempfile

def _find_codex():
    if os.environ.get("AICF_CODEX_BIN"):
        return os.environ["AICF_CODEX_BIN"]
    if shutil.which("codex"):
        return shutil.which("codex")
    if sys.platform == "win32":                                   # the Codex desktop app keeps its CLI outside PATH
        import glob
        hits = sorted(glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin", "*", "codex.exe")), key=os.path.getmtime)
        if hits:
            return hits[-1]
    return os.path.expanduser("~/.local/bin/codex")


CODEX = _find_codex()
AUTH = os.path.join(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"), "auth.json")
PREFIX = ("You are an advisor for a CAD automation agent. Answer ONLY from the text below, in plain text, concisely. "
          "Do not run any command, do not read or write any file, do not browse.\n\n")


IMAGE = os.environ.get("AICF_CODEX_IMAGE") or "alpine:latest"


def _docker_ready():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _bwrap_ready():
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--ro-bind", "/usr", "/usr", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def backend():
    if sys.platform == "win32":
        # No container here: Codex runs in an EMPTY temp directory with a read-only sandbox and a text-only prompt. Weaker than the Linux
        # container (the model could still try to read files by path), so the caller must treat the question as leaving the machine.
        return "windows-direct" if os.path.isfile(CODEX) else None
    if not (sys.platform.startswith("linux") and os.path.isfile(os.path.realpath(CODEX)) and os.path.isfile(AUTH)):
        return None
    return "docker" if _docker_ready() else "bwrap" if _bwrap_ready() else None


def available():
    return backend() is not None


def docker_argv(codex_real, prompt, extra=()):
    args = ["/opt/codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", "/work/cwd", *extra, PREFIX + prompt]
    return ["docker", "run", "--rm", "--pull=never", "--read-only", "--cap-drop", "ALL", "--pids-limit", "256", "--memory", "1g",
            "-u", "%d:%d" % (os.getuid(), os.getgid()), "--tmpfs", "/work:rw,exec,uid=%d,gid=%d,mode=755" % (os.getuid(), os.getgid()), "--tmpfs", "/tmp",
            "-v", "%s:/opt/codex:ro" % codex_real, "-v", "%s:/auth/auth.json:ro" % os.path.realpath(AUTH),
            "-e", "HOME=/work/home", "-e", "CODEX_HOME=/work/codex_home", "-w", "/work", IMAGE,
            "sh", "-c", 'mkdir -p /work/cwd /work/home /work/codex_home && cp /auth/auth.json /work/codex_home/auth.json && exec "$@"', "sh", *args]


def sandbox_argv(codex_real, prompt, extra=()):
    a = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent", "--new-session", "--clearenv",
         "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
         "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/work", "--dir", "/work/cwd", "--dir", "/work/home", "--dir", "/work/codex_home"]
    for p in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl", "/etc/ca-certificates", "/etc/localtime"):
        if os.path.exists(p):
            a += ["--ro-bind", os.path.realpath(p) if os.path.islink(p) else p, p]
    a += ["--tmpfs", "/opt", "--ro-bind", codex_real, "/opt/codex",
          "--bind", os.path.realpath(AUTH), "/work/codex_home/auth.json",          # writable: the login may refresh its token
          "--setenv", "HOME", "/work/home", "--setenv", "CODEX_HOME", "/work/codex_home", "--setenv", "PATH", "/usr/bin:/bin",
          "--chdir", "/work/cwd", "--",
          "/opt/codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", "/work/cwd",
          *extra, PREFIX + prompt]
    return a


def ask(prompt, timeout=240, extra=()):
    """-> (reply text, error). Never raises."""
    if not available():
        return "", "codex advisor not available on this machine (needs Linux, docker or working bwrap, and a logged-in codex)"
    try:
        be = backend()
        if be == "windows-direct":
            with tempfile.TemporaryDirectory(prefix="aicf-codex-") as wd:
                r = subprocess.run([CODEX, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-C", wd, *extra, PREFIX + prompt[:30000]],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, cwd=wd)
            return _result(r)
        argv = (docker_argv if be == "docker" else sandbox_argv)(os.path.realpath(CODEX), prompt[:30000], extra)
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "", "codex advisor timed out after %ds" % timeout
    except OSError as e:
        return "", "codex advisor could not start (%s)" % type(e).__name__
    return _result(r)


def _result(r):
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        return "", "codex advisor failed (exit %d: %s)" % (r.returncode, tail[0][:120])
    return out, ""


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "Reply with the single word OK."
    if q.startswith("@") and os.path.isfile(q[1:]):          # @file: the question is in a file (avoids shell quoting limits over ssh)
        q = open(q[1:], encoding="utf-8").read()
    text, err = ask(q)
    print(text if text else "ERROR: " + err)
