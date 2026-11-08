"""Lead's public API for teammates: the Alibi passport functions, re-exported from ``alibi.api``.

Import from here as RULES.md asks (``from agents.lead.api import load_fixture, ...``). Every
name is ``alibi.api``'s, and ``__all__`` is the same list, with one difference: ``parse_rule_text``
is the ``/rules`` parser chain (``alibi.app.policy_chain``: ``agents.execution.api.compile_policy``
mapped onto core rules, then ``alibi.core.policy.parse_rule_text``; the app's ``/rules`` route also
puts Gemini first when it asks for it with ``use_gemini=True`` and ``GEMINI_API_KEY`` is exported
with replay off). A plain ``parse_rule_text(text)`` from here runs the deterministic layers only
and never calls the network, so ``agents.execution.policy_harness --parser rules`` scores the
same deterministic chain the app falls back to. It returns a ``RuleParse`` subclass with
``.source`` naming the layer that answered. ``alibi/tests/test_api.py::test_agents_lead_api_reexports_alibi_api``
checks every other name is ``alibi.api``'s object. ``alibi/CONTRACTS.md`` documents each
function with JSON examples taken from the committed fixtures.
"""

from alibi.api import *  # noqa: F401,F403
from alibi.api import __all__  # noqa: F401
from alibi.app.policy_chain import parse_rule_text  # noqa: F401  (overrides alibi.api's core parser)
