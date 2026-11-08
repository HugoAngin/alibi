# Explicit synthetic issuer configuration

Proposed native patch: `agents/eval/out/ISSUER_CONFIG.patch`. It is ready for the app owner to review and apply; no native application file was edited in the shared checkout. `git apply --check` passed against the working files on 8 November 2026.

## Problem and change

`kid_for_issuer()` falls back to `merchant_mock` for an unfamiliar issuer. The merchant therefore signs its event with a key pinned to `mock-marketplace`, and the passport correctly rejects the issuer mismatch. Mapping the event successfully does not establish trust.

The patch adds `trusted_issuers`, an optional `kid -> issuer_id` mapping supplied by trusted synthetic-service setup, to `connector_keystore()`, `_World()`, `run_case()` and `run_cases()`. The mapping creates additional record-signing keys before the world builds its shared trust registry. The merchant, webhook sink, bank and investigation then use that same registry. `bootstrap()` and the ingestion verifier need no changes.

The optional `synthetic_key_seed` argument to `_World()`, `run_case()` and `run_cases()` forwards to the keystore's existing deterministic derivation. This binds identical public keys for all simulated services across fresh isolated workers, rather than generating different issuer keys per case. The seed is secret signing-key material supplied explicitly by trusted setup; it is never read from a case or from an environment variable. The default remains `None`, which preserves random fresh keys and existing public behaviour.

Signing selects only a record-signing key already pinned to the event's exact issuer. Existing key choices are preserved for the public demo issuers. An explicitly mismatched signing key is refused. Unknown-issuer pushes are recorded as refused without aborting seeding; lookups return a typed `422 untrusted_issuer` response. No event is rewritten, and neither an event nor a `trusted_issuers` field embedded in a case can grant trust.

Configuration is copied, has path-safe key IDs, permits record-signing purpose only, and rejects collisions with any existing key or issuer. The global default key specification is unchanged. The legacy `kid_for_issuer()` fallback remains available to existing diagnostics; its return value alone no longer determines signing authority.

## Public evidence

Only public Case 1 was used for the new control. Its merchant delivery and lookup issuer IDs were replaced with the fixed test sentinel `public-synthetic-config-control`; all other event content stayed the same. Its answer key was stripped and was not consulted. No private inputs, private gold, captures or model calls were used.

Before the patch, the renamed public case returned 2 requests without a worker failure, but 0 supported claims and 0 merchant claim references. Under the patch with no trusted configuration it still has no supported claims or merchant claim references, including when the case itself supplies a purported trust mapping. With the explicit trusted mapping, all 3 public reference claims are supported. Their statuses, gaps, computed facts, evidence references, links and lookup counts exactly match the unmodified public Case 1 control. Both controls use zero model calls.

Verification against the staged native modules in disposable scratch:

- 26 new regression tests passed, including stable key pins across fresh worlds, seed propagation through `run_cases()` and `run_case()`, different seeds yielding different keys, refusal of malformed setup seeds, and ignoring a seed embedded in a case.
- 82 existing adapter, merchant and connector tests passed, including all 12 public gold-comparison tests. Those existing comparison tests consult public development answer keys only; no private gold was read.
- Total native coverage: 108 passing tests. The first combined run passed 96 tests in 6.07 seconds, and the 12 public comparison tests subsequently passed in 1.01 seconds.
- `git apply --check agents/eval/out/ISSUER_CONFIG.patch` passed.

The regressions cover default public behaviour, caller-supplied configuration, rejection of case-supplied trust, unknown push/lookup refusal, configuration copying, key-ID path traversal, existing key/issuer collisions, duplicate issuer pins, wrong signing purpose, wrong key/issuer pairing, and unchanged ingest rejection for a valid signature made by the wrong issuer or in the wrong domain. The existing model-mode tests use fake clients; no inference provider was called.

The evaluator's `agents/eval/trusted_setup.py` bootstrap was independently exercised against the staged native code using only a disposable synthetic setup file and the renamed public Case 1:

- The loader rejects a changed setup-file digest and strips authoring provenance and incidental fields before forwarding setup.
- Invocation spies confirm identical issuer mapping and seed reach `run_case()` in `rules`, `one_pass` and `agents` modes. These are argument-forwarding checks, not model runs.
- A wrong issuer public-key fingerprint is rejected before `run_case()` is invoked in each of those three modes.
- The real rules bootstrap returns 2 requests, 3 supported claims and zero model calls. Its generated world registry exactly matches all predeclared public-key fingerprints.
- The public case input remains unchanged, and no private setup, private case, private gold or capture was accessed.

These are public development and integration checks, not an unseen-data score or a replacement for the first frozen capture.

## Owner application

The patch changes only:

- `alibi/connectors/keys.py`
- `alibi/connectors/merchant.py`
- `alibi/adapter.py`
- `alibi/tests/test_trusted_synthetic_issuers.py` (new)

From the repository root, the app owner can apply and verify it:

```sh
git apply --check agents/eval/out/ISSUER_CONFIG.patch
git apply agents/eval/out/ISSUER_CONFIG.patch
uv run --frozen python -m pytest -q alibi/tests/test_trusted_synthetic_issuers.py alibi/tests/test_adapter.py alibi/tests/test_connectors_plug.py alibi/tests/test_connectors_merchant.py
```

An explicit public-only setup example is:

```python
run_case(
    public_case,
    "rules",
    root=disposable_root,
    trusted_issuers={"public_synthetic_config_control": "public-synthetic-config-control"},
    synthetic_key_seed=private_setup["synthetic_key_seed"],
)
```

## Required harness integration

This patch supplies the native API; it does not invent or discover a private pack's issuer identities. An authorised pack owner must provide the intended synthetic-service issuer registry as trusted setup metadata. Do not derive that registry by scanning merchant events, outputs, gold, or case-local metadata, and do not grant trust to every issuer encountered.

An original static issuer declaration in trusted synthetic authoring setup is a suitable source when its provenance is established independently of the case event lists. This review did not inspect that private authoring source or verify its contents.

The evaluator must explicitly forward the same fixed mapping and seed into each isolated worker's `run_case()` call for every compared system. Generate a high-entropy random seed once (for example, `secrets.token_hex(32)`) and store it only in the private trust file referenced by path. Never publish the seed, derive it from issuer names or use the predictable public test seed. All simulated service keys derive from this seed, including the built-in bank and package keys, so the entire synthetic run registry is reproducible.

Before capture, construct a disposable keystore from that exact trusted mapping and seed, record each issuer's key ID and public-key fingerprint, and verify that these match the declared trusted pins. A fingerprint can be `sha256(bytes.fromhex(entry.public_key)).hexdigest()` for each record-signing registry entry. Bind the private trust-file digest and declared public-key fingerprints to the run configuration and capture receipt. The isolated harness must verify the file digest and seed-derived fingerprints before invoking the candidate. The candidate's `run_case()` clears its per-case directory, so old PEM files cannot silently override the declared seed; check the emitted registry against the same pins in the retained worker evidence as well. Keep all trust configuration outside the case payload, and publish only the authorised public-key fingerprints rather than private setup material.

Preserve the original frozen capture and label any run on previously opened inputs as a second look on reused inputs. There is no claim here that private registry metadata has been verified by this review.

Native source files at patch construction, SHA-256:

```text
alibi/connectors/keys.py d206df044453344b13931d53b689cffb040fbbd5b89cd425f445f8670be1802a
alibi/connectors/merchant.py 7fba7acab1f10a0ec6ba2a6d774a8e9b8484c77c150f1f614c4571b46f75160b
alibi/adapter.py b909871969442aadabf8b2f3cb499862f77f0a4e378786bc98785dbd043f5dae
```
