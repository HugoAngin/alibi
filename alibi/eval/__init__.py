"""Alibi evaluation: scorer, gold replay, rules reference, sealing and the probe harness.

Modules
-------
- ``common``: refs, time, canonical JSON, point-in-time views of a case, sealed-case loading.
- ``scorer``: scores predictions files against gold on the safe-complete endpoint.
- ``oracle``: replays gold as a prediction (a scorer check, not a system under test).
- ``rules_reference``: the pinned gold rules with a fixed retrieval strategy.
- ``seal``: sha256 commitments over sealed cases, gold, candidate commit and scoring rules.
- ``probes``: the security probe harness skeleton over ``agents/execution/out/probes.json``.

Nothing here imports ``alibi.core`` or ``alibi.api``. Cases are read only through
``agents.data.api``.
"""
