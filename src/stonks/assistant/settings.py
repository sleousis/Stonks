"""``[assistant]`` settings: the in-app AI assistant (roadmap 20.4).

The assistant is off until ``base_url`` names an OpenAI-compatible chat
completions endpoint (Ollama, vLLM, a llama.cpp server). The API key is
optional and read from ``STONKS_ASSISTANT_API_KEY`` only, never from TOML.
"""

from __future__ import annotations

import os
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator

API_KEY_ENV = "STONKS_ASSISTANT_API_KEY"


class AssistantEnvelope(BaseModel):
    """``[assistant.envelope]``: what the assistant may do with orders.

    The assistant never places an order. With ``order_tools`` on it may
    create order drafts inside these limits, which a person approves in the
    web app with a fresh second factor. Off (the default) is research only:
    no order tool is offered at all."""

    model_config = ConfigDict(extra="forbid")

    order_tools: bool = False
    #: Tickers a draft may name; None allows any known instrument.
    allowed_tickers: list[str] | None = None
    max_order_notional: float | None = Field(default=5_000.0, gt=0)
    max_day_notional: float | None = Field(default=20_000.0, gt=0)
    #: How far a limit price may sit from the latest close, as a fraction.
    price_band: float = Field(default=0.05, gt=0, le=0.5)
    #: How long a draft stays approvable.
    draft_ttl_minutes: float = Field(default=24 * 60, gt=0)
    #: Writes (drafts, research jobs) per person: a burst above either rate
    #: freezes the assistant for ``freeze_minutes``.
    max_writes_per_minute: int = Field(default=5, ge=1)
    max_writes_per_hour: int = Field(default=40, ge=1)
    freeze_minutes: float = Field(default=60.0, gt=0)


#: Survival tests the research loop may run. Each judges only the validation
#: window or the run's own trials, so the model's training cutoff check on
#: the validation window covers all of its evidence. Walk-forward, CPCV and
#: the permutation tests score folds across the whole window, which may lie
#: before the cutoff, so they are left out.
CUTOFF_SAFE_TESTS: frozenset[str] = frozenset(
    {
        "oos",
        "deflated_sharpe",
        "pbo",
        "period_stability",
        "perturbation",
        "runs_test",
        "forecast_skill",
    }
)


class AssistantResearch(BaseModel):
    """``[assistant.research]``: the AI research loop (roadmap 22.9).

    The assistant proposes hypotheses and runs lab trials under these
    budgets. It never registers or promotes a strategy. Models remember
    prices from before their training cutoff, so a proposal counts as
    evidence only when its validation window starts after ``model_cutoff``.
    Without a cutoff the loop does not run: it fails closed."""

    model_config = ConfigDict(extra="forbid")

    #: The configured model's training cutoff. None turns the loop off.
    model_cutoff: date | None = None
    #: Tuning trials one session may spend, summed over its proposals.
    max_trials: int = Field(default=200, ge=1, le=10_000)
    #: Proposals one session may make, rejected ones included.
    max_proposals: int = Field(default=10, ge=1, le=100)
    #: Compute one session may spend: the wall time of its lab runs times
    #: the lab's worker count, an upper bound on their CPU time.
    max_cpu_seconds: float = Field(default=3600.0, gt=0, le=7 * 24 * 3600)
    #: Tuning trials one proposal may ask for.
    max_budget_per_proposal: int = Field(default=50, ge=1, le=1_000)
    #: The survival suite of every research lab run (see CUTOFF_SAFE_TESTS).
    survival_tests: list[str] = Field(
        default_factory=lambda: ["oos", "deflated_sharpe", "pbo"], min_length=1
    )
    #: Shortest hypothesis and premortem a proposal may state (P1).
    min_hypothesis_chars: int = Field(default=40, ge=1, le=2_000)
    min_premortem_chars: int = Field(default=20, ge=1, le=2_000)

    @field_validator("survival_tests")
    @classmethod
    def _cutoff_safe(cls, tests: list[str]) -> list[str]:
        unsafe = sorted(set(tests) - CUTOFF_SAFE_TESTS)
        if unsafe:
            raise ValueError(
                f"{unsafe} score data before the validation window, which may lie before "
                f"the model's cutoff; choose from {sorted(CUTOFF_SAFE_TESTS)}"
            )
        return tests

    @property
    def enabled(self) -> bool:
        return self.model_cutoff is not None


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
    #: Order drafts, rate limits and the research-only switch.
    envelope: AssistantEnvelope = Field(default_factory=AssistantEnvelope)
    #: The research loop: the model's cutoff and the budgets.
    research: AssistantResearch = Field(default_factory=AssistantResearch)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.base_url.strip())

    @staticmethod
    def api_key() -> str | None:
        """The endpoint's key from the environment, or None."""
        value = os.environ.get(API_KEY_ENV, "").strip()
        return value or None
