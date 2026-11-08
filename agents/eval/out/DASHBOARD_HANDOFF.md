# Alibi dashboard cards

`dashboard-cards.patch` is a proposal for the research-owned site. It changes `PassportHome.astro` and adds `DashboardSection.astro`. Both Home routes use the same component. The dashboard previews recent activity, banks, merchants and the newest transaction. The four dialogs retain the full lists, bank links and financial timeline.

The cards use Alibi’s existing type and colours. Desktop has two columns, and phone layouts stack the cards and open dialogs as bottom sheets. A card opens with a click or Enter. Close and Escape restore focus. The most recent transaction keeps its date, amount and evidence status. If the backend reports a link and the related details are missing, the preview says those details are unavailable.

Research can apply the proposal from the repo root after comparing it with the chosen visual design.

```bash
git apply --check --whitespace=error agents/eval/out/dashboard-cards.patch
git apply agents/eval/out/dashboard-cards.patch
(cd web && pnpm verify)
```

The patch was checked against the owner’s current source. In a disposable copy, `pnpm verify` passed all 119 tests and built all three apps. Chromium opened each dialog on `/app/` and `/simulate/` at 1440×900 and 390×844. Checks covered whole-card clicking, keyboard opening, initial focus, both close paths, restored focus, phone bottom-sheet bounds, full transaction history and simulation request links. The browser used synthetic Case 1 and local demo sign-in.
