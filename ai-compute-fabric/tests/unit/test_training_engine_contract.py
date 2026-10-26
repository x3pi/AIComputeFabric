"""P1.2: the TrainingEngine contract, checked on a numeric fake and on LoraWorker's method surface."""
import numpy as np

from training.coordinator import Coordinator, Update
from training.engine import TrainingEngine


class QuadraticEngine:
    """Minimises ||w - target||^2 by SGD with a noisy gradient: the smallest thing that satisfies the contract."""
    def __init__(self, target, lr=0.1):
        self.target, self.lr, self.w = np.asarray(target, dtype=np.float64), lr, {"w": np.zeros(len(target))}

    def get_params(self):
        return {k: v.copy() for k, v in self.w.items()}

    def set_params(self, params):
        for k, v in params.items():
            if v.shape != self.w[k].shape:
                raise ValueError("shape")
            self.w[k] = v.copy()

    def local_steps(self, h, seed):
        rng, losses = np.random.default_rng(seed), []
        for _ in range(h):
            g = 2 * (self.w["w"] - self.target) + rng.normal(0, 0.1, self.target.shape)
            losses.append(float(np.sum((self.w["w"] - self.target) ** 2)))
            self.w["w"] = self.w["w"] - self.lr * g
        return losses

    def evaluate(self, texts, tok_dir):
        nll = float(np.sum((self.w["w"] - self.target) ** 2))
        return {"nll": nll, "perplexity": float(np.exp(nll)), "tokens": 1}


def check_contract(e):
    assert isinstance(e, TrainingEngine)
    p = e.get_params()
    p0 = {k: v.copy() for k, v in p.items()}
    for v in p.values():
        v += 123                                   # mutating the returned arrays must not touch the engine
    assert all(np.array_equal(a, b) for a, b in zip(e.get_params().values(), p0.values()))
    a = e.local_steps(5, seed=7)
    after_a = e.get_params()
    e.set_params(p0)
    b = e.local_steps(5, seed=7)
    assert len(a) == 5 and all(isinstance(x, float) for x in a)
    assert a == b and all(np.array_equal(after_a[k], e.get_params()[k]) for k in p0), "same params + same seed must give the same steps"
    assert {"nll", "perplexity", "tokens"} <= set(e.evaluate([], "."))


def test_fake_engine_satisfies_the_contract():
    check_contract(QuadraticEngine([1.0, -2.0, 0.5]))


def test_two_engines_converge_through_the_coordinator():
    target = np.array([1.0, -2.0, 0.5])
    engines = [QuadraticEngine(target) for _ in range(2)]
    co = Coordinator({"w": np.zeros(3)}, outer_lr=0.7, momentum=0.9, min_workers=2)
    for rnd in range(30):
        ver, params = co.pull()
        for k, e in enumerate(engines):
            e.set_params(params)
            e.local_steps(10, seed=rnd * 10 + k)
            co.submit(Update(f"w{k}", ver, 10, {n: v - params[n] for n, v in e.get_params().items()}))
        assert co.outer_step()
    assert np.max(np.abs(co.params["w"] - target)) < 0.1


def test_an_object_missing_a_method_is_not_a_training_engine():
    class NoEval:
        get_params = set_params = local_steps = lambda *a, **k: None
    assert not isinstance(NoEval(), TrainingEngine)


def test_lora_worker_exposes_the_contract_surface():
    from training.lora_trainer import LoraWorker
    assert issubclass(LoraWorker, TrainingEngine)
