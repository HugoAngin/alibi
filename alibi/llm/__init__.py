"""Gemini access for Alibi: one real client, one fake, shared config and schema checks."""

from alibi.llm.client import (
    BudgetExceeded,
    CallBudget,
    GeminiClient,
    LLMError,
    MissingApiKey,
    Result,
    SchemaValidationError,
    Unavailable,
    Usage,
    process_budget,
)
from alibi.llm.fake import FakeClient

__all__ = [
    "BudgetExceeded",
    "CallBudget",
    "FakeClient",
    "GeminiClient",
    "LLMError",
    "MissingApiKey",
    "Result",
    "SchemaValidationError",
    "Unavailable",
    "Usage",
    "process_budget",
]
