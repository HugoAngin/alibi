# Shared contracts

Owned by `lead`. Once the task is known, this folder defines what agents exchange: data schemas, the strategy interface, and the results format. Agents build against these so they can work in parallel. To change one, post a `need` to `lead`.

Each contract pins down:

- **Data:** columns, types, units, timezone, and the time at which each value becomes known, so nobody can use it early.
- **Interface:** the function signature every strategy or model implements, with an example call.
- **Split:** `shared/splits.json` says where the lockbox starts, before anyone studies the data. Pick the last fifth or so of the time range, or the organisers' held-out period if there is one: `{"lockbox_from": "2024-07-01T00:00:00Z"}`. A step or episode number also works. Data's loaders return `visible(frame, "<time column>")` from `tools/lockbox.py`.
- **Results:** the metric names everyone reports, and how costs are assumed. Every command prints its metrics as one JSON object on its last line, and `tools/results.py add` records it with the command, commit, split and number of variants tried. `eval` and the README both read these records.
