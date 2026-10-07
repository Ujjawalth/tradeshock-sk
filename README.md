# TradeShock SK

**An AI stress-tester that shows a Saskatchewan grain farm what a trade headline does to its crop plan, before seed is bought.**

Built solo for the SCAI AI Open Saskatoon 2026 (72-hour build, Oct 5–8, 2026).

> Planning aid, not financial advice.

## The problem

Saskatchewan farm income is exposed to sudden trade shocks, such as tariffs on canola, canola meal and peas. Some tariff relief is time-limited. Farmers plan next year's crop mix in winter, usually with static spreadsheets that can't answer "what if this headline is real?", "how bad could a bad year get?" or "which plan holds up if I'm wrong?"

## What it does

**Core flow**
1. **Farm profile.** Pick a soil zone (Brown / Dark Brown / Black), total acres, which crops you can grow, and the rotation limits. The limits are editable and labelled as assumptions.
2. **Baseline plan.** A linear-programming optimizer (PuLP/CBC) picks the crop mix with the highest return over variable costs, using Crop Planning Guide budgets for that zone. It also shows each crop's break-even price.
3. **Headline → scenario (AI).** Paste a headline. Claude returns strict JSON with, for each affected crop:
   - a base price change %, plus a **bear/bull range**
   - confidence and reasoning
   - duration and assumptions

   Each crop gets an editable slider, so the farmer has the final say.
4. **Shock plan.** Compares three plans:
   - the baseline
   - the old plan kept at the new prices
   - the re-planned mix

   The difference between the last two is the **value of re-planning**.
5. **Explain (AI).** A plain-English summary. A **number guard** rejects any AI text containing a number that didn't come from the optimizer.

**Advanced features**
- **⚡ Trade Watch (automation).** Scans official Government of Canada news feeds (Agriculture and Agri-Food Canada, Finance Canada):
  - Claude triages each item: trade-related or not.
  - Every relevant item is automatically stress-tested against your farm and **ranked by dollar impact**.
  - A schedulable script (`scripts/watch_scan.py`) produces the same digest unattended.
- **Ask TradeShock (AI agent).** Plain-English what-ifs, e.g. *"What if peas drop 20% and I cap canola at 25%?"*:
  - Claude calls the optimizer and risk engine as **tools** (`what_if`, `price_sweep`, `risk_report`, `crop_budgets`). It never does the math itself.
  - Answers pass the number guard.
  - "Apply this plan" pushes the agent's proposal into the sliders.
- **Risk lens (real price and yield history).** Replays your plan through every combination of:
  - **140 real 12-month Saskatchewan price moves** (Statistics Canada, 2013–2026)
  - **35 real detrended yield years** (1991–2025, including the 2002 and 2021 droughts)

  That's 4,900 scenarios. The lens shows:
  - the expected return and a **bad year** (average of the worst 10%)
  - the worst historical replay (e.g. "prices Jun 2022→Jun 2023 + 2021 yields")
  - the chance that returns fall below variable costs

  A **mean-CVaR linear program** finds a risk-aware mix, with a slider for how much you care about bad years. Yield risk can be switched off.
- **Bear / base / bull.** Scores five plans in every case, with **regret**:
  - the best plan for each case (bear, base, bull)
  - a max-min plan, which protects the worst case
  - a **minimax-regret** plan

  The ★ plan is never far behind, whichever case happens.
- **Stress sweep.** Re-optimizes the farm from −60% to +60% for one crop and marks the **tipping points** where the best mix changes.
- **Other farm income (AI extraction).** Upload a SYNTHETIC surface lease (.txt or .pdf):
  - Claude extracts the annual payment and the next compensation review date, each with a **verbatim quote**.
  - A value is accepted only if its quote is in the document and matches the value.
  - The income is added to farm totals, with a warning when the review is due soon.
- **1-page PDF export.** A print-ready plan summary (browser "Save as PDF").
- **Share links and saved farm.** "Copy share link" encodes the farm and scenario in the URL, so judges can open the exact case on their phone. Your farm profile is remembered on this device.
- **Offline-first demo.** Presets, the sample news feed, summaries, the agent's rule-based parser and charts all work with Wi-Fi off. Chart.js has a local fallback.

## Data sources

| Data | Source | Status |
|---|---|---|
| Crop budgets (yield, price, variable and total cost per acre, by soil zone) | Saskatchewan Ministry of Agriculture **Crop Planning Guide 2026** (official PDF downloaded manually into `data/raw/`). `scripts/extract_guide.py` gives every row a `source_page`. | **Verified:** 23 rows (8 crops × 3 zones; the guide has no Durum budget for the Black zone). Every row passed the guide's own arithmetic (yield × price = gross revenue; gross − variable = return over variable expenses). |
| Price risk | **Statistics Canada Table 32-10-0077-01**, *Farm product prices, crops and livestock*, Saskatchewan, monthly. Open Government Licence – Canada. Fitted by `scripts/fit_price_model.py` into `data/price_model.json`. | Real data: 140 joint 12-month windows, 2013-08 → 2026-07. Pulses use class-level series (all dry peas, all lentils). |
| Yield risk | **Statistics Canada Table 32-10-0359-01**, *Estimated areas, yield, production…of principal field crops*, Saskatchewan, annual. Open Government Licence – Canada. Fitted by `scripts/fit_yield_model.py` into `data/yield_model.json`. | Real data: 35 years, 1991–2025. Linear trend removed per crop. Provincial averages (they understate single-farm swings). |
| Trade Watch feeds | Government of Canada news API (Atom): Agriculture and Agri-Food Canada, Department of Finance Canada | Live, official only. Links restricted to canada.ca. |
| Demo headlines / sample feed | Hand-written, labelled **HYPOTHETICAL** | Not news, no tariff rates. |

## How AI is used

| Feature | Model input | Model output | Safety |
|---|---|---|---|
| Headline → scenario | Headline (untrusted, ≤600 chars, inside `<headline>` tags) plus the crop list | Structured-output JSON schema; crop names as an enum; base, low and high % | Pydantic validation, alias mapping, unknown crops dropped, clamped to ±60%, range ordered. Non-trade → friendly message. |
| Trade Watch triage | Feed title + summary (untrusted) | Same scenario schema | Keyword pre-filter, max 6 AI calls per scan, results cached per headline |
| Explain | Optimizer facts only | 90–150 word summary | Number guard → deterministic fallback |
| Ask TradeShock agent | Question + current farm state | Tool calls (`what_if`, `price_sweep`, `bear_base_bull`, `risk_report`, `crop_budgets`) → answer | Tools run the LP; number guard with one corrective retry; offline rule parser |
| Lease extraction | SYNTHETIC lease text (untrusted, inside `<document>` tags) | JSON with values + verbatim quotes | Quote must be in the document and match the value, else regex fallback |

- Each AI call logs the prompt version, model, latency and token use.
- The model is set by `ANTHROPIC_MODEL` (default `claude-opus-5-5`). All provider code is in `ai.py`, and the agent loop is in `agent.py`.
- Server-side refusal fallbacks are on by default (`ANTHROPIC_USE_FALLBACKS=0` turns them off).
- AI endpoints are rate-limited per IP (`AI_RATE_LIMIT_PER_MIN`, default 10). The client IP comes from the proxy's own hop, so faked `X-Forwarded-For` headers can't dodge it. There's also a daily cap (`AI_DAILY_CALL_LIMIT`).
- **Admin lock:** if `AI_ACCESS_PASSWORD` is set (12+ characters), live AI only runs for browsers unlocked with that password. Everyone else gets the full offline app.
  - Constant-time password check.
  - 5 attempts per 15 minutes per IP.
  - Signed 12-hour HttpOnly / Secure / SameSite=Strict cookie.
  - Fails closed if the password is weak.
  - Changing the password revokes every session.

## Run locally

```powershell
python -m venv venv
venv\Scripts\activate              # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env             # then put your ANTHROPIC_API_KEY in .env
python app.py                      # http://127.0.0.1:5000
```

- `http://127.0.0.1:5000/?demo=canola-duties` auto-runs a preset, which is handy for a live pitch.
- Tests: `python -m pytest -q`
- Optimizer CLI: `python optimizer.py --zone "Dark Brown" --acres 2000 --shock "Canola=-20"`
- Trade Watch digest: `python scripts/watch_scan.py` (add `--sample` for offline)

## Refresh data

```powershell
# Crop Planning Guide (official PDF in data/raw/)
python scripts/extract_guide.py inspect data/raw/<guide>.pdf
python scripts/extract_guide.py extract data/raw/<guide>.pdf
python scripts/extract_guide.py template ; python scripts/extract_guide.py from-csv data/budgets_manual.csv   # hand-entry fallback

# Risk models (StatCan CSVs in data/raw/statcan_32100077/ and data/raw/statcan_32100359/)
python scripts/fit_price_model.py
python scripts/fit_yield_model.py

# Cached AI output for presets (needs API key)
python scripts/build_demo_cache.py
```

## Deploy (Render)

1. Push to GitHub.
2. On Render, create a **New → Blueprint** and pick the repo. It uses `render.yaml`.
3. Set `ANTHROPIC_API_KEY` in the dashboard. Never commit it.
4. Optional: add a **Cron Job** running `python scripts/watch_scan.py` for scheduled Trade Watch scans.

## Project layout

```
app.py              Flask routes, rate limit, cache-first presets
optimizer.py        LP: baseline, shock, stand-still, sweep, max-min + minimax-regret
risk.py             Historical price replay + mean-CVaR risk-aware LP
agent.py            Ask TradeShock: Claude tool-use loop + offline parser
watch.py            Trade Watch: official feeds -> AI triage -> ranked $ impact
lease.py            Other farm income: lease extraction with quote verification
ai.py               Claude calls, logging, number guard
models.py           pydantic schemas + scenario repair/clamping
data/               budgets, price_model.json, demo_cache.json, watch_sample.json, raw/ (git-ignored)
scripts/            extract_guide.py, fit_price_model.py, fit_yield_model.py, build_demo_cache.py, watch_scan.py
templates/, static/ single mobile-friendly page (vanilla JS, Chart.js)
tests/              pytest: optimizer, risk, agent, watch, validation, API
```

## Limits and honest caveats

- The risk lens treats price and yield swings as **independent**. That's conservative, since bad harvests often lift prices.
- Yield swings are provincial averages, which understate a single farm's risk. Crop insurance and AgriStability aren't modelled.
- For speed, the risk-aware LP is solved on an evenly spaced 1,200-scenario subset of the 4,900 combinations. All results shown are scored on the full set.
- Historical price windows overlap, so the scenarios aren't independent. Pulse prices and yields are class-level series.
- The optimizer maximizes return over variable costs. Fixed costs are reported but don't change the mix.
- Rotation limits are simple percentage caps, not a multi-year rotation model.
- AI scenarios are planning estimates, not forecasts. The sliders exist so a human decides.
