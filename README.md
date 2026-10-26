# Cart Commute: New Haven grocery trip planner

Tell it where you live in New Haven and what you need. It tells you **which day, which store, and how to get there** (walk, the free Yale Weekend Grocery Shuttle, Uber, or Zipcar) for the lowest real cost. It counts this week's sale prices, sale end dates, ride costs, store hours, weather, and the value of your time.

Four AI agents (List Reader, Price Scout, Trip Advisor, and a Plan Helper chat) work with real tools: weekly flyer search, address lookup, routing, and a 7-day forecast. Python does all the math. **No price is ever made up.** Every number shows where it came from: *Sale* (this week's flyer), *Website* (store website), or *Estimate* (U.S. average or ride estimate, with its basis).

All agents use `gpt-6-luna` through Portkey with `PORTKEY_API_KEY`.

## Run it (about 3 minutes)

You need **Python 3.11 or newer** and an internet connection.

**1. Get the code and open the folder**

```bash
git clone https://github.com/yguru00-cloud/final-project.git
```

```bash
cd final-project
```

**2. Create a virtual environment and install** (5 small packages)

Windows (PowerShell):

```bash
python -m venv .venv
```

```bash
.venv\Scripts\activate
```

Mac / Linux:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

Then, on any system:

```bash
pip install -r requirements.txt
```

**3. The API key**

The app looks for `.env` **in this folder first, then in the folder above it**. If your `.env` with `PORTKEY_API_KEY` already sits one folder up, there is nothing to do. Otherwise copy `.env.example` to `.env` and fill in the key. Never commit `.env`.

**4. Start it**

```bash
python app.py
```

Open **http://localhost:8000**. Type an address (or click `165 Whitney Ave`), add some items (or click the Quick add chips), and press **Plan my trip**. A plan takes about 20-30 seconds. In the Basket tab you can change an amount with − / + or pick another matching product; the plan is re-ranked instantly by code (no AI call). If other versions of an item were found (lactose-free milk, instant rice, bone-in chicken...), an **Other versions found** panel shows them as choices A / B / C: A is the regular product in your plan, and every other choice shows what the plan's total would become before you pick it. The result card also tells you at what value of your time a faster or cheaper plan would win (break-even $/hour). Press **Ask** (bottom right) to chat about the plan.

If port 8000 is busy, set another one first: `set PORT=8010` (Windows) or `export PORT=8010` (Mac/Linux).

## Offline / test mode

Sales, routes and weather are fetched live and saved in `data/cache/`. If a live service is down, the saved copy is used automatically and marked "saved copy" in the app. To use **only** the saved copy (no lookups), add `USE_LIVE_DATA=0` to `.env`. The saved copy includes the demo address `165 Whitney Ave` and common items (eggs, milk, chicken breast, rice, bananas, coffee, bread, ground beef). The model calls still need the key.

## Safety tests (no key needed)

```bash
python tests/test_safety.py
```

```bash
python tests/test_units.py
```

The first checks that injection text is flagged, invented prices are caught, fake offer ids are rejected, unknown items stay "missing", the budget cap stops a runaway run, and the rate limit works. The second checks the unit conversion (dozens, gallons, pounds, multipacks, bananas by the piece or by the pound).

## How it works

```
shopping list + address
   │
   ├─ geocode (US Census / OpenStreetMap) ── outside New Haven? → refused
   ├─ List Reader agent ── list → items, amounts, search words (ignores instructions in the list)
   ├─ search tools ── this week's flyers (Flipp, ZIP 06511) + Trader Joe's/Costco website prices
   ├─ Price Scout agent ── judges which offers really are the item (ids checked against tool results)
   ├─ unit conversion (Python) ── how many of each product to buy for the amount asked, e.g. 2 dozen eggs = 1 pack of 24
   ├─ routes (OSRM) + weather (Open-Meteo) + store hours (OpenStreetMap)
   ├─ optimizer (Python) ── every day × store (or 2 stores) × walk/Uber/Zipcar, all costs added up
   └─ Trip Advisor agent ── explains the best plan; every $ amount is checked against the data
        └─ Plan Helper chat ── answers follow-up questions with tools, same price check
```

| Folder / file | What it is |
|---|---|
| `app.py` | FastAPI app: serves the page and the API (`/api/plan` streams live progress) |
| `backend/agents.py` | The agents and the planning pipeline |
| `backend/data_sources.py` | Flyer search, website prices, geocoding, routes, weather (live + saved copy) |
| `backend/optimizer.py` | All arithmetic: basket per store and day, ride costs, ranking |
| `backend/units.py` | Reads product sizes and converts amounts, so every store is compared on the same amount |
| `backend/safety.py` | Input screen, price grounding check, spend meter, rate limit |
| `backend/prompts/` | One instruction file per agent |
| `data/` | Stores, ride rates (with sources), BLS reference prices, website prices, saved cache |
| `static/` | The web page (HTML, CSS, JS) |
| `tests/test_safety.py` | Offline safety tests |
| `output/audit_trail.jsonl` | Every agent step, tool call and guard decision (created when you run it) |

## Where the numbers come from

- **Sale prices:** this week's store flyers via Flipp's public flyer search for ZIP 06511 (Stop & Shop, ALDI, Big Y, Walmart, Target, BJ's, Costco online deals).
- **Website prices:** 343 everyday prices for 15 staples, read by hand from screenshots on 2026-10-25 and 2026-10-26 (`data/base_prices.json`, built by `tools/build_base_prices.py`):
  - Trader Joe's (traderjoes.com)
  - ALDI (aldi.us, 2300 Dixwell Ave, Hamden store)
  - Stop & Shop (stopandshop.com, 150 Whalley Ave store)
  - ShopRite (shoprite.com, 2100 Dixwell Ave, Hamden store)
  - Walmart (walmart.com, 2300 Dixwell Ave, Hamden store; no fresh meat or produce online there)
  - Target (target.com, North Haven store)
  - BJ's Wholesale Club (bjs.com, North Haven club; planned only if you tick "BJ's member")
  - Costco Same-Day (Instacart), whose prices are usually higher than in the warehouse

  For meat sold by weight, the per-lb price shown on the site is used. In-store prices may differ.
- **Estimates:** when no store price exists, the U.S. average retail price from the Bureau of Labor Statistics (September 2026) is used and labeled *Estimate*. Uber cost = $7.50 + $1.25/mile (minimum $8) each way, fitted to Uber's own published average fares from New Haven. Zipcar = $11/hour by default (you can change it in Trip settings).
- **Yale Weekend Grocery Shuttle:** Saturdays and Sundays, about 7am-5pm, stops at ALDI/Walmart Hamden and ShopRite / Trader Joe's Hamden (Yale's official route sheet; stop locations from Yale's shuttle tracker). Yale does not publish the loop time; it is about 50 minutes, as observed once in Yale's shuttle tracker app, and is split by distance along the one-way loop. Going out, you watch the tracker app (about 5 min wait); coming back, you wait for the same shuttle to come around again (loop time minus your walking and shopping time). No service on University holidays or recess.
- **Store locations and hours:** OpenStreetMap. **Weather:** Open-Meteo. **Busy hours:** a simple rule (weekend midday, weekday 5-6pm), not live data.

## Known limits

- Website prices are a snapshot from 2026-10-25/26 for 15 staples at 8 stores (Big Y only has flyer sales). Items outside that list fall back to this week's flyer or, last, a labeled U.S.-average estimate.
- Costco prices come from Costco Same-Day (Instacart), which is usually more expensive than the warehouse, so Costco plans are slightly overpriced.
- Uber and Zipcar costs are estimates; surge pricing is not included.
- Store crowding is a rule of thumb, not live data.
