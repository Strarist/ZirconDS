# ZirconDS

Multi-source real-estate property scraper. Pulls project/unit data from **100acress**, **MagicBricks**, and (when reachable) **Housing.com**, then merges into one growing archive — **one JSON object per BHK/unit**, never inventing prices, areas, or RERA.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

## Scrape

```bash
# All sources (default): 100acress + housing + magicbricks
python -m scraper --out output/properties.json

# One or more sites
python -m scraper --sites 100acress
python -m scraper --sites 100acress,magicbricks
python -m scraper --sites magicbricks --max-projects 5

# 100acress listing category
python -m scraper --sites 100acress --category residential
python -m scraper --sites 100acress --category commercial

# Optional JSONL of last-run added/updated rows
python -m scraper --out output/properties.json --jsonl output/new.jsonl
```

### Retention & merge

| File | Role |
|------|------|
| `output/properties.json` | Full **archive** (grows across runs) |
| `output/latest.json` | Rows **added or updated** in the last run |
| `output/scrape-run.json` | Run stats: discovered / skipped / scraped / added / updated |
| `output/errors.jsonl` | Per-URL failures (does not abort the batch) |

- Already-seen project URLs (`sourceUrl` / `sources[].url`) are skipped.
- Cross-source match key: `project|city|locality|bhk|superBuiltUpArea` (loose match when area is missing on one side).
- Field merge: keep existing non-null values; fill nulls from the other source; union arrays (`amenities`, `highlights`, …).
- Never overwrite a concrete price/RERA/area with null.

Housing.com often returns anti-bot **406**; the adapter logs and continues with other sources.

## Verification UI

```bash
python ui/serve.py
```

Opens http://127.0.0.1:8765/

- **Run scrape** — background job for all three sites; banner shows discovered / skipped / added / updated
- **All saved** / **Latest run** — archive vs last-run additions/updates
- **Select all** (filtered view) → **Export selected** → JSON dialog → **Copy to clipboard**
- Site chips from `sourceSite` / `sources`
- Filters, multi-word search, download pictures, source links

## Schema

Core fields follow `scraper/schema.py` (`SCHEMA_KEYS`). Unavailable fields stay `null` or `[]`.

Verification extras (outside schema): `sourceUrl`, `imageUrls`, `scrapedAt`, `sourceSite`, `sources` (`[{site, url}, …]`).

Notes:

- Unit `price` is `null` when the source says “Call for Price” (no project-level minPrice fallback).
- Plan size from 100acress `bhk_Area` maps to `superBuiltUpArea`.

## Output policy

Scraped JSON under `output/` is gitignored (except `output/.gitkeep`) so large archives are not committed.

## Notes

- Default delay between HTTP requests is ~1.2s.
- UI scrape status is also mirrored to `output/scrape-status.json`.
