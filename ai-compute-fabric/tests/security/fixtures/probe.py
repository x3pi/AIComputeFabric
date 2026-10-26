"""HOSTILE test tool. It tries to break out of the sandbox and reports whether it managed to.
Only the security test registers it. attack_succeeded must always be false."""
import json, os, socket, subprocess, sys, time

req = json.load(sys.stdin)
a, arg = req["action"], req.get("arg", "")


def result(ok, detail=""):
    print(json.dumps({"attack_succeeded": bool(ok), "detail": str(detail)[:120]}))


def reads(path, marker):
    try:
        data = open(path, "rb").read(200000).decode(errors="replace")
        return marker in data, "read ok" if marker in data else "readable but no host marker"
    except Exception as e:
        return False, type(e).__name__


try:
    if a == "read_host_passwd":
        result(*reads("/etc/passwd", arg))
    elif a == "read_host_file":
        result(*reads(arg, "SECRET"))
    elif a == "read_host_environ":
        result(*reads("/proc/self/environ", arg))
    elif a == "read_host_init":
        result(*reads("/proc/1/cmdline", "systemd"))
    elif a == "write_etc":
        open("/etc/pwned", "w").write("x"); result(True, "wrote /etc/pwned")
    elif a == "write_tool_dir":
        open("/tool/evil", "w").write("x"); result(True, "wrote into tool dir")
    elif a == "overwrite_tool":
        open("/tool/tool.py", "a").write("#"); result(True, "modified the tool")
    elif a == "exec_from_tmp":
        open("/tmp/x.sh", "w").write("#!/bin/sh\necho hi\n"); os.chmod("/tmp/x.sh", 0o755)
        r = subprocess.run(["/tmp/x.sh"], capture_output=True); result(r.returncode == 0, "executed from /tmp")
    elif a == "net_connect":
        host, port = arg.split(":"); socket.create_connection((host, int(port)), timeout=2); result(True, "connected")
    elif a == "dns":
        socket.getaddrinfo("example.com", 80); result(True, "resolved")
    elif a == "docker_socket":
        result(os.path.exists("/var/run/docker.sock"), "docker.sock present")
    elif a == "mount":
        r = subprocess.run(["mount", "-t", "tmpfs", "none", "/mnt"], capture_output=True); result(r.returncode == 0, "mounted")
    elif a == "setuid":
        os.setuid(0); result(os.getuid() == 0, "became root")
    elif a == "capabilities":
        eff = [l for l in open("/proc/self/status") if l.startswith("CapEff")][0].split()[1]
        result(int(eff, 16) != 0, "CapEff=" + eff)
    elif a == "fork_bomb":
        kids = []
        for _ in range(400):
            try:
                kids.append(subprocess.Popen(["sleep", "5"]))
            except Exception:
                break
        n = len(kids)
        for k in kids:
            k.kill()
        result(n > 100, f"started {n} processes")
    elif a == "mem_hog":
        blob = bytearray(1500 * 1024 * 1024)
        for i in range(0, len(blob), 4096):
            blob[i] = 1
        result(True, "allocated 1.5 GB")
    elif a == "cpu_loop":
        while True:
            pass
    elif a == "huge_stdout":
        sys.stdout.write("A" * (5 * 1024 * 1024))
    elif a == "symlink_escape":
        os.symlink(arg, "/tmp/l"); result(*reads("/tmp/l", "SECRET"))
    elif a == "sleep_forever":
        time.sleep(3600)
    else:
        result(False, "unknown action")
except Exception as e:
    result(False, type(e).__name__ + ": " + str(e)[:60])
