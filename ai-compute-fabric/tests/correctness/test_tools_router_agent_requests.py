"""The chat tools router (deploy/demo/aicf_tools.py) and Agent requests (those carrying a function-calling `tools` list).
Pure planning logic: no network, no gateway. The admin switch `agent_requests` is off by default."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "demo"))
import aicf_tools as T  # noqa: E402
import aicf_tools_config as C  # noqa: E402

WEATHER = "Thời tiết Hà Nội hôm nay thế nào?"
TOOLS = [{"type": "function", "function": {"name": "run_cad", "parameters": {"type": "object", "properties": {}}}}]


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """a throw-away settings file; helper to write settings"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(C, "PATH", str(path))
    monkeypatch.setattr(C, "_cache", None)

    def write(**kw):
        path.write_text(json.dumps(kw))
        monkeypatch.setattr(C, "_cache", None)
    return write


def names(body):
    """tools the router would run, without the helper `datetime` (it is added automatically to any date-related question)"""
    return [t.name for t, _ in T.plan(body)[0] if t.name != "datetime"]


def req(text=WEATHER, tools=False, last="user"):
    msgs = [{"role": "system", "content": "You are a CAD agent."}, {"role": "user", "content": text}]
    if last != "user":
        msgs.append({"role": last, "content": "x"})
    body = {"model": "m", "messages": msgs}
    if tools:
        body["tools"] = TOOLS
    return body


def test_plain_chat_still_gets_weather(cfg):
    cfg()
    assert names(req()) == ["weather"]


def test_default_is_off_for_agent_requests(cfg):
    cfg()
    assert C.load()["agent_requests"] is False
    assert names(req(tools=True)) == []


def test_switch_on_enables_weather_for_agent_requests(cfg):
    cfg(agent_requests=True)
    assert names(req(tools=True)) == ["weather"]


def test_functions_field_counts_as_agent_traffic_too(cfg):
    body = req()
    body["functions"] = [{"name": "f"}]
    cfg()
    assert names(body) == []
    cfg(agent_requests=True)
    assert names(body) == ["weather"]


@pytest.mark.parametrize("last", ["tool", "assistant"])
def test_never_inside_an_agent_tool_loop(cfg, last):
    cfg(agent_requests=True)
    assert names(req(tools=True, last=last)) == []


def test_unrelated_message_is_left_alone(cfg):
    cfg(agent_requests=True)
    assert names(req("Vẽ cho tôi một giá đỡ chữ L dày 4 mm", tools=True)) == []


def test_master_switch_off_wins(cfg):
    cfg(enabled=False, agent_requests=True)
    assert names(req(tools=True)) == []


def test_weather_tool_switch_is_still_respected(cfg):
    cfg(agent_requests=True, tools={"weather": False})
    assert names(req(tools=True)) == []


def test_non_boolean_values_are_ignored(cfg):
    cfg(agent_requests="yes")
    assert C.load()["agent_requests"] is False


def test_save_round_trip(cfg):
    saved = C.save({"agent_requests": True}, "tester")
    assert saved["agent_requests"] is True and C.load()["agent_requests"] is True


def test_process_passes_agent_request_through_when_off(cfg):
    cfg()
    raw = json.dumps(req(tools=True)).encode()
    out, hdr, footer = T.process(raw, "")
    assert out == raw and hdr is None and footer is None
