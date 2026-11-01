import subprocess, sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import p11_2gpu_runner as P
import p12_s0_repro as S0
P.setup_wsl_flat_network()
src = Path(__file__).with_name("p12_s2_netfloor.py").read_text()
P.run_wsl_bash("mkdir -p /opt/p12 && cat > /opt/p12/netfloor.py <<'PYEOF'\n" + src + "\nPYEOF")
env = os.environ.copy(); env.update(GLOO_SOCKET_IFNAME="enp133s0", NCCL_SOCKET_IFNAME="enp133s0", NCCL_IB_DISABLE="1", NCCL_P2P_DISABLE="1")
py0 = str(P.WORK_ROOT / "venv-sglang" / "bin" / "python")
p0 = subprocess.Popen([py0, str(Path(__file__).with_name("p12_s2_netfloor.py")), "0", P.LINUX_IP], env=env,
                      stdout=open(S0.P12_LOGS / "netfloor_r0.log", "w"), stderr=subprocess.STDOUT)
cmd1 = f"""{P.WSL_NET_INIT_SNIPPET}
export GLOO_SOCKET_IFNAME=nccl0 NCCL_SOCKET_IFNAME=nccl0 NCCL_IB_DISABLE=1 NCCL_P2P_DISABLE=1
exec /opt/venv-sglang/bin/python /opt/p12/netfloor.py 1 {P.LINUX_IP}
"""
p1 = subprocess.Popen(["ssh", "-o", "BatchMode=yes", P.SSH_HOST, f"{P.WSL_EXE} -d Ubuntu-24.04 -u root -- bash -s"],
                      stdin=subprocess.PIPE, stdout=open(S0.P12_LOGS / "netfloor_r1.log", "w"), stderr=subprocess.STDOUT, text=True)
p1.stdin.write(cmd1 + "\n"); p1.stdin.close()
print("rc", p0.wait(timeout=420), p1.wait(timeout=60))
