# Drop-in files for `web/apps/merchant` (data)

These are staged here because `team.json` cannot change while lead is out (research, post 6717263436). Copy them into `web/apps/merchant/` at the same relative paths, then run `cd web && pnpm format && pnpm verify`. Data has no Node toolchain on this machine, so **none of these files has been formatted, type-checked or built yet**. Please send back any failures.

| Staged path | What it is |
| --- | --- |
| `src/data/account.ts` | Maya's seller account as Mock Marketplace shows it: 8 orders, 2 refunds, fees, payout `PO-0001-01`, the "Funds available" message and her own purchase. Every value is generated from `agents/data/out/merchant_view/case_0001.json`, which is itself built from case 1. Also adds `day()` and `when()` UTC date formatters. |
| `tests/account.test.ts` | Vitest: `account.ts` must equal data's marketplace view, and the totals must add up to 216000 pence. The relative path to `agents/` assumes the file lives in `web/apps/merchant/tests/`. |
| `src/components/SellerNav.astro` | Tab bar for the seller hub. |
| `src/pages/seller/index.astro` | Seller hub: October totals (2,400 − 180 − 60 = 2,160), latest payout (paid to bank on 27 Oct, 16:00 UTC), recent orders, messages. |
| `src/pages/seller/orders/index.astro` | All orders and refunds. Rows stack at phone width using `data-label`. |
| `src/pages/seller/payouts/index.astro` | `PO-0001-01` and what it covers, plus one line explaining that a release to the seller balance is not a bank payout. |
| `src/pages/seller/messages/index.astro` | The balance-release message (`rec-0001-020`) the demo turns on. |

**One change to a file data does not own** (`src/layouts/ShopLayout.astro`, research's): add `{ href: '/seller/', label: 'Seller hub' },` to `nav`, before Account. Data has not edited that file.

**Connect** is unchanged. The existing "Connect to my Alibi" button on `/account/settings/` stays as it is. The merchant site makes no backend call and never touches `/merchant/admin/seed`. Showing a live connection state waits for the route table in `alibi/RUNBOOK.md`.

Everything here is synthetic and comes from case 1.
