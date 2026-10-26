"""P6.3 gates: stale updates are blocked 100 %, a resumed run matches an uninterrupted one exactly,
checkpoints survive corruption, and DiLoCo moves far fewer bytes than DDP (analytic count)."""

import numpy as np
import pytest

from training.coordinator import Coordinator, Rejected, Update, comm_bytes_per_worker

SHAPES = {"w": (4, 3), "b": (3,)}


def fresh(**kw):
    rng = np.random.default_rng(0)
    return Coordinator({k: rng.normal(size=s) for k, s in SHAPES.items()}, **kw)


def local_work(theta, seed, steps=5):
    """Stand-in for H inner steps: a deterministic pseudo-update."""
    rng = np.random.default_rng(seed)
    return {k: 0.01 * steps * rng.normal(size=theta[k].shape) for k in sorted(theta)}   # sorted: a loaded checkpoint orders tensors by name


def run_round(c, workers=("a", "b", "c"), seed_base=0):
    ver, theta = c.pull()
    for i, w in enumerate(workers):
        c.submit(Update(w, ver, 5, local_work(theta, seed_base + ver * 100 + i)))
    assert c.outer_step()


def test_every_stale_update_is_rejected_and_nothing_changes():
    c = fresh()
    for r in range(3):
        run_round(c)
    before = {k: v.copy() for k, v in c.params.items()}
    ver, theta = c.pull()
    stale = 0
    for base in range(ver):                                 # every older version, from several workers
        for w in ("a", "b", "z"):
            with pytest.raises(Rejected) as e:
                c.submit(Update(w, base, 5, local_work(theta, 1)))
            assert e.value.reason == "stale"
            stale += 1
    assert c.rejected["stale"] == stale == ver * 3
    assert c.pending == {} and all(np.array_equal(before[k], c.params[k]) for k in before)


@pytest.mark.parametrize("bad,reason", [
    (lambda c, th: Update("a", c.version + 1, 5, local_work(th, 1)), "from_the_future"),
    (lambda c, th: Update("a", c.version, 0, local_work(th, 1)), "no_local_steps"),
    (lambda c, th: Update("a", c.version, 5, {"w": th["w"]}), "wrong_tensors"),
    (lambda c, th: Update("a", c.version, 5, {"w": np.zeros((9, 9)), "b": np.zeros(3)}), "wrong_shape"),
    (lambda c, th: Update("a", c.version, 5, {"w": np.full((4, 3), np.nan), "b": np.zeros(3)}), "not_finite"),
    (lambda c, th: Update("a", c.version, 5, {"w": np.full((4, 3), np.inf), "b": np.zeros(3)}), "not_finite"),
    (lambda c, th: Update("a", c.version, 5, {"w": np.full((4, 3), 1e9), "b": np.zeros(3)}), "norm_too_large"),
    (lambda c, th: Update("a", True, 5, local_work(th, 1)), "bad_version"),
])
def test_malformed_updates_are_rejected_with_a_reason(bad, reason):
    c = fresh()
    _, th = c.pull()
    with pytest.raises(Rejected) as e:
        c.submit(bad(c, th))
    assert e.value.reason == reason and c.pending == {}


def test_one_update_per_worker_per_round_and_quorum_is_required():
    c = fresh(min_workers=3)
    ver, th = c.pull()
    c.submit(Update("a", ver, 5, local_work(th, 1)))
    with pytest.raises(Rejected) as e:
        c.submit(Update("a", ver, 5, local_work(th, 2)))      # the same worker cannot vote twice
    assert e.value.reason == "duplicate"
    c.submit(Update("b", ver, 5, local_work(th, 3)))
    assert not c.outer_step() and c.version == 0              # 2 of 3: no step
    c.submit(Update("c", ver, 5, local_work(th, 4)))
    assert c.outer_step() and c.version == 1 and c.pending == {}


def test_outer_step_moves_parameters_along_the_average_delta():
    c = fresh(outer_lr=1.0, momentum=0.0, min_workers=2)       # plain averaging when momentum is 0
    ver, th = c.pull()
    d1, d2 = local_work(th, 1), local_work(th, 2)
    c.submit(Update("a", ver, 5, d1))
    c.submit(Update("b", ver, 5, d2))
    c.outer_step()
    for k in th:
        assert np.allclose(c.params[k], th[k] + (d1[k] + d2[k]) / 2)


def test_resume_after_a_crash_gives_bit_identical_results(tmp_path):
    ref = fresh()
    for r in range(8):
        run_round(ref)
    a = fresh()
    for r in range(5):
        run_round(a)
        a.save(tmp_path)
    del a                                                       # crash: process memory is gone
    b = Coordinator.load_latest(tmp_path)
    assert b.version == 5
    for r in range(3):
        run_round(b)
    assert b.version == ref.version == 8
    for k in ref.params:
        assert np.array_equal(ref.params[k], b.params[k]), "parameters differ after resume"
        assert np.array_equal(ref.velocity[k], b.velocity[k]), "optimizer state differs after resume"
    assert b.history == ref.history


def test_a_corrupted_newest_checkpoint_falls_back_and_a_torn_write_is_never_visible(tmp_path):
    c = fresh()
    for r in range(3):
        run_round(c)
        c.save(tmp_path)
    newest = sorted(tmp_path.glob("ckpt-*.npz"))[-1]
    data = bytearray(newest.read_bytes())
    data[len(data) // 2] ^= 0xFF
    newest.write_bytes(bytes(data))
    assert Coordinator.load_latest(tmp_path).version == 2      # version 3 file is corrupt: use version 2
    for p in tmp_path.glob("*.npz"):
        p.write_bytes(b"junk")
    with pytest.raises(FileNotFoundError):
        Coordinator.load_latest(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))                     # no half-written temp files left behind
    assert len(list(tmp_path.glob("ckpt-*.npz"))) <= 2


def test_only_two_checkpoint_generations_are_kept(tmp_path):
    c = fresh()
    for r in range(6):
        run_round(c)
        c.save(tmp_path)
    assert [p.name for p in sorted(tmp_path.glob("ckpt-*.npz"))] == ["ckpt-00000005.npz", "ckpt-00000006.npz"]


def test_diloco_moves_far_fewer_bytes_than_ddp():
    P, steps, n = 1_000_000_000, 10_000, 4
    ddp = comm_bytes_per_worker(P, steps, n, None)
    for h, floor in ((50, 0.90), (100, 0.95), (500, 0.99)):
        saving = 1 - comm_bytes_per_worker(P, steps, n, h) / ddp
        assert saving >= floor, (h, saving)
    # the 70 % gate fails for very frequent sync: it needs H of at least about 7 here, not just "DiLoCo"
    assert 1 - comm_bytes_per_worker(P, steps, n, 2) / ddp < 0.70
    assert 1 - comm_bytes_per_worker(P, steps, n, 8) / ddp >= 0.70


def test_nesterov_outer_update_matches_a_hand_computed_two_round_run():
    lr, mu = 0.5, 0.9
    c = fresh(outer_lr=lr, momentum=mu, min_workers=1)
    theta = {k: v.copy() for k, v in c.params.items()}
    vel = {k: np.zeros_like(v) for k, v in theta.items()}
    for rnd in range(2):
        ver, th = c.pull()
        d = local_work(th, 40 + rnd)
        c.submit(Update("a", ver, 5, d))
        c.outer_step()
        for k in theta:                                   # DiLoCo: pseudo-gradient g = -delta, SGD with Nesterov momentum
            g = -d[k]
            vel[k] = mu * vel[k] + g
            theta[k] = theta[k] - lr * (g + mu * vel[k])
    for k in theta:
        assert np.allclose(c.params[k], theta[k], atol=1e-12)
        assert np.allclose(c.velocity[k], vel[k], atol=1e-12)
