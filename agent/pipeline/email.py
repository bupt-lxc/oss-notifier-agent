"""Email the good-first-issue digest through SMTP over TLS or Postmark."""

from __future__ import annotations

import asyncio
import html
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from agent.agents.single import AgentOutput
from agent.config import settings
from agent.logging import get_logger
from agent.pipeline.models import Issue

logger = get_logger(__name__)

POSTMARK_URL = "https://api.postmarkapp.com/email"


def _digest_subject(count: int) -> str:
    return f"OSS Notifier Agent: {count} issue{'' if count == 1 else 's'} found"


def _send_smtp_digest(flagged: list[tuple[Issue, AgentOutput]], server: str) -> None:
    if not settings.smtp_email or not settings.smtp_password:
        raise RuntimeError("SMTP_EMAIL and SMTP_PASSWORD are required when SMTP_SERVER is set.")

    message = EmailMessage()
    message["From"] = settings.email_from or settings.smtp_email
    message["To"] = settings.email_to or settings.smtp_email
    message["Subject"] = _digest_subject(len(flagged))
    message.set_content(_render_digest_html(flagged), subtype="html")
    with smtplib.SMTP_SSL(
        server, settings.smtp_port, timeout=30, context=ssl.create_default_context()
    ) as smtp:
        smtp.login(settings.smtp_email, settings.smtp_password.get_secret_value())
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError(f"SMTP server refused {len(refused)} recipient(s).")
    logger.info("Digest email sent via SMTP", extra={"issue_count": len(flagged)})


async def send_digest(flagged: list[tuple[Issue, AgentOutput]]) -> None:
    if settings.smtp_server:
        await asyncio.to_thread(_send_smtp_digest, flagged, settings.smtp_server)
        return

    client = PostmarkClient()
    try:
        await client.send_digest(flagged)
    finally:
        await client.aclose()


class PostmarkConfigError(RuntimeError):
    """Raised when required Postmark settings are missing at send time."""


class PostmarkClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_digest(self, flagged: list[tuple[Issue, AgentOutput]]) -> None:
        """Send the digest email. No-ops (via the caller) if flagged is empty.

        Raises:
            PostmarkConfigError: Required settings are missing.
            httpx.HTTPStatusError: Postmark rejected the request.
        """
        if not settings.postmark_server_token:
            raise PostmarkConfigError("POSTMARK_SERVER_TOKEN is not set.")
        if not settings.email_from:
            raise PostmarkConfigError("AGENT_EMAIL_FROM is not set.")
        if not settings.email_to:
            raise PostmarkConfigError("AGENT_EMAIL_TO is not set.")

        recipients = [addr.strip() for addr in settings.email_to.split(",") if addr.strip()]
        html_body = _render_digest_html(flagged)
        response = await self._client.post(
            POSTMARK_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": settings.postmark_server_token,
            },
            json={
                "From": settings.email_from,
                "To": ", ".join(recipients),
                "Subject": _digest_subject(len(flagged)),
                "HtmlBody": html_body,
                "MessageStream": "outbound",
            },
        )
        response.raise_for_status()
        logger.info(
            "Digest email sent",
            extra={"recipient_count": len(recipients), "issue_count": len(flagged)},
        )


def _render_digest_html(flagged: list[tuple[Issue, AgentOutput]]) -> str:
    rows = []
    for issue, verdict in flagged:
        rows.append(f"""
        <li style="margin-bottom: 1.5em;">
            <a href="{html.escape(issue.html_url)}"><strong>{html.escape(issue.title)}</strong></a>
            <br><span style="color: #666;">{html.escape(issue.repo)}</span>
            <p>{html.escape(verdict.summary)}</p>
            <p><em>{html.escape(verdict.reasoning)}</em></p>
        </li>
        """)
    return f"""
    <html>
    <body>
        <h2>Good first issues digest</h2>
        <ul>{"".join(rows)}</ul>
    </body>
    </html>
    """
