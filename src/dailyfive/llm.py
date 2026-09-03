"""Brain-agnostic LLM layer.

Every reasoning role in this studio names a *role*, not a vendor. Which brain
answers is configuration, so the whole roster can move to MiniMax, to a local
model, or to a mix, without touching an agent.

Three providers ship:

``anthropic``           the Anthropic SDK.
``minimax``             MiniMax's OpenAI-compatible surface.
``openai-compatible``   anything else speaking that dialect — OpenRouter,
                        Together, vLLM, Ollama — via ``LLM_BASE_URL``.

The differences that actually matter between brains are handled here rather
than in the agents:

- **Native JSON mode.** OpenAI-compatible surfaces accept
  ``response_format={"type":"json_object"}``, which materially improves
  structured-output reliability on smaller models. Anthropic has no equivalent,
  so there the schema is carried in the prompt. Same call site either way.
- **Reasoning tokens.** MiniMax's M-series reasons by default and can wrap the
  answer in ``<think>`` blocks. Disabled explicitly, and stripped defensively.
- **Failure vocabulary.** MiniMax returns HTTP 200 with an error envelope;
  Anthropic raises. Both become a ``ProviderError`` with an honest
  ``retryable`` flag.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .config import settings
from .errors import ConfigError, ProviderError

log = logging.getLogger(__name__)

# The eight roles that need a brain. Anything else is plain code, on purpose.
ROLES = ("scout", "director", "anr", "lyricist", "clearance", "producer", "retro",
         "videodirector")

# Sensible per-role defaults when nothing is configured. Lyricist leans MiniMax
# because that was the original intent and it is cheap for high-volume creative
# writing; the rest inherit whatever the global default is.
ROLE_HINTS = {
    "scout": "synthesis over messy evidence — rewards a strong reasoner",
    "director": "long context over the codex — rewards a strong reasoner",
    "anr": "set-level reasoning about variety",
    "lyricist": "high-volume creative writing — cheapest capable model wins",
    "clearance": "careful rule-following, low temperature",
    "producer": "comparative judgement across many candidates",
    "retro": "weekly, low volume, reads aggregates",
    "videodirector": "short physical shot lists — a small model is enough",
}


@dataclass(frozen=True)
class Brain:
    """One resolved brain: who answers, as what model, at which endpoint."""
    provider: str
    model: str
    base_url: str | None = None

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


class _Backend(Protocol):
    def complete(self, brain: Brain, system: str, user: str, *,
                 max_tokens: int, temperature: float, json_mode: bool) -> str: ...


# ── provider implementations ─────────────────────────────────────────────────
class AnthropicBackend:
    """Claude, and the three ways it is not an OpenAI-dialect endpoint.

    TEMPERATURE IS NOT SENT. Every agent in this studio passes one — 1.05 for
    the Lyricist's second draft, 0.2 for a JSON repair turn — and current Claude
    models REMOVED the sampling parameters: temperature, top_p and top_k all
    return a 400. Forwarding the caller's value would not degrade the switch, it
    would break every single call in the pipeline. The roles' intent survives
    as `effort`, which is the knob that replaced it.

    A FLOOR ON max_tokens. Thinking is on by default and its tokens come out of
    the same budget as the answer, so a caller's ceiling sized for a
    non-thinking model can be spent entirely on reasoning and return an empty
    string. The Lyricist's forced choice asks for one character with
    max_tokens=8; under adaptive thinking that returns nothing at all. The floor
    is applied here rather than by editing eight call sites, because the number
    is a property of the backend and not of any agent.

    THE WORKSPACE HEADER. An identity-linked key rejects every request without
    `anthropic-workspace-id` — including GET /v1/models, so a key cannot be used
    to discover its own workspace. Sent as a default header when configured.
    """
    name = "anthropic"
    _client = None

    # Enough for adaptive thinking to finish and still leave an answer. The
    # smallest real call in this studio wants eight tokens of output; the
    # thinking in front of it does not fit in eight tokens.
    MIN_TOKENS = 4096

    @classmethod
    def reset(cls) -> None:
        """Drop the cached client so a config change takes effect."""
        cls._client = None

    def _get(self):
        if AnthropicBackend._client is None:
            import anthropic
            cfg = settings()
            key = cfg.anthropic_api_key
            if not key:
                raise ProviderError("anthropic", "ANTHROPIC_API_KEY is not set",
                                    retryable=False)
            headers = {}
            if cfg.anthropic_workspace_id:
                headers["anthropic-workspace-id"] = cfg.anthropic_workspace_id
            AnthropicBackend._client = anthropic.Anthropic(
                api_key=key, default_headers=headers or None)
        return AnthropicBackend._client

    def complete(self, brain, system, user, *, max_tokens, temperature,
                 json_mode) -> str:
        # No native JSON mode here; ask_json carries the schema in the prompt.
        # temperature is accepted and deliberately dropped — see the class note.
        body = {
            "model": brain.model,
            "max_tokens": max(int(max_tokens), self.MIN_TOKENS),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        effort = settings().anthropic_effort
        if effort:
            body["output_config"] = {"effort": effort}
        try:
            resp = self._get().messages.create(**body)
        except Exception as exc:
            raise ProviderError("anthropic", f"messages.create failed: {exc}",
                                retryable=_looks_transient(exc)) from exc
        # A refusal is a 200 with no text, and returning "" from here would
        # surface three layers up as an unparseable JSON body rather than as
        # what it is.
        if getattr(resp, "stop_reason", None) == "refusal":
            detail = getattr(getattr(resp, "stop_details", None), "category", None)
            raise ProviderError("anthropic",
                                f"the model declined this request ({detail or 'no category'})",
                                retryable=False)
        return "\n".join(b.text for b in resp.content
                         if getattr(b, "type", None) == "text").strip()


class OpenAICompatBackend:
    """MiniMax and every other OpenAI-dialect endpoint.

    One implementation covers them because the dialect is the contract; only
    the base URL, key and error envelope differ.
    """
    name = "openai-compatible"

    def __init__(self, key_getter, default_base: str, provider_label: str):
        self._key_getter = key_getter
        self._default_base = default_base
        self.label = provider_label

    def complete(self, brain, system, user, *, max_tokens, temperature,
                 json_mode) -> str:
        from .http import request

        key = self._key_getter()
        if not key:
            raise ProviderError(self.label,
                                f"no API key configured for provider {self.label!r}",
                                retryable=False)
        base = (brain.base_url or self._default_base).rstrip("/")

        body: dict = {
            "model": brain.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            # Reasoning tokens on a lyric write or a score are pure cost, and
            # the <think> wrapper would have to be stripped anyway.
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            # The single biggest reliability win for structured output on
            # smaller models — worth more than any amount of prompt scolding.
            body["response_format"] = {"type": "json_object"}

        resp = request("POST", f"{base}/v1/chat/completions", provider=self.label,
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json"},
                       json=body, timeout=240.0)

        if resp.status_code >= 400:
            raise ProviderError(self.label,
                                f"chat/completions HTTP {resp.status_code}: "
                                f"{resp.text[:300]}",
                                retryable=resp.status_code >= 500,
                                status=resp.status_code)
        data = resp.json()

        # MiniMax answers 200 on failure and puts the truth in base_resp.
        br = data.get("base_resp") or {}
        code = br.get("status_code", 0)
        if code not in (0, None):
            from .providers.minimax import BASE_RESP_CODES
            msg, retryable = BASE_RESP_CODES.get(
                code, (br.get("status_msg", "unknown"), False))
            raise ProviderError(self.label, f"{msg} (status_code {code})",
                                retryable=retryable)

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(self.label, f"no choices returned: {str(data)[:300]}",
                                retryable=True)
        content = (choices[0].get("message") or {}).get("content") or ""
        return _strip_think(content).strip()


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_think(text: str) -> str:
    return _THINK.sub("", text)


def _looks_transient(exc: Exception) -> bool:
    return any(t in str(exc).lower() for t in
               ("overload", "rate", "timeout", "503", "529", "500", "502"))


def _backends() -> dict[str, _Backend]:
    cfg = settings()
    return {
        "anthropic": AnthropicBackend(),
        "minimax": OpenAICompatBackend(
            lambda: settings().minimax_api_key, cfg.minimax_base_url, "minimax"),
        "openai-compatible": OpenAICompatBackend(
            lambda: settings().llm_api_key, cfg.llm_base_url, "openai-compatible"),
    }


DEFAULT_MODELS = {
    "anthropic": lambda: settings().anthropic_model,
    "minimax": lambda: settings().minimax_text_model,
    "openai-compatible": lambda: settings().llm_model,
}


# ── resolution ───────────────────────────────────────────────────────────────
def resolve(role: str) -> Brain:
    """Which brain answers for this role.

    Precedence: the role's own override, then the global default. A spec is
    ``provider`` or ``provider:model``, so ``LLM_LYRICIST=minimax`` is enough to
    move one role without naming a model.
    """
    if role not in ROLES:
        raise ConfigError(f"unknown role {role!r} — known: {', '.join(ROLES)}")
    cfg = settings()
    spec = cfg.role_brain(role) or cfg.llm_default or "anthropic"
    return parse_spec(spec)


def parse_spec(spec: str) -> Brain:
    spec = spec.strip()
    provider, _, model = spec.partition(":")
    provider = provider.strip().lower() or "anthropic"
    if provider in ("openai", "compatible", "openai_compatible"):
        provider = "openai-compatible"
    if provider not in DEFAULT_MODELS:
        raise ConfigError(
            f"unknown LLM provider {provider!r} — known: "
            f"{', '.join(sorted(DEFAULT_MODELS))}")
    model = model.strip() or DEFAULT_MODELS[provider]()
    if not model:
        raise ConfigError(f"no model configured for provider {provider!r}")
    base = settings().llm_base_url if provider == "openai-compatible" else None
    return Brain(provider=provider, model=model, base_url=base)


def roster() -> dict[str, Brain]:
    """Every role and the brain currently behind it. Used by the console."""
    out: dict[str, Brain] = {}
    for role in ROLES:
        try:
            out[role] = resolve(role)
        except ConfigError as exc:
            out[role] = Brain(provider="unconfigured", model=str(exc))
    return out


# ── the call ─────────────────────────────────────────────────────────────────
def complete(role: str, system: str, user: str, *, max_tokens: int = 4000,
             temperature: float = 1.0, json_mode: bool = False,
             brain: Brain | None = None) -> tuple[str, Brain]:
    """One completion for a role. Returns the text and which brain produced it.

    Returning the brain is not incidental — every agent records it on the run
    so the console can show which model actually wrote each thing, and so a
    change in output quality can be traced to a change in brain.
    """
    brain = brain or resolve(role)
    backend = _backends().get(brain.provider)
    if backend is None:
        raise ConfigError(f"no backend for provider {brain.provider!r}")

    supports_json = brain.provider != "anthropic"
    text = backend.complete(brain, system, user, max_tokens=max_tokens,
                            temperature=temperature,
                            json_mode=json_mode and supports_json)
    return text, brain


def probe(role: str) -> tuple[bool, str]:
    """Cheapest possible liveness check for one role's brain.

    Costs a handful of tokens rather than being free, which is why it is a
    command you run rather than something the pipeline does on every start.
    """
    try:
        brain = resolve(role)
    except ConfigError as exc:
        return False, str(exc)
    try:
        text, _ = complete(role, "Reply with the single word: ok",
                           "ok", max_tokens=8, temperature=0.0)
    except (ProviderError, ConfigError) as exc:
        return False, str(exc)
    return True, f"{brain} → {text.strip()[:40]!r}"
