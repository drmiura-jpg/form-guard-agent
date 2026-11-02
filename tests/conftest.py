"""Shared fixtures: samples are generated into a temp dir so tests never depend on committed files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from form_guard.pipeline import run_pipeline  # noqa: E402
from form_guard.samples import DEMOS, demo_paths, generate_samples, read_memo  # noqa: E402


@pytest.fixture(autouse=True)
def _hermetic_model_settings(monkeypatch):
    """Model routing must not depend on the developer's shell (FORM_GUARD_MODEL etc.)."""
    for var in ("FORM_GUARD_MODEL", "FORM_GUARD_MODEL_TIER", "FORM_GUARD_EFFORT", "FORM_GUARD_FALLBACKS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def samples_dir(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("samples")
    generate_samples(base, with_comparisons=False)
    return base


@pytest.fixture(scope="session")
def demo_runs(samples_dir, tmp_path_factory):
    """Lazily run (and cache) the offline pipeline for each demo id."""
    cache: dict = {}
    out = tmp_path_factory.mktemp("runs")

    def get(demo_id: str):
        if demo_id not in cache:
            d = next(x for x in DEMOS if x["id"] == demo_id)
            p = demo_paths(d, samples_dir)
            cache[demo_id] = run_pipeline(p["form"], read_memo(p["memo"]), out / demo_id, mode="demo",
                                          naive_path=p["naive"], demo_id=demo_id)
        return cache[demo_id]

    return get


# ------------------------------------------------------------- fake Claude


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        response = self.responses.pop(0)
        return FakeStream(response(kwargs) if callable(response) else response)


class FakeClient:
    """Stands in for anthropic.Anthropic(): client.beta.messages.stream(**kw) -> get_final_message().

    A response may be a message or a callable ``kwargs -> message`` (e.g. to echo the requested model)."""

    def __init__(self, responses):
        self.beta = SimpleNamespace(messages=FakeMessages(responses))


def fake_message(payload: dict, stop_reason: str = "end_turn", category=None, model: str = "claude-opus-5-5",
                 thinking: str = ""):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=thinking, signature="sig"),
                 SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category=category) if category else None,
        usage=SimpleNamespace(input_tokens=1200, output_tokens=300, cache_read_input_tokens=0),
        model=model,
    )


def echo_model(payload: dict, thinking: str = ""):
    """A fake response that reports the model the request asked for (as the API does)."""
    return lambda kwargs: fake_message(payload, model=kwargs["model"], thinking=thinking)


@pytest.fixture
def fake_client_factory():
    return lambda responses: FakeClient(responses)
