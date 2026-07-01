"""
lib/mailer.py
Send notification emails via Gmail SMTP.
Credentials come from trader/secrets.yaml (gitignored).

Usage:
    from lib.mailer import send
    send("Critical2026 step 3 DONE", "Details here...")
"""

import smtplib
import yaml
from email.mime.text import MIMEText
from pathlib import Path

from lib.config_loader import get_config


def _load_app_password() -> str:
    secrets_path = Path(__file__).parent.parent / "trader" / "secrets.yaml"
    if not secrets_path.exists():
        raise FileNotFoundError(f"secrets.yaml not found: {secrets_path}")
    with open(secrets_path) as f:
        data = yaml.safe_load(f)
    return data["mailer"]["app_password"]


def send(subject: str, body: str = "") -> None:
    cfg = get_config().mailer
    password = _load_app_password()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg.from_addr, password)
        smtp.sendmail(cfg.from_addr, [cfg.to_addr], msg.as_string())
