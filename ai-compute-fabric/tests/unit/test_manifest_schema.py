"""P0.2 gate: upstream-manifest schema + license gate."""
import re
from pathlib import Path

import pytest
import yaml

MANIFEST = Path(__file__).resolve().parents[2] / "third_party" / "upstream-manifest.yaml"
STRATEGIES = {"PORT", "VENDOR", "DEPEND", "RESEARCH_ONLY"}
ALLOWED_PROD = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause"}
DENY = ("AGPL", "GPL", "SSPL")
REQUIRED = {"name", "repo_url", "commit_sha", "spdx_license", "integration_strategy", "tier"}


@pytest.fixture(scope="module")
def comps():
    return yaml.safe_load(MANIFEST.read_text())["components"]


def test_required_fields_and_sha_format(comps):
    for c in comps:
        assert REQUIRED <= c.keys(), f"{c.get('name')} missing {REQUIRED - c.keys()}"
        assert re.fullmatch(r"[0-9a-f]{40}", c["commit_sha"]), c["name"]
        assert c["integration_strategy"] in STRATEGIES, c["name"]
        assert c["tier"] in {"production", "research"}, c["name"]


def test_names_unique(comps):
    names = [c["name"] for c in comps]
    assert len(names) == len(set(names))


def test_production_licenses_are_allowed(comps):
    for c in comps:
        if c["tier"] == "production":
            assert c["spdx_license"] in ALLOWED_PROD, c["name"]
            assert not any(d in c["spdx_license"] for d in DENY), c["name"]


def test_unverified_license_never_in_production(comps):
    for c in comps:
        if c["spdx_license"] == "UNVERIFIED":
            assert c["tier"] == "research", c["name"]


def test_research_only_strategy_not_production(comps):
    for c in comps:
        if c["integration_strategy"] == "RESEARCH_ONLY":
            assert c["tier"] == "research", c["name"]


def test_denylist_catches_agpl():
    """The gate itself must reject a forbidden license (guards the guard)."""
    bad = {"spdx_license": "AGPL-3.0", "tier": "production"}
    assert not (bad["spdx_license"] in ALLOWED_PROD)


@pytest.mark.xfail(reason="P0 gate: needs Legal sign-off; flips to pass when legal_reviewed=true", strict=True)
def test_legal_signoff_for_production(comps):
    assert all(c.get("legal_reviewed") for c in comps if c["tier"] == "production")
