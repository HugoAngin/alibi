"""Gemini agents for Alibi: Matcher, Detective, Challenger and Curator over the tool gateway.

Session S3 owns this package. The roles propose; the backend (``alibi.core``) validates every
reference against retrieved material, recomputes every amount and decides every status from
explicit fields. ``alibi.agents.runner.run_investigation`` has the same shape as the rules
version in ``alibi.core.investigate_rules`` so ``alibi.api`` can dispatch on ``mode``.
"""
