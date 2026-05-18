"""Deliver the digest to email, Telegram, and a dated markdown file."""

from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path

import requests

log = logging.getLogger("delivery")

REPO_ROOT = Path(__file__).resolve().parent.parent
DIGESTS_DIR = REPO_ROOT / "digests"


# ─── Email (SMTP) ───────────────────────────────────────────────────────────
def send_email(subject: str, html_body: str, text_body: str) -> bool:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM", "EMAIL_TO"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.info("[delivery] email skipped (missing secrets: %s)", ", ".join(missing))
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        log.info("[delivery] email sent to %s", os.environ["EMAIL_TO"])
        return True
    except Exception as e:
        log.error("[delivery] email failed: %s", e)
        return False


# ─── Telegram ───────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.info("[delivery] telegram skipped (missing secrets)")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        r.raise_for_status()
        log.info("[delivery] telegram sent to chat %s", chat_id)
        return True
    except Exception as e:
        log.error("[delivery] telegram failed: %s", e)
        return False


# ─── Local markdown file ─────────────────────────────────────────────────────
def write_markdown_file(markdown: str, date: str | None = None) -> Path:
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    date = date or dt.date.today().isoformat()
    path = DIGESTS_DIR / f"{date}.md"
    path.write_text(markdown, encoding="utf-8")
    log.info("[delivery] wrote %s", path)
    return path


def commit_and_push_markdown(path: Path) -> bool:
    """Stage the digest file, commit, and push to origin/main.

    Skipped inside GitHub Actions — the workflow's own git step handles it there
    (git identity is already configured by the workflow before the bot runs).
    """
    if os.environ.get("GITHUB_ACTIONS"):
        log.info("[delivery] Actions environment detected — git push delegated to workflow")
        return True
    cwd = REPO_ROOT
    try:
        subprocess.run(["git", "add", str(path)], cwd=cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"digest: add {path.name}"],
            cwd=cwd, check=True, capture_output=True,
        )
        subprocess.run(["git", "push"], cwd=cwd, check=True, capture_output=True)
        log.info("[delivery] pushed %s to remote", path.name)
        return True
    except subprocess.CalledProcessError as e:
        log.error("[delivery] git step failed: %s", e.stderr.decode().strip())
        return False
