# Bank console redesign (research, for execution to land)

Patch: `bank-console-redesign.patch`. It applies cleanly to main at the time of writing. To land it:

```bash
git apply agents/research/out/patches/bank-console-redesign.patch
```

Then run `cd web && pnpm verify`. In research's isolated copy this gave 0 astro errors in all three apps, 96 tests passing and three builds.

## What changes

**The case queue (`/`) is redesigned.** The console is now a graphite tool with one steel-teal accent.
- It shows a filterable case table with priority and status.
- The whole row is clickable.

**Each case opens in a panel that slides in from the right** (`CaseQueue.astro`). The panel shows:
- the amount
- the booking time, payer reference and transaction
- why the case was flagged, its priority and any restriction
- "What the bank can see" next to "What it cannot see"

Its footer carries a solid "Request customer's alibi" button.

**Requesting is one review screen and one Send.** The panel turns into "Request Maya's Alibi", which shows:
- the two questions from data's case 1 request
- what the request is about
- the reply deadline
- who sees the answer

**The send itself:**
- Send posts `kind=request, case_id` to `/act/`.
- The server looks up the request ID from the case and posts `{request_id: "rfc-0001-01"}` to the bank's `/rfc` with the Bank A bearer.
- The form never supplies an ID or a path.
- Filler cases have no request, so nothing is sent for them. `tests/request.test.ts` covers this.

**After sending, the panel follows the bank's own inbox** (`caseState.ts`): request sent, then the customer reviewing, then the signed package received. Once the package arrives, "Open the Alibi package" links to the existing package page.

**Every state is a real server-rendered URL:**
- `/`
- `/cases/<id>/`
- `/cases/<id>/request/`

With JavaScript the screens switch in place, and back, forward and Escape all work. Without it, every link and form still works.

**Preview mode:** without a configured bank service, every case starts at "not requested" and Send is disabled with a note saying why.

**Small changes elsewhere:**
- `RequestAlibiButton` gains `variant="solid"` and `block`. The default outline style is unchanged.
- Sign-in now lands on the case queue.
- The other console pages pick up the new header and tokens.

## Checked

**Against a stand-in bank service:**
- The console asks for sign-in first.
- Send made exactly one call, `POST /rfc {"request_id":"rfc-0001-01"}`, with the bearer token.
- The panel then showed "Awaiting customer".
- After a package was marked delivered, it showed "Alibi received" with a link to the package.

**Layout:** desktop at 800 px wide, and phone at 375 px with a full-width panel and the button fixed at the bottom.

**Not checked:** against the real `alibi/bank/service.py`. The real `/rfc` also forwards the request to the holder, so it needs a seeded clock (`/admin/seed`) first.
