#!/usr/bin/env python3
"""
Forwarders - delivery helpers for the Nifty screener output.

Supports THREE channels:
  1. Telegram  (easiest, recommended, free, no setup fee)
  2. Email     (via Gmail SMTP - needs an App Password, not your regular password)
  3. WhatsApp  (via Twilio WhatsApp Business API - paid, ~$0.005/msg)

CONFIGURATION
-------------
This script reads credentials from ENVIRONMENT VARIABLES first (so it
works natively in GitHub Actions / cloud functions / Docker), and falls
back to a local ~/.screener_secrets.json file if env vars are missing.

For GitHub Actions:  add each var as a repository Secret (Settings >
Secrets and variables > Actions > New repository secret).

For local use:  create ~/.screener_secrets.json with this shape:
    {
        "telegram_bot_token": "123456:ABC-DEF...",
        "telegram_chat_id":   "987654321",
        "email_user":         "you@gmail.com",
        "email_pass":         "your_gmail_app_password",
        "email_to":           "you@gmail.com",
        "twilio_sid":         "ACxxxxxxx",
        "twilio_token":       "xxxxxxx",
        "twilio_whatsapp_from":"whatsapp:+14155238886",
        "twilio_whatsapp_to":  "whatsapp:+91XXXXXXXXXX"
    }

USAGE
-----
    python forwarders.py telegram   # send via Telegram
    python forwarders.py email      # send via Email
    python forwarders.py whatsapp   # send via WhatsApp
    python forwarders.py all        # try all configured channels
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

JSON_PATH = Path(__file__).parent / "nifty_picks_latest.json"


# ----------------------------------------------------------------------------
# Credential loading: env vars first, then local secrets file
# ----------------------------------------------------------------------------

def get_cred(key: str, default: str = "") -> str:
    """Read a credential from env var, falling back to ~/.screener_secrets.json."""
    val = os.environ.get(key, "")
    if val:
        return val
    secrets_path = Path.home() / ".screener_secrets.json"
    if secrets_path.exists():
        with open(secrets_path) as f:
            return json.load(f).get(key, default)
    return default


def load_picks():
    """Load the most recent screener report."""
    if not JSON_PATH.exists():
        # Fallback: most recent nifty_picks_*.json in script dir
        candidates = sorted(Path(__file__).parent.glob("nifty_picks_*.json"))
        if not candidates:
            print(f"[X] No JSON report found at {JSON_PATH}")
            print("    Run nifty_intraday_screener.py first.")
            sys.exit(1)
        path = candidates[-1]
    else:
        path = JSON_PATH
    with open(path) as f:
        return json.load(f), path


# ----------------------------------------------------------------------------
# Message formatting (Markdown for Telegram, plain text for email/WhatsApp)
# ----------------------------------------------------------------------------

def format_message(report: dict) -> str:
    lines = [
        "*Nifty 50 Intraday Picks*",
        f"Run: {report['run_time_ist'][:19]} IST",
        f"Strategy: {report['strategy']}",
        f"Setups found: {report['setups_found']}/{report['total_scanned']}",
        "",
    ]
    if not report['picks']:
        lines += [
            "No high-probability setups today.",
            "Better to skip than force a bad trade.",
        ]
    else:
        for i, p in enumerate(report['picks'], 1):
            lines += [
                f"#{i} *{p['symbol']}*",
                f"  Entry: INR {p['entry']} | SL: INR {p['stop_loss']} (-0.5%)",
                f"  Target: INR {p['target_low']} - INR {p['target_high']} (+1.5% to +2%)",
                f"  R:R {p['risk_reward']} | VWAP INR {p['vwap']}",
                f"  Gap {p['gap_pct']:+.2f}% | Vol {p['volume_spike']}x avg",
                f"  Why: {p['reason']}",
                "",
            ]
    lines.append("_Educational only. Not investment advice._")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Channel: Telegram (recommended - free, instant, no setup cost)
# ----------------------------------------------------------------------------
# SETUP:
#   1. Open Telegram, search for @BotFather
#   2. Send /newbot, follow prompts, get bot token
#   3. Add your bot to a chat (or start a chat with it)
#   4. Visit https://api.telegram.org/bot<TOKEN>/getUpdates to find your chat_id
# ---------------------------------------------------------------------------

def send_telegram(message: str) -> bool:
    import urllib.request
    import urllib.parse

    token   = get_cred("TELEGRAM_BOT_TOKEN")
    chat_id = get_cred("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[X] Telegram credentials not configured.")
        print("    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")
        print("    OR add them to ~/.screener_secrets.json")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    chat_id,
        "text":       message,
        "parse_mode": "Markdown",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                print("[OK] Telegram: message sent.")
                return True
            else:
                print(f"[X] Telegram error: HTTP {resp.status}")
                return False
    except Exception as e:
        print(f"[X] Telegram error: {e}")
        return False


# ----------------------------------------------------------------------------
# Channel: Email (Gmail SMTP)
# ----------------------------------------------------------------------------
# SETUP:
#   1. Enable 2FA on your Google account
#   2. Generate an App Password: https://myaccount.google.com/apppasswords
#   3. Use that 16-char password (NOT your regular Gmail password)
# ---------------------------------------------------------------------------

def send_email(message: str, subject: str = "Nifty 50 Intraday Picks") -> bool:
    user = get_cred("EMAIL_USER")
    pwd  = get_cred("EMAIL_PASS")
    to   = get_cred("EMAIL_TO") or user
    if not user or not pwd:
        print("[X] Email credentials not configured.")
        print("    Set EMAIL_USER, EMAIL_PASS, EMAIL_TO env vars")
        print("    OR add them to ~/.screener_secrets.json")
        return False

    msg = MIMEMultipart("alternative")
    msg['From']    = user
    msg['To']      = to
    msg['Subject'] = subject
    msg.attach(MIMEText(message, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
        print(f"[OK] Email sent to {to}.")
        return True
    except Exception as e:
        print(f"[X] Email error: {e}")
        return False


# ----------------------------------------------------------------------------
# Channel: WhatsApp (via Twilio)
# ----------------------------------------------------------------------------
# SETUP:
#   1. Sign up at https://www.twilio.com (free trial gives ~$15 credit)
#   2. Enable WhatsApp Business API in console
#   3. Get a Twilio WhatsApp sender number
#   4. Verify your recipient number (trial accounts need this)
#   5. Costs ~$0.005/message after trial
# ---------------------------------------------------------------------------

def send_whatsapp(message: str) -> bool:
    try:
        from twilio.rest import Client
    except ImportError:
        print("[X] Twilio SDK not installed. Run:  pip install twilio")
        return False

    sid       = get_cred("TWILIO_SID")
    token     = get_cred("TWILIO_TOKEN")
    from_num  = get_cred("TWILIO_WHATSAPP_FROM")
    to_num    = get_cred("TWILIO_WHATSAPP_TO")
    if not all([sid, token, from_num, to_num]):
        print("[X] Twilio credentials not configured.")
        print("    Set TWILIO_SID, TWILIO_TOKEN, TWILIO_WHATSAPP_FROM, TWILIO_WHATSAPP_TO")
        return False

    try:
        client = Client(sid, token)
        msg = client.messages.create(
            from_=from_num,
            body=message,
            to=to_num,
        )
        print(f"[OK] WhatsApp sent. SID: {msg.sid}")
        return True
    except Exception as e:
        print(f"[X] WhatsApp error: {e}")
        return False


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"telegram", "email", "whatsapp", "all"}:
        print("Usage: python forwarders.py <telegram|email|whatsapp|all>")
        sys.exit(1)

    report, path = load_picks()
    print(f"[i] Loaded report from: {path}")
    message = format_message(report)
    print("\n--- MESSAGE PREVIEW ---")
    print(message)
    print("--- END PREVIEW ---\n")

    channel = sys.argv[1]
    if channel == "telegram":
        send_telegram(message)
    elif channel == "email":
        send_email(message)
    elif channel == "whatsapp":
        send_whatsapp(message)
    elif channel == "all":
        # Try every configured channel, report results
        results = []
        results.append(("telegram", send_telegram(message)))
        results.append(("email",    send_email(message)))
        results.append(("whatsapp", send_whatsapp(message)))
        print("\n--- DELIVERY SUMMARY ---")
        for ch, ok in results:
            print(f"  {ch:10s}: {'OK' if ok else 'FAILED (or not configured)'}")
