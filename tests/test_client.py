"""Tests for orchestra.client."""

import pytest

from orchestra.client import ClaudeClient, Completion, FakeClient


def test_completion_cost_opus():
    c = Completion(text="hi", model="claude-opus-4-20250514", input_tokens=1_000_000, output_tokens=1_000_000)
    # opus: 15 in + 75 out per Mtok
    assert c.cost_usd == pytest.approx(90.0)


def test_completion_cost_sonnet_default():
    c = Completion(text="hi", model="unknown-model", input_tokens=1_000_000, output_tokens=0)
    assert c.cost_usd == pytest.approx(3.0)  # default price


def test_fake_client_echo_and_records():
    fc = FakeClient()
    out = fc.complete("hello world", temperature=0.3)
    assert out.text.startswith("echo:")
    assert out.input_tokens > 0
    assert fc.calls[0]["temperature"] == 0.3


def test_fake_client_custom_responder():
    fc = FakeClient(responder=lambda p: p.upper())
    assert fc.complete("abc").text == "ABC"


def test_claude_client_requires_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeClient()


def test_claude_client_accepts_explicit_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = ClaudeClient(api_key="test-key-not-real")
    assert c.api_key == "test-key-not-real"
