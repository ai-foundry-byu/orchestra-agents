"""orchestra.client — a thin, testable wrapper over the Anthropic SDK.

BYOK: the real client reads ``ANTHROPIC_API_KEY`` from the environment. Nothing
is hardcoded and no key ever touches disk.

The whole library is written against the small :class:`AgentClient` protocol
below rather than the SDK directly, so:

  * unit tests inject :class:`FakeClient` and run fully offline / free, and
  * you can swap in a different backend (Bedrock, Vertex, a local proxy)
    without touching the orchestration code.

Costs are reported per call in :class:`Completion` so the fleet, best-of-n, and
eval layers can roll a real dollar figure up to the human.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

# Public list-price per million tokens (USD), input/output. Kept small and
# explicit so cost roll-ups are auditable. Update as Anthropic pricing changes.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (0.80, 4.0),
}
_DEFAULT_PRICE = (3.0, 15.0)


def _price_for(model: str) -> tuple[float, float]:
    for prefix, price in _PRICE_PER_MTOK.items():
        if model.startswith(prefix):
            return price
    return _DEFAULT_PRICE


@dataclass
class Completion:
    """The result of a single model call."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        in_price, out_price = _price_for(self.model)
        return (self.input_tokens / 1e6) * in_price + (
            self.output_tokens / 1e6
        ) * out_price


class AgentClient(Protocol):
    """The one method the whole library depends on."""

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Completion: ...


class ClaudeClient:
    """Real Anthropic-backed client. Imports the SDK lazily so the rest of the
    package (and its tests) work without the dependency installed."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str = "claude-sonnet-4-20250514",
    ) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. This is a bring-your-own-key library; "
                "export your key or pass api_key=... explicitly."
            )
        self.default_model = default_model
        self._sdk = None  # lazy

    def _client(self):
        if self._sdk is None:
            try:
                import anthropic  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "The 'anthropic' package is required for ClaudeClient. "
                    "Install it with: pip install anthropic"
                ) from exc
            self._sdk = anthropic.Anthropic(api_key=self.api_key)
        return self._sdk

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Completion:
        model = model or self.default_model
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        resp = self._client().messages.create(**kwargs)
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        usage = getattr(resp, "usage", None)
        return Completion(
            text=text,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )


@dataclass
class FakeClient:
    """Deterministic offline client for tests and demos.

    Supply a ``responder`` that maps a prompt to a response string, or rely on
    the default echo. Records every call for assertions.
    """

    responder: Callable[[str], str] | None = None
    model: str = "fake-claude"
    calls: list[dict] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Completion:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "model": model or self.model,
                "temperature": temperature,
            }
        )
        text = self.responder(prompt) if self.responder else f"echo: {prompt[:80]}"
        return Completion(
            text=text,
            model=model or self.model,
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(text) // 4),
        )
