"""FakeClient: scripted answers with the GeminiClient.generate signature, for other sessions' tests.

    from alibi.llm.fake import FakeClient
    fake = FakeClient({
        "challenger": {"claim_id": "c1", "proposed_status": "unresolved", ...},   # one answer, reused
        "matcher": [first_answer, second_answer],                                 # answers in order
        "curator": lambda model, system, user, schema: {...},                     # computed
        "detective": Unavailable("simulated outage"),                             # raised
    })
    fake.generate(FLASH, system, user, schema, budget)

The script key is matched against the user text, then the system prompt, then the model
name, case-insensitively, longest key first. The special key "*" is the fallback. Every
answer is validated against the schema that was passed in, exactly as the real client
would, so a scripted answer that drifts from the schema fails the test that uses it.
Pass `validate=False` to skip that. `fake.calls` records every invocation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from alibi.llm.client import BudgetExceeded, CallBudget, Result, SchemaValidationError, Usage, prompt_sha256
from alibi.llm.schema import validate as _validate

Script = Any  # dict | list[dict] | Callable | BaseException


@dataclass
class FakeCall:
    model: str
    system: str
    user: str
    json_schema: dict
    prompt_sha256: str
    answer: Any = None
    error: Optional[BaseException] = None


@dataclass
class FakeClient:
    scripts: dict[str, Script] = field(default_factory=dict)
    validate: bool = True
    usage: Usage = field(default_factory=lambda: Usage(prompt_tokens=10, output_tokens=10, total_tokens=20))
    calls: list[FakeCall] = field(default_factory=list)
    process_budget: CallBudget = field(default_factory=lambda: CallBudget(10_000, name="fake"))

    def generate(
        self,
        model: str,
        system: str,
        user: str,
        json_schema: dict,
        budget: Optional[CallBudget] = None,
    ) -> Result:
        key = prompt_sha256(model, system, user, json_schema)
        call = FakeCall(model, system, user, json_schema, key)
        self.calls.append(call)

        self.process_budget.spend()
        if budget is not None:
            try:
                budget.spend()
            except BudgetExceeded:
                self.process_budget.refund()
                call.error = BudgetExceeded(str(budget.name))
                raise

        script = self._select(model, system, user)
        try:
            answer = self._resolve(script, model, system, user, json_schema)
        except BaseException as exc:  # noqa: BLE001 - scripted failures are re-raised as-is
            call.error = exc
            raise

        if self.validate:
            problems = _validate(answer, json_schema)
            if problems:
                call.error = SchemaValidationError(problems)
                raise call.error
        call.answer = answer
        return Result(data=copy.deepcopy(answer), model=model, prompt_sha256=key, cached=False, usage=self.usage)

    # ---- helpers
    def _select(self, model: str, system: str, user: str) -> Script:
        haystacks = [user.lower(), system.lower(), model.lower()]
        for hay in haystacks:
            for name in sorted(self.scripts, key=len, reverse=True):
                if name != "*" and name.lower() in hay:
                    return self.scripts[name]
        if "*" in self.scripts:
            return self.scripts["*"]
        raise KeyError(f"FakeClient has no script for this call (keys: {sorted(self.scripts)})")

    @staticmethod
    def _resolve(script: Script, model: str, system: str, user: str, json_schema: dict) -> Any:
        if isinstance(script, BaseException):
            raise script
        if isinstance(script, list):
            if not script:
                raise IndexError("FakeClient script list is exhausted")
            item = script.pop(0)
            return FakeClient._resolve(item, model, system, user, json_schema)
        if callable(script):
            return script(model, system, user, json_schema)
        return script

    @property
    def call_count(self) -> int:
        return len(self.calls)
