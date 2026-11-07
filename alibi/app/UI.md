# Alibi UI: templates and their context variables

Static UI built in session S4a. No routes or application logic live here; the
wiring session passes the contexts below to Jinja2. The sample contexts in
`alibi/app/preview_render.py` are the executable version of this document: every
shape here is copied from a `ctx_*` function there, and the test
`alibi/tests/test_ui_preview.py` renders every template with them.

## How to render

```python
from alibi.app.preview_render import make_env   # FileSystemLoader on alibi/app/templates, StrictUndefined
html = make_env().get_template("passport/home.html").render(**context)
```

- `make_env()` uses `StrictUndefined`: a missing variable or key raises instead of rendering blank. Pass every variable listed for a template, even when its value is `None` or `[]`.
- Autoescape is on for `.html`. Pass plain strings; the templates escape them.
- Serve `alibi/app/static/alibi.css` at `<static_prefix>/alibi.css`.
- Preview every page: `uv run --frozen python -m alibi.app.preview_render` writes `data/alibi/ui_preview/*.html` plus `index.html`.

## Conventions

- **Money** is always `{"minor": int, "currency": "GBP"}`. Templates format it with the `money` macro (`£2,160.00`) and show the integer with `money_raw` (`216000 GBP`) where arithmetic matters. Negative minor units render with a leading minus.
- **Times** are UTC ISO-8601 strings and are printed as given. Periods are half-open and printed as `[from, to)`.
- **Three separate vocabularies**, never conflated:
  - link state: `unmatched | ambiguous | linked` (macro `link_badge`)
  - source trust: `issuer_verified | bank_observed | self_supplied` (macro `trust_badge`)
  - claim status: `supported | contradicted | unresolved` (macro `claim_badge`)
  - everything else (connection status, request status, check results) uses `status_badge(value)`; the CSS colours known values and leaves unknown ones neutral.
- Record references are `record_id` plus integer `revision`, printed as `po_7781 r2`.
- Forms post to the `*_action` URL in the context with `method="post"`. Field names are listed per template.
- `href` values are opaque strings. The templates never build URLs themselves.

## Common context (every template, from `base.html`)

| Variable | Type | Notes |
| --- | --- | --- |
| `page_title` | str | Used in `<title>`: "<page_title> · Alibi" |
| `area` | `"holder" \| "analyst" \| "public"` | Sets the header label and `body.area-<area>` |
| `nav_items` | list of `{key: str, label: str, href: str, active: bool}` | May be empty; the first item's href is the brand link |
| `actor` | `{name: str, role: str}` | Shown top right |
| `replay_mode` | bool | When true, `partials/replay_banner.html` is included above the content |
| `replay` | `{label: str, recorded_at: str, source: str}` or `None` | Read only when `replay_mode` is true |
| `flashes` | list of `{kind: "info" \| "success" \| "error", text: str}` | Rendered as banners under the nav |
| `static_prefix` | str | e.g. `/static` |
| `now_utc` | str | Printed in the footer |

Blocks a child template may override: `title`, `head`, `header_extra`, `content`, `footer`, `scripts`.

## Partials

- `partials/badges.html` exports `trust_badge(trust)`, `claim_badge(status)`, `link_badge(state, count=None)`, `status_badge(value, label=None)`.
- `partials/money.html` exports `money(amount, show_sign=False)` and `money_raw(amount)`.
- `partials/replay_banner.html` reads `replay` (see common context). Included by `base.html`; do not include it again.

## Grayscale-safe badges

Each trust badge and each claim badge has a text mark and a distinct border, so they stay distinct without colour:

| Badge | Mark | Border |
| --- | --- | --- |
| issuer_verified | ● | solid, filled dark |
| bank_observed | ◆ | 2px solid |
| self_supplied | ○ | dashed |
| supported | ✓ | solid |
| contradicted | ✕ | double |
| unresolved | ? | dashed |
| linked | = | solid |
| ambiguous | ~ | dotted |
| unmatched | – | dashed |

## Templates

### alibi/app/templates/passport/home.html

Preview name `passport_home`, context from `ctx_passport_home()`. Extends `base.html` and reads the common context through it (`page_title`, `area`, `nav_items`, `actor`, `replay_mode`, `replay`, `flashes`, `static_prefix`, `now_utc`). Imports `trust_badge`, `claim_badge`, `link_badge`, `status_badge` and `money`, `money_raw` (`claim_badge` and `money_raw` are imported but not called on this page). Sections in order, each a `section.card.section-anchor` with an id and an `aria-labelledby` heading (`<id>-title`): `#holder`, `#connections`, `#transactions`, `#evidence`, `#alerts`, `#rules` (also `card--private`), `#requests`, `#sharing`. Every list section renders one `p.empty` line when its list is empty; `#holder` and `#evidence` are always rendered. No forms, no POST actions.

- `holder` — dict (kv list)
  - `name` str
  - `subject_ref` str (in `<code>`)
- `connections` — list of dict (`ul.items`; each `li` gets `id="<connection_id>"`; header shows the list length)
  - `connection_id` str
  - `name` str (links to `href`)
  - `kind` str, one of `merchant` | `bank` | `import` (printed raw)
  - `status` str, one of `connected` | `syncing` | `error` | `disconnected` (rendered with `status_badge`)
  - `trust` str, one of `issuer_verified` | `bank_observed` | `self_supplied` (rendered with `trust_badge`)
  - `last_synced_at` str, UTC ISO-8601, printed raw
  - `record_count` int (pluralised "record"/"records")
  - `href` str
- `transactions` — list of dict (`table.table` in `div.table-wrap`; each `tr` gets `id="<txn_id>"`)
  - `txn_id` str
  - `revision` int (printed as `r<revision>`)
  - `booked_at` str, UTC ISO-8601
  - `description` str (links to `href`)
  - `amount` dict `{minor: int, currency: str}` (rendered with `money`, wrapped in `span.item__amount.amount--<direction>`)
  - `direction` str, one of `credit` | `debit` (printed raw and used as the amount class suffix)
  - `link_state` str, one of `linked` | `ambiguous` | `unmatched` (rendered with `link_badge(link_state, linked_count)`)
  - `linked_count` int (0 hides the count in the badge)
  - `href` str
- `evidence_status` — dict (`div.stats` grid of four `div.stat`; always rendered)
  - `linked` int
  - `ambiguous` int
  - `unmatched` int
  - `total` int
  - `last_checked_at` str, UTC ISO-8601 (card header, "Last checked <value>")
  - `superseded_sources` int (pluralised sentence "<n> source record(s) superseded by a newer revision")
- `alerts` — list of dict (`ul.items`; each `li` gets `id="<alert_id>"` and class `alert alert--severity-<severity>`)
  - `alert_id` str
  - `kind` str (printed with `_` replaced by spaces)
  - `severity` str, one of `info` | `warning` | `high` (rendered with `status_badge`; all three have colour rules in alibi.css)
  - `title` str (links to `href`)
  - `created_at` str, UTC ISO-8601
  - `status` str, one of `open` | `resolved` (rendered with `status_badge`)
  - `href` str
- `rules` — list of dict (`ul.items` inside the `card--private` card; each `li` gets `id="<rule_id>"`)
  - `rule_id` str
  - `text` str (links to `href`)
  - `status` str, one of `active` | `held` | `rejected` (rendered with `status_badge`)
  - `summary` str
  - `href` str
- `requests` — list of dict (`table.table` in `div.table-wrap`; each `tr` gets `id="<request_id>"`)
  - `request_id` str (in `<code>`, links to `href`)
  - `recipient` str
  - `question_count` int
  - `received_at` str, UTC ISO-8601
  - `expires_at` str, UTC ISO-8601
  - `status` str, one of `new` | `investigating` | `draft` | `approved` | `sent` | `refused` | `expired` (rendered with `status_badge`)
  - `href` str
- `sharing_history` — list of dict (`table.table` in `div.table-wrap`; each `tr` gets `id="<package_id>"`)
  - `package_id` str (in `<code>`, links to `href`)
  - `request_id` str (shown under the package id as "for <request_id>")
  - `recipient` str
  - `sent_at` str, UTC ISO-8601
  - `expires_at` str, UTC ISO-8601
  - `item_count` int
  - `status` str, one of `delivered` | `acknowledged` | `superseded` | `expired` (rendered with `status_badge`)
  - `href` str

Hard-coded links in this template (section header links, matching `holder_nav`): `/alerts`, `/rules`, `/requests`. The template does not read any other context key.

### alibi/app/templates/passport/transaction.html

Preview name `transaction_detail`, context from `ctx_transaction_detail()`. Extends `base.html` (common context as above). Imports the four badge macros and `money`, `money_raw`. Sections in order, each a `section.card.section-anchor` with an id and `aria-labelledby` heading: `#summary`, `#sources`, `#reconciliation`, `#claims`, `#changes`. `sources`, `claims` and `changes` render one `p.empty` line when empty; `#summary` and `#reconciliation` are always rendered. No forms, no POST actions.

- `txn` — dict (page title "Transaction <txn_id>", lede = description, then `dl.kv`)
  - `txn_id` str
  - `revision` int (printed as `r<revision>`)
  - `booked_at` str, UTC ISO-8601
  - `description` str (lede and kv row)
  - `amount` dict `{minor: int, currency: str}` (rendered with `money` in `span.item__amount.amount--<direction>`, followed by "stored as" `money_raw`)
  - `direction` str, one of `credit` | `debit` (printed raw and used as the amount class suffix)
  - `link_state` str, one of `linked` | `ambiguous` | `unmatched` (rendered with `link_badge(link_state, linked_count)` in the card header, beside the trust badge)
  - `linked_count` int
  - `href` str (present in the contract, not rendered)
  - `account_label` str
  - `connection_name` str
  - `references` list of str (each in `<code>`, comma separated; empty list renders "No payment references on this transaction.")
  - `provenance` dict (one kv row: "Connection <connection_id>, retrieved <retrieved_at>, source version <source_version>")
    - `connection_id` str
    - `retrieved_at` str, UTC ISO-8601
    - `source_version` str
  - `trust` str, one of `issuer_verified` | `bank_observed` | `self_supplied` (rendered with `trust_badge` in the card header; `bank_observed` in the sample)
- `sources` — list of dict (`ul.items`; each `li` gets `id="<record_id>-r<revision>"`; header shows the list length)
  - `record_id` str (in `<code>`, links to `href` together with `r<revision>`)
  - `revision` int
  - `kind` str, e.g. `order` | `refund` | `fee` | `payout` | `settlement` | `receipt` (printed raw)
  - `issuer` str
  - `trust` str, one of `issuer_verified` | `bank_observed` | `self_supplied` (rendered with `trust_badge`)
  - `occurred_at` str, UTC ISO-8601
  - `amount` dict `{minor: int, currency: str}` (rendered with `money` in the item head and `money_raw` in the meta line)
  - `status` str, free-form record status, e.g. `completed` | `charged` | `settled` | `released_to_balance` | `imported` (rendered with `status_badge`; `completed`, `settled`, `charged`, `released_to_balance` and `imported` have colour rules in alibi.css, other values use the neutral `.badge--status` style)
  - `summary` str
  - `href` str
  - `signature` dict
    - `present` bool (False renders "no issuer signature"; `key_id` and `verified` are read only when True)
    - `key_id` str or None (printed in `<code>` when `present`)
    - `verified` bool or None (when `present`: True renders "verified", False renders "not verified")
  - `superseded` bool (True adds `span.badge.badge--status.badge--superseded` "superseded" after the title and the note "superseded by a newer revision; shown for history" in the meta line)
- `reconciliation` — dict (`table.recon` in `div.table-wrap`; columns: sign, line, sources, amount via `money`, minor units via `money_raw`)
  - `title` str (card header)
  - `lines` list of dict (one `tr` each)
    - `label` str
    - `kind` str (printed in parentheses after the label)
    - `sign` str, `+` | `-` (in `td.sign`)
    - `amount` dict `{minor: int, currency: str}` (rendered with `money` and `money_raw`)
    - `source_ids` list of str (each in `<code>`, comma separated)
  - `result` dict `{minor, currency}` (the `tr.recon__total` row labelled "Result", sign `=`, `money` and `money_raw`)
  - `compared_to` dict (first `tr.recon__compare` row, "Compared to <label>", third cell says "matches" or "does not match")
    - `label` str
    - `amount` dict `{minor, currency}` (`money` and `money_raw`)
  - `difference` dict `{minor, currency}` (second `tr.recon__compare` row labelled "Difference", `money` and `money_raw`)
  - `matches` bool (True renders "matches" and `status_badge("ok", "Matches")`; False renders "does not match" and `status_badge("fail", "Does not match")`)
  - `expression` str, printed verbatim in `p.recon__expression` (e.g. `240000 - 18000 - 6000 = 216000`)
  - `computed_by` str (e.g. `code`)
  - `computed_at` str, UTC ISO-8601 (rendered as "Computed by <computed_by> at <computed_at>.")
- `claims` — list of dict (`ul.items`; each `li` gets `id="<claim_id>"`)
  - `claim_id` str (in `<code>`)
  - `question_id` str (in `<code>`, "question <question_id>")
  - `question` str (item title)
  - `status` str, one of `supported` | `contradicted` | `unresolved` (rendered with `claim_badge`)
  - `rationale` str
  - `href` str (link text "View trace")
- `changes` — list of dict (`ul.items`; each `li` gets `id="<change_id>"`)
  - `change_id` str
  - `received_at` str, UTC ISO-8601 ("Received <value>")
  - `record_id` str (in `<code>`; item title "<record_id> r<older.revision> to r<newer.revision> · <kind>")
  - `kind` str
  - `effect` str
  - `diff` list of dict (`table.table.table--compact` in `div.table-wrap`; headers "Field", "Before (r<older.revision>)", "After (r<newer.revision>)")
    - `field` str (in `<code>`)
    - `before` str (in `span.diff-before`)
    - `after` str (in `span.diff-after`)
  - `older` dict (inside `details.disclosure` with summary "View older revision r<revision>", left cell of `div.revision-compare` as `div.revision.revision--older`, titled "Revision r<n>" with a `badge--superseded` "superseded" mark)
    - `revision` int (also used in the item title, the diff headers and the summary label)
    - `received_at` str, UTC ISO-8601 (first `dl.kv` row "Received")
    - `href` str (link "Open revision r<revision>")
    - `fields` list of dict `{name: str, value: str}` (remaining `dl.kv` rows, `name` in `<code>`)
  - `newer` dict (right cell of `div.revision-compare` as `div.revision`, titled "Revision r<n> (active)")
    - `revision` int (also used in the item title and the diff headers)
    - `received_at` str, UTC ISO-8601
    - `href` str (link "Open revision r<revision>")
    - `fields` list of dict `{name: str, value: str}` (rendered as a `dl.kv`)

### alibi/app/templates/passport/alerts.html

Preview names: `alerts` (merchant_lookup.result is None), `alerts_lookup_unverified` (result set, status "unverified"). Context builder: `ctx_alerts(with_lookup_result)`. Inherits every base.html variable (page_title, area, nav_items, actor, replay_mode, replay, flashes, static_prefix, now_utc). Imports all four badge macros and both money macros; only `status_badge` is used (no trust, claim-status, link-state or money values exist in this context).

- `alerts` — list of dict, rendered in order; an empty list shows one `.empty` paragraph instead of the list. Each alert becomes `<article class="card alert alert--severity-<severity> section-anchor" id="<alert_id>">`
  - `alert_id` — str, used as the element id (deep link `/alerts#alrt_001`) and printed in `<code>`
  - `kind` — str, shown via `status_badge(kind)` (underscores print as spaces, neutral grey style). Sample values: `settlement_discrepancy`, `merchant_unverified`, `unexplained_credit`
  - `severity` — enum `info | warning | high`; drives the card border class `alert--severity-<severity>` and is printed as plain text ("Severity: warning")
  - `title` — str, the card heading (h3)
  - `explanation` — str, one paragraph
  - `created_at` — str, UTC ISO-8601, printed raw ("Raised <created_at>")
  - `status` — enum `open | resolved`, via `status_badge(status)` in the card head
  - `txn_href` — str, href of the "Open the transaction" link
  - `facts` — list of dict, rendered under the h4 "Facts behind this alert" as `table.table.table--compact` inside `.table-wrap` with columns Fact / Value / Source; an empty list replaces the table with a `.empty` paragraph ("No facts recorded for this alert.")
    - `label` — str
    - `value` — str, already formatted; the template prints it verbatim and does not apply money()
    - `source_id` — str, printed in `<code>` inside the source link
    - `source_revision` — int, printed as `r<n>` after the source id
    - `href` — str, link target of the Source cell
  - `suggested_checks` — list of str, bulleted under an h4 "Suggested checks"; the whole h4 + list is omitted when the list is empty
- `merchant_lookup` — dict, rendered as `<section id="merchant-lookup" class="card">`
  - `action` — str, POST target of the lookup form; single text input named `query`, submit button "Look up" (`btn btn--primary`)
  - `query` — str, prefills the text input (may be "")
  - `result` — `None` or dict. None shows a `.empty` hint that this is a mock registry check and unknown merchants come back as unverified (unknown, not fraudulent). A dict shows an h3 "Result" (`id="lookup-result"`) followed by a `dl.kv`
    - `query` — str
    - `status` — enum `verified | unverified`, via `status_badge(status, status|capitalize)` so it prints "Verified" / "Unverified"
    - `name` — str or `None`; None prints "no match" in `.muted`, a str prints verbatim ("Registry name" row)
    - `registry` — str
    - `checked_at` — str, UTC ISO-8601, printed raw
    - `notes` — str (the sample explains that unverified means unknown to the registry, not fraudulent)

Section ids: `alerts`, one `<alert_id>` per card, `merchant-lookup`, `lookup-result` (only when result is set). Heading ids also present: `alerts-heading`, `lookup-heading`. Keep alert ids distinct from these.

### alibi/app/templates/passport/rules.html

Preview names: `rules` (interpretation None, rule_form.text ""), `rules_interpretation` (interpretation set). Context builder: `ctx_rules(with_interpretation)`. Inherits every base.html variable. Only `status_badge` is used from the imported macros; money macros are imported but unused.

- `rule_form` — dict, rendered as `<section id="add-rule" class="card">` titled "Add a rule in plain English"
  - `action` — str, POST target of the form; textarea named `text`, submit button "Interpret" (`btn btn--primary`)
  - `text` — str, prefills the textarea (may be "")
  - `examples` — list of str, bulleted under an h3 "Examples"; the heading and list are omitted when empty
- `interpretation` — `None` or dict. None omits the whole `#interpretation` card. A dict renders `<section id="interpretation" class="card">` titled "Alibi's interpretation"
  - `rule_id` — str, shown in `<code>` in the card head
  - `text` — str, the original wording, shown in a `dl.kv` row "You wrote"
  - `plain_lines` — list of str, bulleted under h3 "In plain terms" (rendered even if empty: empty `<ul>`)
  - `policy` — dict, rendered under h3 "Typed policy"
    - `kind` — enum `deny | generalise | age | recipient | alert_pref`, via `status_badge(kind)` in the "Kind" row of a `dl.kv`
    - `fields` — list of `{name: str, value: str}`, one `dt`/`dd` row each after "Kind"; value printed in `<code>`. Note: the sample includes a field named `kind`, which then appears twice (badge row plus field row)
    - `json` — str, printed verbatim inside `<pre>` within `<details class="disclosure">` with summary "Policy as JSON"
  - `warnings` — list of str, bulleted inside `.banner.banner--info` titled "Before you confirm"; the banner is omitted when empty
  - `confirm_action` — str, POST target of the inline form whose button is `btn btn--primary` "Confirm this interpretation"
  - `reject_action` — str, POST target of the inline form whose button is `btn` "Reject". Both forms sit in one `.btn-row`
- `active_rules` — list of dict, rendered in `<section id="active-rules" class="card">` titled "Active rules" as `ul.items`; an empty list shows a `.empty` line ("No active rules. Default-minimum disclosure still applies ..."). The one-line policy note (held rules are not applied; deny wins on conflicts; unclassified fields are withheld until a rule can be evaluated) sits at the top of this card in all cases. Each entry is `<li id="<rule_id>" class="section-anchor">`
  - `rule_id` — str, element id and `<code>` label
  - `text` — str, item title (`.item__title`)
  - `kind` — str, same enum as `policy.kind`, via `status_badge(kind)`
  - `summary` — str, one paragraph
  - `version` — str, printed as "Policy version <version>"
  - `status` — str, via `status_badge(status)` in the item head (sample: `active`)
  - `confirmed_at` — str, UTC ISO-8601, printed as "Confirmed <confirmed_at>"
  - `retire_action` — str, POST target of the inline form with button `btn btn--small` "Retire"
- `held_rules` — list of dict, rendered in `<section id="held-rules" class="card">` titled "Held for review" as `ul.items`; an empty list shows a `.empty` line ("No rules are held for review."). Each entry is `<li id="<rule_id>" class="section-anchor">` and carries a fixed `status_badge("held")` because the shape has no status key
  - `rule_id` — str, element id and `<code>` label
  - `text` — str, item title
  - `reason` — str, why the rule is held, one paragraph
  - `created_at` — str, UTC ISO-8601, printed as "Held since <created_at>"
  - `clarify_action` — str, POST target of the inline form with button `btn btn--small` "Clarify"
  - `discard_action` — str, POST target of the inline form with button `btn btn--small` "Discard". Both forms sit in one `.btn-row`

Section ids: `add-rule`, `interpretation` (only when set), `active-rules`, `held-rules`, plus `<rule_id>` on each list item (rule ids must therefore be unique across active and held lists). Heading ids also present: `add-rule-heading`, `interpretation-heading`, `active-rules-heading`, `held-rules-heading`.

### alibi/app/templates/audit/list.html

Preview names: `audit` (replay_mode False), `audit_replay` (replay_mode True; the replay banner is rendered once by base.html and is not repeated here). Context builder: `ctx_audit(replay_mode)`. Inherits every base.html variable; `replay_mode` and `replay` are consumed only by base.html. Only `status_badge` is used from the imported macros; money macros are imported but unused.

- `filter` — dict, rendered as `<section id="filter" class="card">` with a GET form and submit button `btn` "Filter"
  - `action` — str, form action
  - `query` — str, prefills the text input named `q` (may be "")
  - `actor` — str, the currently selected actor. The select named `actor` has a first option "All actors" with `value=""`, selected when `filter.actor` is falsy; otherwise the option whose value equals `filter.actor` exactly is selected
  - `actors` — list of str, one `<option>` each with value and label identical
- `entries` — list of dict, rendered in `<section id="entries">` as `table.table.table--compact` inside `.table-wrap` with columns At (UTC) / Actor / Action / Request / event / Outcome / Detail; an empty list replaces the table with a `.empty` line ("No entries match this filter."). Each entry is `<tr id="<entry_id>" class="section-anchor">`
  - `entry_id` — str, row id
  - `at` — str, UTC ISO-8601, printed raw in a `.nowrap` cell
  - `actor` — str
  - `role` — str, printed after the actor in `.muted.small`
  - `action` — str, printed in `<code>`
  - `request_id` — str or `None`; None prints "—", a str prints in `<code>`
  - `event_id` — str or `None`; None prints "—", a str prints in `<code>`. Request and event ids share one cell separated by " / "
  - `outcome` — str, via `status_badge(outcome)` (sample: `ok`, `refused`)
  - `detail` — str
- `page` — dict, rendered as `<nav id="pagination" class="cluster cluster--between">`
  - `number` — int, current page (1-based), printed as "Page <number> of <count>"; the template does not clamp it
  - `size` — int, entries per page. The page count is computed in the template as `ceil(total / size)`; it falls back to 1 when `size` is 0 or when the computed count is 0 (total 0). Also printed as "(<total> entries, <size> per page)"
  - `total` — int, total entries across all pages
  - `prev_href` — str or `None`; a `btn btn--small` "Previous" link with `rel="prev"` is rendered only when not None
  - `next_href` — str or `None`; a `btn btn--small` "Next" link with `rel="next"` is rendered only when not None

Section ids: `filter`, `entries`, `pagination`, plus `<entry_id>` on each row. Heading ids also present: `filter-heading`, `entries-heading`.

### alibi/app/templates/requests/detail.html

Preview name `request_detail`, context `ctx_request_detail()`. Inherits the common base context from base.html (`page_title`, `area`, `nav_items`, `actor`, `replay_mode`, `replay`, `flashes`, `static_prefix`, `now_utc`). Sections, each a `section.card` with an id: `#rfc` (with `#rfc-questions`), `#investigation`, `#trace`, `#review` (class `card card--private`, with `#review-claims`, `#included`, `#excluded`, `#generalised`, `#gaps`, and the approval box `div.approval#approval`). Deep-link ids inside: each question `<li id="<question_id>">`; each trace step `<li id="<step_id>">`; each review claim `<li id="review-<claim_id>">`; each included, excluded and generalised item `<li id="<item_id>">`. Gaps have no id.

- `request` (dict)
  - `request_id` str
  - `parent_request_id` str | None (None prints "none")
  - `issuer` str (bank display name; also used in the page lede "From <issuer>")
  - `audience` str (recipient id, e.g. `bank_a`)
  - `subject_ref` str
  - `received_at` str, UTC ISO
  - `expires_at` str, UTC ISO
  - `period` {`from`: str UTC ISO, `to`: str UTC ISO} (printed half-open as `[from, to)`)
  - `txn_ids` list[str] (empty prints "none listed; period scope only")
  - `questions` list of {`question_id`: str, `text`: str} (ordered list)
  - `status` str, enum `new | investigating | draft | approved | sent | refused | expired` (status_badge, shown in the card head and in the kv Status row)
  - `signature` {`verified`: bool (true prints status_badge("verified"), false prints status_badge("invalid", "not verified")), `key_id`: str}
  - `href` str (present in the context, not rendered)
- `investigation` (dict)
  - `status` str, enum `not_started | running | complete | failed` (status_badge; also printed with underscores as spaces in the "Not available while the investigation is …" note when `can_run` is false)
  - `can_run` bool (false adds `disabled` to the "Run investigation" button and the muted note)
  - `run_action` str, POST URL (form has no fields)
  - `started_at` str UTC ISO | None (None prints "not started")
  - `completed_at` str UTC ISO | None (None prints "not completed")
  - `budget` {`calls_used`: int, `calls_cap`: int} (printed "<calls_used> of <calls_cap> Gemini calls")
  - `roles` list of {`name`: str, `status`: str (status_badge; sample values `skipped`, `complete`; preview_render.py pins no enum, any other value renders as a neutral badge), `calls`: int, `note`: str} (empty prints the table row "No roles have run yet.")
  - `failure_reason` str | None (kv row "Failure" only when truthy)
- `trace` list of step dicts (empty prints "No trace yet. Run the investigation to produce one.")
  - `step_id` str (`<li id>`)
  - `question_id` str
  - `question` str
  - `claim` {`claim_id`: str, `text`: str, `status`: str enum `supported | contradicted | unresolved` (claim_badge)}
  - `sources` list of {`record_id`: str, `revision`: int, `trust`: str enum `issuer_verified | bank_observed | self_supplied` (trust_badge), `href`: str (link text "<record_id> r<revision>")} (empty prints "no sources")
  - `lookups` list of {`tool`: str, `args_summary`: str, `result_summary`: str, `at`: str UTC ISO} (empty prints "no scoped lookups")
  - `calculation` {`expression`: str, `result`: {`minor`: int, `currency`: str} (money and money_raw), `computed_by`: str (printed "computed by <computed_by>"), `matches`: bool (true "matches the cited amounts", false "does not match the cited amounts")} | None (None prints "no calculation")
  - `status` str enum `supported | contradicted | unresolved` (claim_badge, shown in the step head and in the Status sub-step)
  - `status_reason` str
  - `at` str UTC ISO
- `review` (dict, private card)
  - `claims` list of {`claim_id`: str (`<li id="review-<claim_id>">`), `question_id`: str, `question`: str, `status`: claim enum (claim_badge), `rationale`: str, `href`: str (link "Open trace")} (empty prints "No claims yet.")
  - `included` list of {`item_id`: str, `question_id`: str, `label`: str, `kind`: str, `record_id`: str, `revision`: int, `trust`: trust enum (trust_badge), `reason`: str, `removable`: bool, `remove_action`: str | None} (Remove form, POST `remove_action`, no fields, rendered only when `removable` and `remove_action` are both truthy; otherwise muted "required for the claim"; empty list prints "Nothing is included.")
  - `excluded` list of {`item_id`: str, `label`: str, `reason`: str, `rule_id`: str | None (appends "Rule <rule_id>." when truthy)} (empty prints "Nothing was excluded.")
  - `generalised` list of {`item_id`: str, `label`: str, `original_summary`: str (`.diff-before`), `generalised_as`: str (`.diff-after`), `rule_id`: str} (empty prints "Nothing was generalised.")
  - `gaps` list of {`question_id`: str, `description`: str, `reason_code`: str, e.g. `evidence_unavailable` (status_badge)} (empty prints "No gaps.")
  - `counts` {`included`: int, `excluded`: int, `generalised`: int, `gaps`: int} (`.stats`)
  - `approval` (dict)
    - `status` str enum `none | pending | approved | invalidated` (status_badge)
    - `recipient` str (display name; in the sentence "Approving sends exactly this preview to <recipient>", the kv Recipient row and the button "Approve for <recipient>")
    - `audience` str (recipient id; printed in `<code>` next to the recipient and posted as hidden input `audience`)
    - `action` str, POST URL
    - `digest` str (`.digest`; hidden input `digest`)
    - `policy_version` str (hidden input `policy_version`)
    - `valid_from` str UTC ISO (`.approval__window`, "<valid_from> to <valid_until>")
    - `valid_until` str UTC ISO
    - `approved_at` str UTC ISO | None (kv row "Approved at" only when truthy)
    - `invalidated_reason` str | None (kv row "Invalidated" only when truthy)
  - `preview_href` str (link "Preview the package as the bank will see it")

Forms: POST `investigation.run_action` (no fields, button "Run investigation", `btn btn--primary`, disabled when `can_run` is false); POST `review.included[].remove_action` (no fields, button "Remove", `btn btn--danger btn--small`); POST `review.approval.action` with hidden `digest`, `audience`, `policy_version` (button "Approve for <recipient>", `btn btn--primary`).

### alibi/app/templates/requests/export.html

Preview name `export`, context `ctx_export()`. Inherits the common base context. Sections: `div.banner.banner--info.no-print#authoritative` (download link, muted print note, verify hint, print button), `section.card#package`, `section.card#approval`, `section#claims` (one `article.card` with `id="claim-<question_id>"` per claim), `section.card#attachments` (each `<tr id="<attachment_id>">`), then `div.print-only.print-footer`. The only inline script is the print button `onclick="window.print()"`.

- `package` (dict)
  - `package_id` str (page title "Package <package_id>", kv row, print footer)
  - `request_id` str
  - `version` str, e.g. `alibi-package/1` (card head and kv row)
  - `audience` str (recipient id)
  - `recipient` str (display name; page lede and kv row)
  - `case_id` str
  - `subject_ref_masked` str
  - `period` {`from`: str UTC ISO, `to`: str UTC ISO} (printed `[from, to)`)
  - `issued_at` str UTC ISO
  - `expires_at` str UTC ISO
  - `digest` str (`.digest` kv row and the print footer)
  - `signature` {`alg`: str, `key_id`: str, `domain`: str, `value`: str (`.digest`)}
  - `claims` list of (empty prints "This package carries no claims.")
    - `question_id` str (article id `claim-<question_id>`)
    - `question` str
    - `status` str enum `supported | contradicted | unresolved` (claim_badge)
    - `rationale` str
    - `evidence_refs` list of {`record_id`: str, `revision`: int, `trust`: trust enum `issuer_verified | bank_observed | self_supplied` (trust_badge)} (printed "<record_id> r<revision>"; empty prints "no evidence cited")
    - `calculation` {`expression`: str, `result`: {`minor`: int, `currency`: str}} | None (kv row "Calculation" only when truthy; expression in `<code>`, then money and money_raw)
    - `gap` str | None (kv row "Gap" only when truthy)
  - `attachments` list of (empty prints the table row "No attachments.")
    - `attachment_id` str (`<tr id>`)
    - `kind` str
    - `record_id` str (printed "<record_id> r<revision>")
    - `revision` int
    - `issuer` str
    - `trust` trust enum (trust_badge)
    - `attestation` str enum `issuer_signature | package_signer | none` (status_badge)
    - `redacted` bool (prints "yes" / "no")
    - `summary` str
- `export` (dict)
  - `json_href` str (download link; `download` attribute set to `json_filename`)
  - `json_filename` str (link text "Download <json_filename>")
  - `json_size_bytes` int (printed "(<n> bytes)")
  - `verify_hint` str (printed in `<code>` after "Verify with")
  - `approval` dict, same shape as `review.approval` on the detail page: {`status`: str enum `none | pending | approved | invalidated` (status_badge, card head and kv row), `recipient`: str, `audience`: str, `action`: str, `digest`: str, `policy_version`: str, `valid_from`: str, `valid_until`: str, `approved_at`: str UTC ISO | None (None prints "not approved"), `invalidated_reason`: str | None (kv row "Invalidated" only when truthy)}. Only `status`, `recipient`, `approved_at`, `policy_version` and `invalidated_reason` are rendered; `audience`, `action`, `digest`, `valid_from`, `valid_until` are read from the same dict but not shown on this page.

No forms. Print footer text: "This PDF is the browser's print of this page. It is a readable rendering of package <package_id>. The signed JSON (digest <digest>) is the authoritative artefact for verification."

### alibi/app/templates/requests/dispatch.html

Preview names `dispatch_success` and `dispatch_refused`, context `ctx_dispatch(outcome)`. Inherits the common base context. Page title "Dispatch result". Renders `div.result.result--success#delivered` when `dispatch.outcome == "success"` and `dispatch.receipt` is truthy; otherwise `div.result.result--refused#refused` (so a success with no receipt falls into the refused branch; the wiring session should always set `receipt` on success).

- `dispatch` (dict)
  - `outcome` str enum `success | refused`
  - `package_id` str (kv row in both branches)
  - `request_id` str (kv row in both branches)
  - `recipient` str (display name; success heading "Delivered to <recipient>", refused kv row "Recipient")
  - `attempted_at` str UTC ISO (kv row in both branches)
  - `receipt` dict | None (None on refusal; rendered only in the success branch under `h3#receipt`)
    - `receipt_id` str
    - `package_digest` str (`.digest`)
    - `received_at` str UTC ISO
    - `status` str, e.g. `accepted` (status_badge)
    - `signer_key_id` str
    - `expires_at` str UTC ISO
  - `refusal` dict | None (None on success; the refused branch renders the code/digest rows and the reason block only when truthy)
    - `code` str, e.g. `approval_digest_mismatch` (status_badge; underscores shown as spaces; no colour rule in the CSS, falls back to the neutral `.badge--status`)
    - `reason` str, printed verbatim inside `<pre class="result__reason">` under `h3#reason` "Reason from the backend (verbatim)"
    - `checked_at` str UTC ISO
    - `expected_digest` str (`.digest`)
    - `actual_digest` str (`.digest`)
  - `retry_action` str | None (refused branch only: POST form with button "Retry dispatch", no fields, rendered only when truthy)
  - `review_href` str (success: `btn btn--ghost` link "Back to the review"; refused: `btn btn--primary` link "Review and approve the revised body")
  - `history_href` str (success branch only: `btn` link "Sharing history"; read from the context but not rendered on refusal)

Forms: POST `dispatch.retry_action` (no fields, button "Retry dispatch", `btn`), refused branch only.

### alibi/app/templates/analyst/inbox.html

Preview name `analyst_inbox`, context `ctx_analyst_inbox()`. Extends `base.html` (needs the common context: `page_title`, `area="analyst"`, `nav_items`, `actor`, `replay_mode`, `replay`, `flashes`, `static_prefix`, `now_utc`). Imports `trust_badge`, `claim_badge`, `link_badge`, `status_badge`, `money`, `money_raw` (only `claim_badge` and `status_badge` are called). Sections: `#packages`, `#requests-sent`. Rows get ids `pkg-<package_id>` and `req-<request_id>`. No forms.

- `recipient` dict
  - `id` str, e.g. `"bank_a"` (shown in `<code>` in the lede and in the "only packages bound to" sentence)
  - `name` str, e.g. `"Bank A"` (page title is `"<name> inbox"`)
  - `analyst_name` str (shown in the lede; there is no top-level `analyst_name` in this context)
- `packages` list of dict (empty list renders "No packages have been received for this recipient.")
  - `package_id` str (linked to `href`)
  - `request_id` str
  - `received_at` str, UTC ISO-8601, printed raw
  - `expires_at` str, UTC ISO-8601, printed raw
  - `subject_ref_masked` str
  - `authentication` str, enum `valid | invalid | expired | wrong_audience` (rendered with `status_badge`)
  - `claim_counts` dict (each count printed after a fixed `claim_badge("supported")`, `claim_badge("contradicted")`, `claim_badge("unresolved")`)
    - `supported` int
    - `contradicted` int
    - `unresolved` int
  - `disposition` `None` or str enum `clear | escalate | request_more` (`None` prints muted "none"; otherwise `status_badge`)
  - `href` str, link target for the package id
- `requests_sent` list of dict (empty list renders "No requests have been sent from this recipient.")
  - `request_id` str
  - `parent_request_id` `None` or str (`None` prints muted "—")
  - `sent_at` str, UTC ISO-8601, printed raw
  - `status` str, e.g. `answered | awaiting_consent` (rendered with `status_badge`)
  - `question_count` int

### alibi/app/templates/analyst/package.html

Preview name `analyst_package`, context `ctx_analyst_package()`. Extends `base.html` (same common context as above). Imports the same macros (`link_badge` is imported but not called). Section ids in order: `#summary`, `#checks` (contains panels `#authentication`, `#source-trust` with items `#trust-<attachment_id>`, `#claim-status` with items `#claim-status-<question_id>`), `#evidence` (cards `#evidence-<question_id>`), `#reconciliation`, `#contradictions` (items `#contradiction-<claim_id>`), `#gaps` (items `#gap-<question_id>`), `#disposition`, `#followup`. Form control ids: `disposition-analyst-name`, `disposition-note`, `followup-period-from`, `followup-period-to`, `followup-question`.

- `recipient` dict
  - `id` str
  - `name` str
  - `analyst_name` str
- `package` dict (only these keys are read; the rest of `PACKAGE_A` is ignored)
  - `package_id` str (page title "Package <package_id>")
  - `request_id` str
  - `recipient` str, display name
  - `subject_ref_masked` str
  - `period` dict
    - `from` str, UTC ISO-8601 (inclusive)
    - `to` str, UTC ISO-8601 (exclusive)
  - `issued_at` str, UTC ISO-8601
  - `expires_at` str, UTC ISO-8601
- `authentication` dict
  - `status` str, enum `valid | invalid | expired | wrong_audience | replayed` (`status_badge`)
  - `received_at` str, UTC ISO-8601 (also used as "Received" in the summary kv, since `package` has no `received_at`)
  - `verified_at` str, UTC ISO-8601
  - `package_digest` str, e.g. `"sha256:..."` (rendered in `.digest`)
  - `signer_key_id` str
  - `checks` list of dict (empty list renders "No checks recorded.")
    - `name` str
    - `result` str, `pass | fail` (`status_badge`; any value accepted)
    - `detail` str
  - `note` str, printed verbatim under the checks table
- `source_trust` list of dict (empty list renders "No attachments in this package.")
  - `attachment_id` str
  - `record_id` str
  - `revision` int (printed as `<record_id> r<revision>`)
  - `issuer` str
  - `trust` str, enum `issuer_verified | bank_observed | self_supplied` (`trust_badge`)
  - `signature_check` str, e.g. `pass | fail | none` (`status_badge`)
  - `detail` str
- `claim_status` list of dict (empty list renders "No claims in this package.")
  - `question_id` str
  - `status` str, enum `supported | contradicted | unresolved` (`claim_badge`)
  - `summary` str
- `evidence_by_question` list of dict (empty list renders "This package answers no questions.")
  - `question_id` str
  - `question` str
  - `claim` dict
    - `status` str, enum `supported | contradicted | unresolved` (`claim_badge`)
    - `rationale` str
  - `evidence` list of dict (empty list renders "No evidence attached.")
    - `attachment_id` str (linked to `href`)
    - `record_id` str
    - `revision` int
    - `trust` str, enum `issuer_verified | bank_observed | self_supplied` (`trust_badge`)
    - `summary` str
    - `href` str
  - `calculation` `None` or dict (None renders nothing)
    - `expression` str
    - `result` money dict `{minor: int, currency: str}` (shown with `money` and `money_raw`)
    - `computed_by` str, e.g. `"verifier"` ("Recomputed by <computed_by>.")
    - `matches` bool (true -> `status_badge("pass", "matches")`, false -> `status_badge("fail", "does not match")`)
  - `gap` `None` or str (None renders nothing; empty string also renders nothing)
- `reconciliation` dict (always rendered; no empty state)
  - `title` str
  - `lines` list of dict
    - `label` str
    - `kind` str, e.g. `order | refund | fee` (printed in parentheses after the label)
    - `sign` str, `"+"` or `"-"`
    - `amount` money dict `{minor, currency}` (positive magnitude; the sign column carries the sign; shown with `money` and `money_raw`)
    - `source_ids` list of str, e.g. `["ord_1001 r1"]`
  - `expression` str
  - `result` money dict (recon__total row)
  - `compared_to` dict (recon__compare row)
    - `label` str
    - `amount` money dict
  - `difference` money dict (second recon__compare row)
  - `matches` bool (true -> `status_badge("pass", "matches")`, false -> `status_badge("fail", "does not match")`)
  - `computed_by` str
  - `computed_at` str, UTC ISO-8601 ("Recomputed by <computed_by> at <computed_at>.")
- `contradictions` list of dict (empty list renders "No contradictions recorded.")
  - `claim_id` str
  - `question_id` str
  - `description` str
  - `sources` list of dict (may be empty; then no source line is rendered)
    - `record_id` str
    - `revision` int
    - `trust` str, enum `issuer_verified | bank_observed | self_supplied` (`trust_badge`)
- `gaps` list of dict (empty list renders "No gaps recorded.")
  - `question_id` str
  - `description` str
  - `reason_code` str, e.g. `evidence_unavailable` (`status_badge`, underscores shown as spaces)
- `disposition_form` dict
  - `action` str, POST target; form fields sent: `disposition` (radio, required), `analyst_name` (text, required), `note` (textarea, optional)
  - `options` list of dict (one radio each; none preselected)
    - `value` str, enum `clear | escalate | request_more`
    - `label` str
  - `analyst_name` str, prefill for the text input
  - `current` `None` or dict (None prints "No disposition recorded yet.")
    - `disposition` str, enum `clear | escalate | request_more` (`status_badge`)
    - `analyst_name` str
    - `recorded_at` str, UTC ISO-8601
    - `note` str (empty string prints muted "none")
- `followup_form` dict
  - `action` str, POST target; form fields sent: `parent_request_id` (hidden), `period_from` (text, required), `period_to` (text, required), `question` (textarea, required)
  - `parent_request_id` str (hidden field and shown in the kv)
  - `subject_ref_masked` str, displayed read-only (not submitted)
  - `period` dict
    - `from` str, UTC ISO-8601, prefill for `period_from`
    - `to` str, UTC ISO-8601, prefill for `period_to`
  - `question_prefill` str (may be empty)
  - `consent_note` str, shown in a `banner banner--info` above the form

