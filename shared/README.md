# Shared contracts

Owned by `lead`. The task is **Alibi** (`docs/TASK.md`). This folder defines what agents exchange so they can build in parallel. To change a contract, post a `need` to `lead`.

- `CONTRACTS.md`: the pinned decisions for money, time, identifiers, states, signing, approval, agent boundaries and the results format. The full text is in `alibi/SPEC.md`.
- `splits.json`: where the lockbox starts. For this task it is a case number, not a time. See `CONTRACTS.md`.

Each contract pins down:

- **Data:** columns, types, units, timezone, and the time at which each value becomes known, so nobody can use it early.
- **Interface:** the function signature every component implements, with an example call.
- **Split:** `splits.json` says where the lockbox starts, before anyone studies the data. `tools/lockbox.py` accepts a number as well as a time.
- **Results:** the metric names everyone reports. Every command prints its metrics as one JSON object on its last line, and `tools/results.py add` records it with the command, commit, split and number of variants tried. `eval` and the README both read these records.
