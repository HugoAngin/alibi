"""Lead's public API for teammates: the Alibi passport functions, re-exported from ``alibi.api``.

Import from here as RULES.md asks (``from agents.lead.api import load_fixture, ...``). Every
name is ``alibi.api``'s, and ``__all__`` is the same list. ``alibi/CONTRACTS.md`` documents each
function with JSON examples taken from the committed fixtures.
"""

from alibi.api import *  # noqa: F401,F403
from alibi.api import __all__  # noqa: F401
