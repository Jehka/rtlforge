"""Model-independent LLM interface.

Groq, Google AI Studio and Ollama all expose OpenAI-compatible chat endpoints,
so one client covers the free-API path and the local-inference path. Swapping
provider is a config change, which is what makes the local-vs-cloud ablation
cheap to run later.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

# base_url, default model, env var holding the key
PROVIDERS = {
    "groq": (
        "https://api.groq.com/openai/v1",
        "openai/gpt-oss-120b",
        "GROQ_API_KEY",
    ),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "gemini-2.5-flash",
        "GEMINI_API_KEY",
    ),
    "ollama": (
        "http://localhost:11434/v1",
        "qwen2.5-coder:7b",
        None,  # no key needed
    ),
}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        provider: str = "groq",
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        max_retries: int = 4,
        max_tokens: int = 8192,
    ):
        if provider not in PROVIDERS:
            raise LLMError(
                f"unknown provider {provider!r}; "
                f"choose from {sorted(PROVIDERS)}"
            )
        default_url, default_model, key_env = PROVIDERS[provider]

        self.provider = provider
        self.base_url = (base_url or default_url).rstrip("/")
        self.model = model or default_model
        self.temperature = temperature
        self.max_retries = max_retries
        # Reasoning models spend budget before emitting code, so this must be
        # well above the size of the expected module. But it is also reserved
        # against tokens-per-minute at request time, so oversizing it can
        # cause 429s on an otherwise idle account. 8192 is the compromise.
        self.max_tokens = max_tokens
        self.usage = Usage()
        # Set after every call. "length" means the response was cut off, which
        # produces a truncated module that looks like a syntax error. That
        # misattribution is deadly to the experiment, so it is surfaced
        # explicitly rather than left to the linter to misdiagnose.
        self.last_finish_reason: Optional[str] = None

        self.api_key = os.environ.get(key_env) if key_env else None
        if key_env and not self.api_key:
            raise LLMError(
                f"{key_env} is not set. Copy .env.example to .env and fill it in."
            )

    def chat(self, messages: List[dict],
             max_tokens: Optional[int] = None) -> str:
        max_tokens = max_tokens or self.max_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }

        delay = 2.0
        last_err = ""
        for attempt in range(self.max_retries):
            try:
                r = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=180,
                )
            except requests.RequestException as e:
                last_err = str(e)
                time.sleep(delay)
                delay *= 2
                continue

            if r.status_code == 429:
                # The body says WHICH limit was hit. Requests-per-day and
                # tokens-per-minute need opposite responses -- one means stop
                # for today, the other means wait a minute -- so never discard
                # this text.
                body = r.text[:300]
                limits = {
                    k: v for k, v in r.headers.items()
                    if k.lower().startswith("x-ratelimit")
                }
                low = body.lower()

                # A per-day exhaustion will not clear by retrying. Fail fast
                # rather than burning four backoffs to reach the same place.
                if "per day" in low or "rpd" in low or "tpd" in low:
                    raise LLMError(
                        "daily quota exhausted -- retrying will not help.\n"
                        f"  {body}\n"
                        f"  limits: {limits}"
                    )

                wait = float(r.headers.get("retry-after", delay))
                time.sleep(min(wait, 120))
                delay = min(delay * 2, 120)
                last_err = f"rate limited (429): {body} | limits: {limits}"
                continue

            if r.status_code >= 500:
                time.sleep(delay)
                delay *= 2
                last_err = f"server error {r.status_code}"
                continue

            if r.status_code != 200:
                raise LLMError(f"{r.status_code}: {r.text[:400]}")

            data = r.json()
            choice = data["choices"][0]
            self.last_finish_reason = choice.get("finish_reason")
            u = data.get("usage") or {}
            self.usage = self.usage + Usage(
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
            )
            return choice["message"].get("content") or ""

        raise LLMError(
            f"failed after {self.max_retries} attempts: {last_err}\n"
            "  If this mentions tokens-per-minute, lower --max-tokens: the "
            "ceiling is reserved against your TPM allowance at request time, "
            "so an oversized budget can 429 even on an idle account."
        )


# ------------------------------------------------------------------ helpers

_FENCE_RE = re.compile(
    r"```(?:systemverilog|verilog|sv|v)?\s*\n(?P<body>.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_verilog(text: str) -> str:
    """Pull RTL out of a model response.

    Takes the longest fenced block -- models often emit a short illustrative
    snippet before the real module. Falls back to raw text if unfenced.
    """
    blocks = [m.group("body") for m in _FENCE_RE.finditer(text)]
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"
