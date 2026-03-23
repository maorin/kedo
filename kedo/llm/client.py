"""LLM client that wraps the OpenAI API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LLMMessage:
    """A single message in an LLM conversation."""

    role: str  # "system", "user", or "assistant"
    content: str


@dataclass
class LLMResponse:
    """A response from the LLM."""

    content: str
    model: str
    usage: dict = field(default_factory=dict)


class LLMClient:
    """Thin wrapper around the OpenAI chat completion API.

    The client reads the API key from the ``OPENAI_API_KEY`` environment
    variable (or from the *api_key* constructor argument).  It exposes a
    single :meth:`chat` method that accepts a list of :class:`LLMMessage`
    objects and returns an :class:`LLMResponse`.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")

    def chat(
        self,
        messages: List[LLMMessage],
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send *messages* to the LLM and return the reply.

        Raises
        ------
        ImportError
            If the ``openai`` package is not installed.
        RuntimeError
            If ``OPENAI_API_KEY`` is not configured.
        """
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "The 'openai' package is required. Install it with: pip install openai"
            ) from exc

        if not self._api_key:
            raise RuntimeError(
                "OpenAI API key is not set. "
                "Provide it via the OPENAI_API_KEY environment variable or the "
                "api_key constructor argument."
            )

        client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        payload = [{"role": m.role, "content": m.content} for m in messages]
        response = client.chat.completions.create(
            model=self.model,
            messages=payload,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            model=response.model,
            usage=dict(response.usage) if response.usage else {},
        )
