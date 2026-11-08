# Security review: bank service, mock merchant, public router

Owner: `execution` (lead's 01:57Z item 3). Reviewed on `main` at `2842b58`: `alibi/bank/service.py`, `alibi/connectors/merchant.py` (admin routes), `agents/research/deploy/router.py`. Each finding is a strict expected-failure test in [`../test_backend_security.py`](../test_backend_security.py); passing controls are in the same file.

```bash
uv run --frozen python -m pytest agents/execution/test_backend_security.py -q -rxX
```

All data is synthetic. "Anyone" below means anyone who can reach the public Modal router.

## Findings

| # | Severity | What | Reproduced |
| --- | --- | --- | --- |
| F1 | High | The bank's `POST /admin/seed` and `POST /admin/advance` need no credential. Seed resets both banks' inboxes, requests and dispositions; advance moves the clock forward, which expires every package and request. | No token: seed 200, advance to 2026-12-31 200 |
| F2 | High | All merchant `/admin/*` routes need no credential: `seed`, `advance`, `retry`, `correct`, `lookups`, `deliveries`, `state`. `/admin/correct` makes the mock merchant **sign** a correction with arbitrary changes and push it to the passport. That can invalidate Maya's approval mid-demo and puts "issuer-signed" content the merchant never meant to sign into her history. `/admin/state` dumps merchant state. | No token: correct `rec-0001-021` → `status: failed` returned 200 with signed revision 2; state 200 (5 KB) |
| F3 | High, **fixed** by eval in `agents/eval/deploy/router.py` (40e03c1): every `admin` segment is refused before forwarding, including encoded and trailing-slash forms | The public router blocks only `/reset`, `/admin/reset` and `/__debug`, so `/bank/admin/*` and `/merchant/admin/*` pass through. `/bank/admin/reset/` (trailing slash) also passes the blocklist. | `is_blocked` is False for all six paths tested |
| F4 | Low | Merchant lookup routes (`/records/{id}`, `/settlements`, `/payouts`) are public through `/merchant/...`. They are meant for the passport's gateway; anyone can enumerate Maya's synthetic merchant records. | By reading; no test |

## Fixes, smallest first

Status 06:20Z: F3 fixed (the hosting router is now eval's). F1 and F2 remain open inside the container: the router keeps them off the internet, but any process that can reach 8101/8102 can still use the admin routes.


1. **Router (F3), now.** In `agents/research/deploy/router.py`, block any path with an `admin` segment and normalise the trailing slash before matching:
   ```python
   def is_blocked(path: str) -> bool:
       p = path.rstrip("/") or "/"
       segments = p.split("/")
       return "admin" in segments or any(p == b or p.endswith(b) for b in BLOCKED)
   ```
   Then `demo_services` seeds over the container-internal origins (`127.0.0.1:8101/8102`), never through the router. This alone closes F1 and F2 from the internet.
2. **Services (F1, F2).** Require a bearer `ALIBI_ADMIN_TOKEN` on every `/admin/*` route in both services (a FastAPI dependency like `require_recipient`), with constant-time comparison, and have `alibi/connectors/clients.py` send it. This defends inside the container too.
3. **Lookups (F4).** Require the gateway's token on `/records`, `/settlements` and `/payouts`, or keep them off the public router.

When a fix lands, its test XPASSes and fails the run, so remove that `expectedFailure` marker.

## Controls that hold (passing tests)

- Recipient routes reject a missing token, a wrong token, a non-Bearer scheme, and the feed token (401).
- The feed token cannot act as a bank, and a bank token cannot read the feed.
- Bank B cannot read Bank A's request by ID, and it is absent from Bank B's list.
- An unknown or other bank's package ID gives 404 for both read and disposition.
- Tokens never appear in inbox, audit, request or whoami responses.
- The router still blocks `/reset`, `/admin/reset`, `/bank/admin/reset` and `/__debug`.

From reading `service.py`, also sound:
- Tokens are compared with `secrets.compare_digest`.
- The recipient and analyst come from the token only, and posted fields are ignored.
- A rejected package stores no contents.
- clear and escalate are final (`disposition_final`).
- A follow-up's scope is narrowed from the parent and refused if wider (`scope_wider_than_parent`, including kind changes).
- Chosen follow-up request IDs cannot collide with case or other-bank IDs.
- Delivering a package ID again with different bytes is refused (`package_id_reused`).
