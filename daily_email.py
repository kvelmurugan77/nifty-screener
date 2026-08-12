#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_email.py — run the stock scanner and email the pick of the day.

Works in two environments:
  1. GitHub Actions (recommended, free cloud): credentials come from environment
     variables (GMAIL_USER, GMAIL_APP_PASSWORD, TO_EMAIL, CAPITAL, RISK_PCT),
     which GitHub injects from your repository Secrets.
  2. Your own PC: also works with the same env vars, or fill settings.json.

USAGE
  python daily_email.py              # scan + send email
  python daily_email.py --dry-run    # scan + print email (no sending)
  python daily_email.py --test-mail  # send a test email (no scan)

NOTE: educational tool, not investment advice.
"""

import argparse
import datetime as dt
import json
import os
import smtplib
import ssl
import subprocess
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
SCANNER = os.path.join(HERE, "stock_scanner.py")
REPORT_DIR = os.path.join(HERE, "reports")
LOG_FILE = os.path.join(HERE, "daily_email.log")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def ist_now():
    return dt.datetime.now(IST)


def log(msg):
    line = f"[{ist_now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------
# Settings: environment variables first, settings.json as fallback
# ----------------------------------------------------------------------------
def load_settings():
    cfg = {}
    settings_path = os.path.join(HERE, "settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:  # noqa: BLE001
            pass

    gmail_user = os.environ.get("GMAIL_USER") or cfg.get("gmail_user", "").strip()
    app_pass = os.environ.get("GMAIL_APP_PASSWORD") or cfg.get("gmail_app_password", "").strip()
    to_email = os.environ.get("TO_EMAIL") or cfg.get("to_email", "").strip()
    try:
        capital = float(os.environ.get("CAPITAL") or cfg.get("capital", 100000))
    except (TypeError, ValueError):
        capital = 100000
    try:
        risk = float(os.environ.get("RISK_PCT") or cfg.get("risk_pct", 1.5))
    except (TypeError, ValueError):
        risk = 1.5
    return gmail_user, app_pass, to_email, capital, risk


# ----------------------------------------------------------------------------
# Run the scanner
# ----------------------------------------------------------------------------
def run_scan(capital, risk):
    """Run stock_scanner.py, return the parsed JSON report (or None)."""
    date = ist_now().strftime("%Y-%m-%d")
    json_path = os.path.join(REPORT_DIR, f"picks_{date}.json")

    # If today's report already exists AND the market is closed, reuse it
    now = ist_now()
    market_open = 9 * 60 + 15 <= now.hour * 60 + now.minute <= 15 * 60 + 30
    if os.path.exists(json_path) and not market_open:
        try:
            with open(json_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            pass

    log("running stock scanner ...")
    cmd = [sys.executable, SCANNER, "--capital", str(capital), "--risk", str(risk)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE, timeout=900)
        log(f"scanner finished with exit code {proc.returncode}")
        tail = "\n".join((proc.stdout or "").splitlines()[-12:])
        if tail:
            log("scanner output (tail):\n" + tail)
    except Exception as e:  # noqa: BLE001
        log(f"scanner crashed: {e}")
        return None

    if not os.path.exists(json_path):
        log("no JSON report produced — scan failed")
        return None
    try:
        with open(json_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        log(f"could not read JSON report: {e}")
        return None


# ----------------------------------------------------------------------------
# Compose the email
# ----------------------------------------------------------------------------
def inr(x):
    try:
        x = int(round(float(x)))
    except (TypeError, ValueError):
        return "-"
    s = str(abs(x))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return ("-" if x < 0 else "") + s


def compose(data, capital, risk):
    meta = data.get("meta", {})
    date = meta.get("date", ist_now().strftime("%Y-%m-%d"))
    market_open = meta.get("market_open", False)
    regime = data.get("regime", "Unknown")
    picks = data.get("picks", [])

    lines = []
    lines.append("==============================================")
    lines.append("  INDIAN DAILY STOCK SCANNER")
    lines.append(f"  {date}")
    lines.append("==============================================")
    lines.append(f"Market regime : {regime}")
    lines.append(f"Capital       : Rs {inr(capital)} | Risk {risk}% per trade")
    if market_open:
        lines.append("")
        lines.append("⚠ MARKET IS OPEN — levels below are LIVE and will")
        lines.append("  shift during the session. Verify before entering.")
        lines.append("  (Best: run/read after 3:30 PM for final levels.)")
    lines.append("")

    if not picks:
        lines.append("NO PICK TODAY - STAY IN CASH")
        lines.append("")
        lines.append("No stock passed the quality filters. Not trading is a")
        lines.append("perfectly valid decision - protecting capital is #1.")
        lines.append("")
        lines.append("How to read this: the scanner ranks ~150 liquid NSE")
        lines.append("stocks on trend, momentum, volume, volatility and")
        lines.append("breakout setup. Today nothing scored high enough.")
        subject = f"Daily Stock Scanner {date}: No pick - stay in cash"
        return subject, "\n".join(lines)

    p = picks[0]
    tp = p.get("plan") or {}
    name = p.get("name", "")
    ticker = p.get("ticker", "").replace(".NS", "")
    score = p.get("total", 0)

    lines.append(f"🏆 PICK OF THE DAY : {ticker}  ({name})")
    lines.append(f"   Score {score:.0f}/100")
    lines.append("")
    lines.append(f"   Last close   : Rs {p.get('close', 0):,.2f}  ({p.get('chg_pct', 0):+.2f}%)")
    lines.append(f"   Entry zone   : Rs {tp.get('entry', 0):,.2f}")
    lines.append(f"   Stop-loss    : Rs {tp.get('sl', 0):,.2f}  (-{tp.get('sl_pct', 0):.1f}%)")
    lines.append(f"   Target 1     : Rs {tp.get('t1', 0):,.2f}  (+{tp.get('t1_pct', 0):.1f}%)")
    lines.append(f"   Target 2     : Rs {tp.get('t2', 0):,.2f}  (+{tp.get('t2_pct', 0):.1f}%)")
    lines.append(f"   Quantity     : {inr(tp.get('qty', 0))} shares")
    lines.append(f"   Capital used : Rs {inr(tp.get('notional', 0))} ({tp.get('notional_pct', 0):.0f}%)")
    lines.append(f"   Risk if SL   : Rs {inr(tp.get('risk_rs', 0))} = {risk}% of capital")
    if tp.get("notional_pct", 0) > 40:
        lines.append("   ⚠ Position uses >40% of capital - consider 2-3 smaller positions")
    why = " · ".join(p.get("reasons", [])) or "trend + setup"
    lines.append(f"   Why          : {why}")
    lines.append(f"   RSI {p.get('rsi', 0):.0f} | ATR {p.get('atr_pct', 0):.1f}% | "
                 f"{p.get('dist_52h', 0):+.1f}% from 52w high")
    lines.append("")
    lines.append("Runners-up (top 5):")
    for i, q in enumerate(picks[1:6], 2):
        lines.append(f"   {i}. {q.get('ticker','').replace('.NS',''):<14} "
                     f"score {q.get('total',0):.0f}  RSI {q.get('rsi',0):.0f}")
    lines.append("")
    lines.append("Checklist:")
    lines.append("  1. Enter at/near open or on a dip toward entry zone.")
    lines.append("  2. Place the stop-loss immediately - it is your plan.")
    lines.append("  3. Book half at Target 1, trail the rest to Target 2.")
    lines.append("  4. Holding window: 1 day to ~1 week. No progress in")
    lines.append("     5 sessions -> exit.")
    lines.append("  5. Keep 2-3 positions max overall.")
    lines.append("")
    lines.append("------------------------------------------------------")
    lines.append("Educational tool, NOT SEBI-registered investment advice.")
    lines.append("No guarantee of profit. Risk only what you can afford to")
    lines.append("lose and use stop-losses on every trade.")
    lines.append("Generated by the Indian Daily Stock Scanner.")

    subject = f"📈 Daily Pick {date}: {ticker} (score {score:.0f})"
    return subject, "\n".join(lines)


# ----------------------------------------------------------------------------
# Send
# ----------------------------------------------------------------------------
def send_email(gmail_user, app_pass, to_email, subject, body, dry_run):
    if dry_run:
        log("DRY-RUN mode — email NOT sent.")
        print("\n----- EMAIL PREVIEW -----")
        print(f"To: {to_email}")
        print(f"Subject: {subject}")
        print("------------------------")
        print(body)
        print("------------------------\n")
        return True

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = formataddr(("Daily Stock Scanner", gmail_user))
    msg["To"] = to_email

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as server:
            server.starttls(context=ctx)
            server.login(gmail_user, app_pass)
            server.sendmail(gmail_user, [to_email], msg.as_string())
        log(f"email sent to {to_email} — subject: {subject}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        log(f"EMAIL FAILED — authentication error: {e}. Check the App Password (16 chars, "
            "no spaces, 2-Step Verification must be ON).")
    except Exception as e:  # noqa: BLE001
        log(f"EMAIL FAILED: {e}")
    return False


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Run scanner + email pick of the day")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and print the email without sending")
    ap.add_argument("--test-mail", action="store_true",
                    help="send a quick test email (no scan)")
    args = ap.parse_args()

    gmail_user, app_pass, to_email, capital, risk = load_settings()

    if args.test_mail:
        if not gmail_user or not app_pass or not to_email:
            log("Set GMAIL_USER, GMAIL_APP_PASSWORD and TO_EMAIL "
                "(env vars or settings.json) first.")
            return 1
        ok = send_email(gmail_user, app_pass, to_email,
                        "✅ Test from Daily Stock Scanner",
                        "If you are reading this, your Gmail SMTP setup works!\n\n"
                        "Tomorrow morning you will receive the pick of the day here.",
                        dry_run=False)
        return 0 if ok else 1

    missing = [k for k, v in (("GMAIL_USER", gmail_user), ("GMAIL_APP_PASSWORD", app_pass),
                              ("TO_EMAIL", to_email)) if not v]
    if missing and not args.dry_run:
        log(f"Missing settings: {', '.join(missing)}. Set them as env vars "
            "(GitHub Secrets) or in settings.json.")
        return 1

    data = run_scan(capital, risk)
    if data is None:
        subject = f"Daily Stock Scanner {ist_now().strftime('%Y-%m-%d')}: data unavailable"
        body = ("The scanner could not download data this morning (often a temporary "
                "Yahoo/network issue). No pick today.\n\n"
                "Check the GitHub Actions log, then re-run: Actions tab -> "
                "Daily Stock Scanner -> Run workflow.\n\n"
                "Educational tool, not investment advice.")
        log("no data — sending failure notice")
    else:
        subject, body = compose(data, capital, risk)

    send_email(gmail_user, app_pass, to_email, subject, body, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
