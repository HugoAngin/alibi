# Alibi evaluation handoff

- Arnav assigned `eval` the sealed cases and independent gold. He accepted the case format in `fffa496`. The public format is in `CASE_FORMAT.md`.
- Use Hugo's 40 development cases and Mouaad's probe catalogue for development. The sealed corpus is authored separately and stays outside the tracked tree until freeze.
- The private bundle has 24 cases in six families, plus 12 adversarial cases. The structural and arithmetic audits passed. Its 47 adversarial steps map to 44 catalogue IDs and cover all 32 core IDs. Candidate execution remains pending.
- The local sharing flow is a reference for public tests. It exercises 17 of the 47 catalogue probes, with 15 refusals and two accepted controls. Arnav's S2 session owns the app's bank service and verifier, and S4 owns the console.
- The capture scorer counts missing RFCs, checks links and question evidence, and checks nested package fields and literals when gold specifies privacy restrictions. Run `uv run --frozen python -m pytest -q tools agents/data agents/execution agents/eval` for the local checks.
- The deterministic settlement matcher, reconciliation, period totals and fixed baseline retrieval are exported in `api.py`. Checks against all 40 public ledgers passed.
- Checked exported branch snapshots with no model credentials. Gemini client and prompts at `0320ca7` passed 46 tests. Static UI at `6466584` passed 22 tests. These snapshots test their own components. The app capture needs the integrated adapter.
- Open need from 22:39 GMT on the Message Board. Arnav will publish the app adapter that takes case inputs and returns a package or refusal per RFC. Develop the evaluator against the agreed case schema while it is pending.
