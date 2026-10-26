"""contracts/openapi/aicf_v1.yaml (the /api/studio/* part) against the running Studio service, in both directions:
every route the service mounts is in the spec and every Studio path in the spec is served; sample bodies validate."""
import re
from pathlib import Path

import jsonschema
import yaml

from tests.studio.test_studio_service import CFG, JSONL, call, needs_pg, svc, tenant  # noqa: F401  (fixtures)

SPEC = yaml.safe_load((Path(__file__).resolve().parents[2] / "contracts/openapi/aicf_v1.yaml").read_text())


def validate(name, instance):
    jsonschema.validate(instance, {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]})


def spec_routes():
    out = set()
    for path, ops in SPEC["paths"].items():
        if path.startswith("/api/studio/"):
            out |= {(m.upper(), path[len("/api/studio"):]) for m in ops if m in ("get", "post", "put", "delete")}
    return out


@needs_pg
def test_routes_and_spec_agree_in_both_directions(svc):  # noqa: F811
    served = {(m, re.sub(r"\{(\w+)\}", lambda g: "{" + g.group(1) + "}", t)) for m, _, _, t in svc.app.router.routes}
    norm = lambda r: {(m, re.sub(r"\{\w+\}", "{}", p)) for m, p in r}  # the spec may name a parameter differently
    assert norm(served) == norm(spec_routes()), (sorted(norm(served) - norm(spec_routes())), sorted(norm(spec_routes()) - norm(served)))


@needs_pg
def test_bodies_validate_against_the_schemas(svc, tenant):  # noqa: F811
    validate("StudioMethods", call(svc, "GET", "/v1/methods", role="viewer")[1])
    validate("StudioHealth", call(svc, "GET", "/v1/health", role="viewer")[1])
    st, job = call(svc, "POST", f"/v1/tenants/{tenant}/lora/jobs", {"base_model": "tiny", "format": "jsonl", "content": JSONL, "steps": 10}, role="operator")
    assert st == 202
    validate("StudioJob", job)
    validate("StudioJob", call(svc, "GET", f"/v1/tenants/{tenant}/lora/jobs/{job['id']}", role="viewer")[1])
    st, err = call(svc, "POST", f"/v1/tenants/{tenant}/lora/jobs", {"base_model": "nope", "content": JSONL}, role="operator")
    assert st == 400
    validate("StudioError", err)
    assert CFG
