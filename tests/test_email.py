"""Unit tests for agent.pipeline.email — mocked transport, no network."""

import httpx
import pytest
from pydantic import SecretStr

from agent.agents import AgentOutput
from agent.config import settings
from agent.pipeline.email import PostmarkClient, PostmarkConfigError
from agent.pipeline.models import Issue

ISSUE = Issue(
    repo="octocat/example-repo",
    title="Fix typo in README",
    body="The word 'recieve' should be 'receive'.",
    labels=["good first issue"],
    html_url="https://github.com/octocat/example-repo/issues/1",
    created_at="2026-07-20T00:00:00Z",
)
VERDICT = AgentOutput(
    is_good_first_issue=True,
    reasoning="Small, localized documentation fix.",
    summary="Fix a typo in the README. Start at line 12 of README.md.",
)


@pytest.fixture(autouse=True)
def postmark_settings(monkeypatch):
    monkeypatch.setattr(settings, "postmark_server_token", "test-token")
    monkeypatch.setattr(settings, "email_from", "digest@example.com")
    monkeypatch.setattr(settings, "email_to", "a@example.com, b@example.com")


async def test_send_digest_posts_to_postmark():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = request.content
        return httpx.Response(200, json={"MessageID": "abc"})

    client = PostmarkClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.send_digest([(ISSUE, VERDICT)])
    finally:
        await client._client.aclose()

    assert captured["url"] == "https://api.postmarkapp.com/email"
    assert captured["headers"]["x-postmark-server-token"] == "test-token"
    assert b"Fix typo in README" in captured["body"]


async def test_send_digest_raises_without_server_token(monkeypatch):
    monkeypatch.setattr(settings, "postmark_server_token", None)
    client = PostmarkClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    )
    try:
        with pytest.raises(PostmarkConfigError):
            await client.send_digest([(ISSUE, VERDICT)])
    finally:
        await client._client.aclose()


async def test_send_digest_raises_on_postmark_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"Message": "invalid"})

    client = PostmarkClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_digest([(ISSUE, VERDICT)])
    finally:
        await client._client.aclose()


async def test_smtp_digest_uses_mailbox_as_sender_and_recipient(monkeypatch):
    from agent.pipeline.email import send_digest

    monkeypatch.setattr(settings, "smtp_server", "smtp.163.com", raising=False)
    monkeypatch.setattr(settings, "smtp_email", "test@163.com", raising=False)
    monkeypatch.setattr(
        settings, "smtp_password", SecretStr("test-authorization-code"), raising=False
    )
    monkeypatch.setattr(settings, "email_from", None)
    monkeypatch.setattr(settings, "email_to", None)
    captured = {}

    class FakeSmtp:
        def __init__(self, host, port, *, timeout, context):
            captured["server"] = (host, port)
            assert context.check_hostname

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def login(self, user, password):
            captured["login"] = (user, password)

        def send_message(self, message):
            captured["message"] = message
            return {}

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSmtp)
    await send_digest([(ISSUE, VERDICT)])

    assert captured["server"] == ("smtp.163.com", 465)
    assert captured["login"] == ("test@163.com", "test-authorization-code")
    message = captured["message"]
    assert message["From"] == message["To"] == "test@163.com"
    assert ISSUE.title in message.get_body(preferencelist=("html",)).get_content()
