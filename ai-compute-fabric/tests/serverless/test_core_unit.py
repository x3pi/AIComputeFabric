"""Serverless core: requirement validation, env hash, container command hardening, tenant keys (no Docker needed)."""
import pytest

from agents.env_cache import EnvError, EnvSpec, normalize_requirement
from agents.sandbox import GpuDockerSandbox, SandboxError, ServerlessLimits, WarmKey, validate_env

H = "a" * 64


@pytest.mark.parametrize("req,expect", [
    ("numpy==2.5.4", "numpy==2.5.4"), ("Pillow>=10,<12", "pillow>=10,<12"), ("torch[cuda]==2.7.0", "torch[cuda]==2.7.0"),
    ("My_Pkg.Name~=1.2", "my-pkg-name~=1.2"), ("requests", "requests"),
])
def test_requirement_accepted_and_normalised(req, expect):
    assert normalize_requirement(req) == expect


@pytest.mark.parametrize("bad", [
    "git+https://github.com/x/y", "./local", "/abs/path", "-r other.txt", "--index-url=http://evil", "numpy @ http://x/a.whl",
    "numpy; os_name=='nt'", "a b", "a;b", "$(id)", "numpy`id`", "", "../x", "numpy==1.0 --hash=sha256:aa", "http://x/y.whl", "numpy\nrequests",
    "pkg'; rm -rf /; '", "x" * 200, "a==1,", "==1",
])
def test_requirement_rejected(bad):
    with pytest.raises(EnvError):
        normalize_requirement(bad)


def test_env_hash_is_deterministic_and_order_insensitive():
    a = EnvSpec(packages=("numpy==2.5.4", "pillow"))
    b = EnvSpec(packages=("Pillow", "numpy==2.5.4"))
    assert a.env_hash() == b.env_hash() and len(a.env_hash()) == 64


def test_env_hash_changes_with_every_input():
    base = EnvSpec(packages=("numpy",))
    hs = {base.env_hash(), EnvSpec(packages=("numpy", "pillow")).env_hash(), EnvSpec(packages=("numpy",), cuda_tag="cu130").env_hash(),
          EnvSpec(packages=("numpy",), local_source_hash="b" * 64).env_hash(), EnvSpec(packages=("numpy==2.5.4",)).env_hash()}
    assert len(hs) == 5


def test_runtime_package_always_present():
    assert "cloudpickle==3.1.2" in EnvSpec().normalized()


def test_env_spec_rejects_unknown_python_cuda_and_hash():
    with pytest.raises(EnvError):
        EnvSpec(python_version="2.7").env_hash()
    with pytest.raises(EnvError):
        EnvSpec(cuda_tag="cu130; rm").env_hash()
    with pytest.raises(EnvError):
        EnvSpec(local_source_hash="zz").env_hash()
    with pytest.raises(EnvError):
        EnvSpec(packages=tuple(f"p{i}" for i in range(65))).env_hash()


@pytest.mark.parametrize("k", ["NVIDIA_VISIBLE_DEVICES", "LD_PRELOAD", "PYTHONPATH", "AICF_VRAM_MB", "PATH", "HOME", "DOCKER_HOST", "lower", "A B", "CUDA_VISIBLE_DEVICES"])
def test_env_denied(k):
    with pytest.raises(SandboxError):
        validate_env({k: "x"})


def test_env_allowed_and_value_checks():
    assert validate_env({"OMP_NUM_THREADS": "4", "MY_FLAG": "1"}) == {"OMP_NUM_THREADS": "4", "MY_FLAG": "1"}
    for v in ("a\nb", "x" * 2000, "a\x00b"):
        with pytest.raises(SandboxError):
            validate_env({"OK": v})


class _Sb(GpuDockerSandbox):
    def __init__(self):               # no docker probing in unit tests
        self.image, self.no_new_privileges = "python:3.12-slim", True


def _cmd(**kw):
    lim = kw.pop("limits", ServerlessLimits())
    return _Sb().warm_command(kw.pop("name", "aicf-sl-abc"), kw.pop("key", WarmKey("tenant-a", H, "fn1")), kw.pop("env_dir", "/store/envs/" + H), lim, kw.pop("env", None))


def test_command_hardening_flags_present():
    c = _cmd()
    s = " ".join(c)
    for must in ("--network none", "--read-only", "--cap-drop ALL", "--user 65534:65534", "--pids-limit", "--memory-swap", "no-new-privileges", "--rm"):
        assert must in s, must
    for never in ("--privileged", "--gpus", "docker.sock", "--cap-add", "--pid host", "--net host", "--network host", "--device"):
        assert never not in s, never
    assert "--runtime=nvidia" not in s            # CPU function: no GPU


def test_command_mounts_are_exactly_env_ro_and_runner_ro():
    c = _cmd()
    mounts = [c[i + 1] for i, a in enumerate(c) if a == "-v"]
    assert len(mounts) == 2 and all(m.endswith(":ro") for m in mounts)
    assert mounts[0].endswith(":/env:ro") and mounts[1].endswith(":/aicf/runner.py:ro")


def test_command_gpu_uses_runtime_nvidia_and_only_selected_index():
    c = _cmd(limits=ServerlessLimits(gpu_index=1, vram_mb=4096))
    assert "--runtime=nvidia" in c and "NVIDIA_VISIBLE_DEVICES=1" in c and "AICF_VRAM_MB=4096" in c
    assert "NVIDIA_VISIBLE_DEVICES=all" not in c


def test_tenant_cannot_override_gpu_visibility_via_env():
    with pytest.raises(SandboxError):
        _cmd(limits=ServerlessLimits(gpu_index=0), env={"NVIDIA_VISIBLE_DEVICES": "all"})


def test_command_labels_and_name_are_validated():
    c = _cmd()
    assert "aicf.sl=1" in c and "aicf.tenant=tenant-a" in c
    for bad in ("t;rm -rf /", "a b", "../x", "t$(id)", "", "x" * 80, "--privileged"):
        with pytest.raises(SandboxError):
            _cmd(key=WarmKey(bad, H, "fn1"))
        with pytest.raises(SandboxError):
            _cmd(key=WarmKey("t", H, bad))
    with pytest.raises(SandboxError):
        _cmd(key=WarmKey("t", "nothex", "fn1"))
    with pytest.raises(SandboxError):
        _cmd(name="x;y")


def test_env_built_for_another_image_is_refused(tmp_path):
    (tmp_path / "spec.json").write_text('{"image": "python:3.12-alpine"}')
    with pytest.raises(SandboxError, match="ABI"):
        _cmd(env_dir=tmp_path)
    (tmp_path / "spec.json").write_text('{"image": "python:3.12-slim"}')
    assert "python:3.12-slim" in _cmd(env_dir=tmp_path)


def test_default_image_is_glibc_not_alpine():
    assert GpuDockerSandbox.__init__.__defaults__[0] == "python:3.12-slim"


def test_warm_key_includes_tenant():
    assert WarmKey("a", H, "f") != WarmKey("b", H, "f")
    assert len({WarmKey("a", H, "f"), WarmKey("b", H, "f")}) == 2


def test_builder_command_is_wheels_only_hash_pinned_and_hardened(tmp_path, monkeypatch):
    """The builder is the only place that touches the network and installs packages: assert exactly how it is invoked."""
    import subprocess
    from agents.env_cache import EnvCache
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    c = EnvCache(tmp_path / "store", uv="/bin/true")
    c._build("python:3.12-slim", ("cloudpickle==3.1.2", "numpy==2.5.4"), "x.tmp")
    cmd = seen["cmd"]
    shell = cmd[-1]
    assert "--generate-hashes" in shell and "--require-hashes" in shell
    assert shell.count("--only-binary :all:") == 2                       # compile AND install
    assert "--no-binary" not in shell and "--index-url" not in shell and "--extra-index-url" not in shell
    s = " ".join(cmd)
    for must in ("--cap-drop ALL", "--user 65534:65534", "--read-only", "--memory 4g", "--pids-limit 256"):
        assert must in s
    for never in ("--privileged", "--network host", "docker.sock"):
        assert never not in s
    assert [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"] == ["/bin/true:/usr/local/bin/uv:ro", f"{c.store}:/store"]


def test_builder_refuses_unsafe_characters_even_if_validation_were_bypassed(tmp_path):
    from agents.env_cache import EnvCache
    c = EnvCache(tmp_path / "store", uv="/bin/true")
    with pytest.raises(EnvError):
        c._build("python:3.12-slim", ("numpy'; rm -rf /; '",), "x.tmp")
