"""``[assistant]`` settings: the in-app AI assistant (roadmap 20.4).

The assistant is off until ``base_url`` names an OpenAI-compatible chat
completions endpoint (Ollama, vLLM, a llama.cpp server). The API key is
optional and read from ``STONKS_ASSISTANT_API_KEY`` only, never from TOML.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

API_KEY_ENV = "STONKS_ASSISTANT_API_KEY"


class AssistantConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Base URL of the chat completions API, e.g. ``http://127.0.0.1:11434/v1``
    #: for Ollama. None turns the assistant off.
    base_url: str | None = None
    #: The model name the endpoint serves.
    model: str = "llama3.1"
    #: Model calls per turn (each tool round is one step).
    max_steps: int = Field(default=8, ge=1, le=50)
    #: Tokens the model may write per call.
    max_tokens: int = Field(default=1024, ge=16, le=32768)
    #: Wall-clock limit for one turn, tool calls included.
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    #: Most recent messages of a conversation sent to the model.
    max_conversation_messages: int = Field(default=40, ge=2, le=500)
    #: Characters of one tool result fed back to the model.
    max_tool_result_chars: int = Field(default=8000, ge=200, le=200_000)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.base_url.strip())

    @staticmethod
    def api_key() -> str | None:
        """The endpoint's key from the environment, or None."""
        value = os.environ.get(API_KEY_ENV, "").strip()
        return value or None
