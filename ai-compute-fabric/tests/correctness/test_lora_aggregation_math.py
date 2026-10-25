import numpy as np

from training.lora_merger import exact_delta_w, lora_fa_merge, naive_merge, svd_refactor

K, D_OUT, D_IN, R, ALPHA = 4, 64, 48, 8, 16


def mats(seed=0, shared_a=False):
    rng = np.random.default_rng(seed)
    a0 = rng.standard_normal((R, D_IN)).astype(np.float32)
    As = [a0 if shared_a else rng.standard_normal((R, D_IN)).astype(np.float32) for _ in range(K)]
    Bs = [rng.standard_normal((D_OUT, R)).astype(np.float32) for _ in range(K)]
    return As, Bs


def test_naive_average_is_wrong():
    As, Bs = mats()
    truth = np.mean([b @ a for a, b in zip(As, Bs)], axis=0)
    err = np.linalg.norm(naive_merge(As, Bs) - truth)
    assert err > 1.0          # large, not a rounding effect


def test_lora_fa_is_exact():
    As, Bs = mats(shared_a=True)
    a = As[0]
    truth = np.mean([b @ a for b in Bs], axis=0)
    err = np.linalg.norm(lora_fa_merge(Bs) @ a - truth)
    assert err < 1e-4         # float32 round-off; relative error below
    assert err / np.linalg.norm(truth) < 1e-6


def test_exact_delta_w_matches_definition():
    As, Bs = mats()
    got = exact_delta_w(As, Bs, ALPHA, R)
    want = (ALPHA / R) * sum(b @ a for a, b in zip(As, Bs)) / K
    assert np.allclose(got, want, atol=1e-4)


def test_svd_truncation_error_equals_discarded_singular_values():
    As, Bs = mats()
    dw = exact_delta_w(As, Bs, ALPHA, R)             # rank up to K*R = 32
    _, _, err = svd_refactor(dw, R)
    s = np.linalg.svd(dw, compute_uv=False)
    assert abs(err - np.sqrt(np.sum(s[R:] ** 2))) < 1e-3 * err
    assert err > 0            # SVD to rank r is approximate, not exact


def test_svd_full_rank_is_exact():
    As, Bs = mats()
    dw = exact_delta_w(As, Bs, ALPHA, R)
    _, _, err = svd_refactor(dw, K * R)
    assert err / np.linalg.norm(dw) < 1e-5
