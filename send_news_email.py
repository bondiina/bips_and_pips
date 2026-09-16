#!/usr/bin/env python3

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

REPORT = Path(__file__).resolve().parent / "latest-news.md"
EMAIL_TO = "derry2bond@gmail.com"


def main():
    body = REPORT.read_text(encoding="utf-8").strip()

    if not body or "No new articles found" in body:
        print("No new news to email.")
        return

    msg = EmailMessage()
    msg["Subject"] = "Fintech News — New Updates"
    msg["From"] = os.environ["SMTP_USERNAME"]
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(
        os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        int(os.environ.get("SMTP_PORT", "465")),
        context=context,
    ) as smtp:
        smtp.login(
            os.environ["SMTP_USERNAME"],
            os.environ["SMTP_PASSWORD"],
        )
        smtp.send_message(msg)

    print(f"News email sent to {EMAIL_TO}")


if __name__ == "__main__":
    main()
