# docs/

Everything that isn't code. The repo root now holds only entry points.

| Folder | What lives here |
|---|---|
| `academic/` | Major Project submission — abstract, guide acceptance letter, project report |
| `business/` | Broker outreach — Fyers pitch decks, contact list, outreach plan, email drafts |
| `reports/` | Analysis write-ups: competitive positioning, the scraping + model build guide |
| `reference/` | Working notes — rules reference, model-training notes |
| `DEPLOY_AWS.md` | AWS Lightsail deployment runbook |

Regenerate a report PDF after editing its `.txt`:

```bash
python scripts/txt2pdf.py docs/reports/COMPETITIVE_EDGE.txt
```

## Still at the root, deliberately

- **`PROJECT.txt`** — the authoritative full spec. Referenced by code; read it first.
- **`plan.txt`** — the phased build roadmap. Referenced in four places.
- **`README.md`** — setup and run instructions.

## Note on version control

The root `.gitignore` excludes `*.txt`, `*.pdf`, `*.pptx` and `*.docx`, so
nothing in this folder — including `PROJECT.txt` and `plan.txt` — is tracked by
git. That is fine for the pitch decks and the academic paperwork; it does mean
the spec and the roadmap have no version history. If you want them tracked, add
a negation to `.gitignore`:

```
!PROJECT.txt
!plan.txt
!docs/**/*.txt
```
