# 📈 Indian Daily Stock Scanner (intraday → swing)

A beginner-friendly scanner that looks at **~150 liquid NSE stocks** every day and tells you
**which ONE stock to consider trading** the next session, with a full trade plan:

- **Entry zone** (buy at/near the open or on a dip toward the last close)
- **Stop-loss** (tight, structure + volatility based — ~2–3%)
- **Target 1 & Target 2** (1.5× and 2.6× the stop distance)
- **Position size** calculated so that if the stop-loss is hit you lose **exactly your
  chosen risk % of capital (default 1.5%, your 1–2% band)**
- **Market regime check** — tells you whether to even be long today
- **Avoid list** — stocks showing distribution / breakdown (e.g. TCS on a -4% volume dump)
- **Market context layer** — India VIX (fear gauge), top news headlines for the pick
  with risk-keyword flags, a "chase guard" when the pick already gapped/moved too much,
  and a "market closed" email on NSE holidays

---

## ⚠ Read this first

This is an **educational tool, not SEBI-registered investment advice**. It uses technical
indicators only (RSI, MACD, moving averages, ATR, volume) to rank stocks. **Nothing
guarantees a profit.** Markets can move against you; always:

- place your stop-loss the moment you enter,
- risk only what you can afford to lose (the tool keeps it to your chosen %),
- treat losses as the cost of doing business (expect roughly half your trades to lose),
- verify levels yourself and consult a SEBI-registered advisor if unsure.

---

## Install (one time)

```bash
pip install -r requirements.txt
```

Requires Python 3.9+.

## Run (every day you want a plan)

```bash
cd trade_scanner
python stock_scanner.py
```

That's it. It downloads the latest data, scans, and prints the plan, then saves:

| File | What it is |
|---|---|
| `reports/picks_YYYY-MM-DD.html` | Readable daily report (open in any browser) |
| `reports/picks_YYYY-MM-DD.json` | Same data in machine-readable form |

### Options

```bash
python stock_scanner.py --capital 200000   # your real trading capital (default ₹1,00,000)
python stock_scanner.py --risk 1.0         # risk % per trade — keep in 1–2
python stock_scanner.py --refresh          # force re-download (ignore cached data)
python stock_scanner.py --limit 30         # quick test on first 30 stocks
python stock_scanner.py --no-html          # console output only
```

### When to run

- **Recommended (automated):** **8:45 AM IST** — *before* the market opens at 9:15.
  The plan is built only on yesterday's **final settled data**, so an opening spike
  can never contaminate the entry/stop/targets. You get the email 30 min before the
  bell (this is what the GitHub Actions workflow uses: `15 3 * * 1-5` UTC).
- **Also good:** after market close (3:30 PM IST) → final data, plan for tomorrow.
- If you run while the market is open, the report prints a clear warning that prices are
  **live** and levels will shift; re-run after 3:30 PM.

Data is cached per day, so repeated runs on the same day are instant.

---

## 🌅 Automate: get the pick in your email every morning at 9:20 AM IST

**Free plan of PythonAnywhere** (cloud, always on, no PC needed) + **Gmail** = a daily
email with the pick of the day. Total setup time: ~20 minutes, one time.

### Step 0 — Prepare your Gmail App Password (one time, ~5 min)

Normal Gmail passwords don't work for scripts. Create an **App Password**:

1. Go to **https://myaccount.google.com/security** and turn ON **2-Step Verification**
   (required).
2. Go to **https://myaccount.google.com/apppasswords**.
3. App name: anything, e.g. "daily scanner" → **Create**.
4. Copy the **16-character code** (like `abcd efgh ijkl mnop`).

### Step 1 — Create a free PythonAnywhere account

1. Go to **https://www.pythonanywhere.com** → **Pricing** → **Create a Beginner account** (free).
2. Verify email, log in.

### Step 2 — Upload the scanner

1. Download `trade_scanner.zip` (from this project) to your computer.
2. On PythonAnywhere: **Files** tab → click **Upload a file** → pick the zip.
   (Or upload the folder's files individually via the web editor.)
3. In the **Bash** console (top-right menu), unzip and check:

   ```bash
   cd ~
   unzip trade_scanner.zip -d .
   ls trade_scanner
   ```

### Step 3 — Install the libraries

In the Bash console:

```bash
cd ~/trade_scanner
python3 -m pip install --user -r requirements.txt
```

### Step 4 — Enter your email settings

In the **Files** tab, open `trade_scanner/settings.json` and fill in:

```json
{
  "gmail_user": "youremail@gmail.com",
  "gmail_app_password": "PASTE_16_CHAR_CODE_HERE",
  "to_email": "youremail@gmail.com",
  "capital": 100000,
  "risk_pct": 1.5
}
```

> 🛡️ Keep this file private — it contains your Gmail App Password. Do not share it or
> upload it to any public site.

### Step 5 — Test it (very important)

In the Bash console:

```bash
cd ~/trade_scanner
python3 setup_check.py            # environment check - tells you what to fix
python3 daily_email.py --test-mail # sends a quick test email
python3 daily_email.py --dry-run   # runs the scan, prints the email (no send)
python3 daily_email.py             # full run: scan + real email
```

Check your Gmail for the test email. If it fails, run `setup_check.py` and fix what
it flags — 99% of the time it's the App Password (spaces, wrong code, or 2-Step
Verification not enabled).

### Step 6 — Schedule it for 9:20 AM IST daily

1. Click **Tasks** tab → **Add a new scheduled task**.
2. Set the time to **`03:50`** — PythonAnywhere tasks run in **UTC**, and
   **9:20 AM IST = 03:50 UTC** (IST is UTC+5:30).
3. Command (one line):

   ```bash
   python3 /home/YOUR_USERNAME/trade_scanner/daily_email.py
   ```

   Replace `YOUR_USERNAME` with your PythonAnywhere username.
4. **Save.** Done — the pick email arrives every trading morning at 9:20 AM IST.
   (Free plan allows 1 daily task — that's exactly this one.)

### Checking it works / troubleshooting

- Every run writes `trade_scanner/daily_email.log` — check it in the **Files** tab.
- If a morning's email says *"data unavailable"*: it's usually a temporary Yahoo hiccup.
  Re-run manually with `python3 daily_email.py`. If it keeps failing for days, use the
  **GitHub Actions** alternative below (Yahoo blocks some cloud IPs from time to time).
- The free plan limits internet access to an allowlist — **`.yahoo.com` and Gmail SMTP
  are both on it**, so the scanner works there. (Paid plans remove all limits if you
  ever need more.)

### Alternative hosts (if you ever want them)

- **Windows Task Scheduler (your own PC):** install Python + `pip install -r
  requirements.txt`, fill `settings.json`, then run
  `python daily_email.py` via Task Scheduler at 09:20 daily (PC must be on).
- **GitHub Actions (free cloud):** commit this folder to a GitHub repo and add a
  workflow with cron `50 3 * * *` (UTC) that runs `daily_email.py`; store
  `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `TO_EMAIL` as repo secrets. Ask me and I'll
  generate the workflow file for you.

---

## How the scoring works (transparent, no black box)

Every stock gets a score out of 100 from five blocks, then penalties:

| Block | Max | What it measures |
|---|---|---|
| Trend | 35 | Price above EMA21 / SMA50, EMA21 > SMA50, positive 20-day return |
| Momentum | 25 | RSI sweet spot (52–68), MACD positive, healthy 5-day push |
| Volume | 15 | 5-day volume above 20-day, accumulation, today's activity |
| Volatility fit | 10 | ATR 1.5–4.5% of price — enough movement, not too wild |
| Setup | 15 | Near 52-week/20-day highs = breakout zone |
| **Penalties** | − | Overbought RSI >78, extended >2.5 ATR, big red candle, -2% drop on heavy volume, gap-up chase |

Only liquid names pass the gate first: **20-day average traded value ≥ ₹10 crore** and
**≥ 3 lakh shares/day**, ATR between 1% and 9% of price.

### Improved pick filter (on by default)

On top of the base gates, the pick of the day must also pass a **success-probability
filter** (default ≥ 28%), an **RSI sweet-spot band** (default 50–65), and a **weekly RSI
trend filter** (weekly RSI-14 ≥ 50, measured to add a small real gain). These are the
measured improvements from a 2-year backtest of this exact strategy:

| | Baseline | Improved |
|---|---|---|
| Win rate (hit T1) | 34.2% | **~38.6%** |
| Avg per trade | +0.154R | **~+0.229R** |
| Profit factor | 1.35 | **~1.52** |

It trades less often (~189 vs 237 signals/2y) but each trade is better. When nothing
passes, the tool says **"stay in cash"** — that's the filter working, not a bug.
Tune it with `--min-prob 0.30`, `--rsi-min 45`, `--rsi-max 70`, `--weekly-rsi-min 45`,
or disable the weekly filter with `--no-weekly-filter`.

### Re-pick cooldown (stops repeated picks of the same stock)

The scanner **remembers its recent picks** (`picks_history.json`, committed back to the
repo each day by the GitHub workflow) and **skips any stock picked in the last 7
sessions**. This was added after real feedback and confirmed by the 2-year backtest:

| | First-time pick | Re-pick within 10 sessions |
|---|---|---|
| T1 hit rate | 41.4% | 26.8% |
| Avg per trade | +0.278R | **−0.068R** |

Re-picking a stock that already ran once **loses money on average** — so the cooldown
lifts the overall results (PF 1.46 → 1.55). If a stock you liked was skipped, the
report tells you explicitly ("🚫 Cooldown: 1 higher-scoring candidate skipped").
Disable with `--no-cooldown` or change the window with `--cooldown 10`.

> Honesty note: other filters were tested and **not** added because they HURT results
> (e.g. "daily close above 200-DMA" dropped avg/trade to +0.162R) — the gains here are
> real but modest; ~38% is roughly the ceiling for a daily momentum pick.

## How to trade the pick (the 30-second checklist)

1. Check the **regime chip** at the top. Bearish → skip trading that day.
2. Trade only the **top 1–3** picks; don't overtrade.
3. Enter at the **open or on a dip toward the entry zone**; never chase a +3% gap.
4. Place the **stop-loss immediately**. Getting stopped = losing ~1.5% of capital = the plan working.
5. Book half at **Target 1**, trail the rest toward **Target 2**.
6. Holding window: **1 day to ~1 week**. No progress in 5 sessions → exit.
7. Keep **2–3 positions max** total.

## What it deliberately does NOT do

- **No news-driven scoring.** Headlines and VIX are *context warnings only* — the score
  stays 100% technical (price, volume, RSI, MACD, ATR). It does not "understand"
  geopolitics; it flags, and you verify.
- No fundamentals (earnings, ratios, FII/DII flows) — technical momentum screen only.
- No guarantees, no "sure shot". Some days it will tell you **"stay in cash"** — that's a win.
- Not built for penny stocks, options, or F&O. Cash-equity swings only.

---

## Project layout

```
trade_scanner/
├── stock_scanner.py      # main engine (fetch → analyze → score → report)
├── daily_email.py        # automation: run scan + email pick of the day
├── setup_check.py        # environment checker (run first on PythonAnywhere)
├── SETUP_GUIDE.md        # click-by-click setup walkthrough (steps 3–7)
├── watchlist.py          # ~150 NSE symbols (NIFTY50 + Next50 + liquid mid/large caps)
├── settings.json         # ← fill in your Gmail details here (App Password)
├── requirements.txt
├── README.md
├── cache/                # per-day downloaded data (auto-managed)
├── reports/              # daily HTML + JSON reports
└── daily_email.log       # automation log (created on first run)
```

### Want a different watchlist?

Edit `watchlist.py` — add/remove `("SYMBOL.NS", "Company Name")` tuples. Symbols that
fail to download are skipped automatically, so a typo never breaks the run.

---
*Made for a beginner with a 1–2% risk budget and a 1-day-to-1-week holding window.*
