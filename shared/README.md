# Shared contracts

Owned by `lead`. Once the task is known, this folder defines what agents exchange: data schemas, the strategy interface, and the results format. Agents build against these so they can work in parallel. To change one, post a `need` to `lead`.

Each contract pins down:

- **Data:** columns, types, units, timezone, and the time at which each value becomes known, so nobody can use it early.
- **Interface:** the function signature every strategy or model implements, with an example call.
- **Results:** one record format for every reported number: metric, value, test period, the command that reproduces it, seed, number of variants tried, and the cost assumptions. `eval` and the README both read these records.
