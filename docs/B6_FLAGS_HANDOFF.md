# B6 → A1 hand-off: research flags for auto-pause

B6's nightly re-validation (`app/research/revalidate.py`, off by default — Algo Lab → Research)
re-checks every switched-on strategy each weekday evening and opens a **flag** when its edge no
longer holds. It never switches anything off itself: the live desk's auto-pause decides.

## Reading the flags

- In process (the runner / auto-pause):
  ```python
  from app.db import session as dbs
  from app.research.revalidate import active_flags
  with dbs.SessionLocal() as db:
      open_ = active_flags(db, strategy_id)       # [] = nothing against it
  ```
- Over HTTP: `GET /api/algo/research/flags?strategy_id=12` (open only; `active=false` for history).

A flag (`algo_research_flags`): `strategy_id`, `reason` (plain text), `card_id` (the nightly card
with the evidence), `detail.since_card` (trades / net since its passing card), `created_at`,
`cleared_at` / `cleared_note`. One open flag per strategy at a time.

## Why a strategy is flagged

1. The research check on its card's window rolled to today is **killed** (DSR, walk-forward,
   held-out or PBO below the bar).
2. It **lost money since its passing card** over at least that card's minimum trades — data the
   card never saw.
3. It trades **LIVE without ever passing** a research check (rule 6).

New flags are also published on `system.error` (`where: "research nightly re-validation"`), so
the Telegram / Discord channels that take errors hear about them.

## Suggested use in A1

Treat an open flag like the drift / drawdown limits: pause the strategy (event + Telegram), and
let the person who clears the flag (`POST /api/algo/research/flags/{id}/clear {note}`) decide
whether it comes back.
