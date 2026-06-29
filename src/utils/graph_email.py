"""
src/utils/graph_email.py
------------------------
Sends email via Microsoft Graph using client-credentials flow.

Requires .env:
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
    EMAIL_FROM — the mailbox the app sends on behalf of (e.g. harvey.jain@gforceaus.com)
"""
import base64
import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

AZURE_TENANT_ID     = os.environ.get("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID     = os.environ.get("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("AZURE_CLIENT_SECRET", "")
EMAIL_FROM          = os.environ.get("EMAIL_FROM", "")


def _get_token() -> str:
    url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(url, data={
        "grant_type":    "client_credentials",
        "client_id":     AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "scope":         "https://graph.microsoft.com/.default",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_email(
    to: list[str],
    subject: str,
    body: str,
    attachment: tuple[str, str] | None = None,  # (filename, csv_content)
) -> None:
    if not EMAIL_FROM:
        raise ValueError("EMAIL_FROM not set in .env — needed to send via Graph API")

    token = _get_token()
    message: dict = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
    }

    if attachment:
        filename, csv_content = attachment
        message["attachments"] = [{
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": filename,
            "contentType": "text/csv",
            "contentBytes": base64.b64encode(csv_content.encode()).decode(),
        }]

    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{EMAIL_FROM}/sendMail",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json={"message": message, "saveToSentItems": False},
        timeout=15,
    )
    resp.raise_for_status()
    logger.info(f"Email sent: '{subject}' → {to}")
