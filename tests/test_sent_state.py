import base64
import json
from collections import deque

import httpx
import pytest

from agent.pipeline.sent_state import SentIssueStore

REPOSITORY = "owner/notifier"
FILE = f"/repos/{REPOSITORY}/contents/sent-issues.json"
REF = f"/repos/{REPOSITORY}/git/ref/heads/notifier-state"
GIT = f"/repos/{REPOSITORY}/git"
FIRST = "https://github.com/owner/project/issues/1"
SECOND = "https://github.com/owner/project/issues/2"
THIRD = "https://github.com/owner/project/issues/3"
STAMP = "2026-11-19T00:00:00+00:00"


def contents(sent, sha):
    return httpx.Response(
        200,
        json={
            "type": "file",
            "sha": sha,
            "encoding": "base64",
            "content": base64.b64encode(json.dumps(sent).encode()).decode(),
        },
    )


def saved(sha):
    return httpx.Response(200, json={"content": {"sha": sha}})


class ScriptedGitHub:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.requests = []

    def __call__(self, request):
        method, path, response = self.responses.popleft()
        self.requests.append(request)
        assert (request.method, request.url.path) == (method, path)
        assert request.headers["Authorization"] == "Bearer test-token"
        if isinstance(response, httpx.TransportError):
            raise response
        return response

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


async def test_load_and_mark_sent_preserves_prior_records():
    github = ScriptedGitHub(
        ("GET", FILE, contents({FIRST: STAMP}, "previous")),
        ("PUT", FILE, saved("updated")),
    )
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        assert await store.load() == {FIRST}
        await store.mark_sent([FIRST, SECOND, SECOND])
        await store.mark_sent([SECOND])

    assert not github.responses
    assert github.requests[0].url.params["ref"] == "notifier-state"
    payload = json.loads(github.requests[1].content)
    assert payload["branch"] == "notifier-state"
    assert payload["sha"] == "previous"
    sent = json.loads(base64.b64decode(payload["content"]))
    assert sent[FIRST] == STAMP
    assert sent[SECOND]
    assert set(sent) == {FIRST, SECOND}


async def test_conflict_reloads_latest_state_and_merges_before_retry():
    github = ScriptedGitHub(
        ("GET", FILE, contents({FIRST: STAMP}, "previous")),
        ("PUT", FILE, httpx.Response(409)),
        ("GET", FILE, contents({FIRST: STAMP, THIRD: STAMP}, "concurrent")),
        ("PUT", FILE, saved("updated")),
    )
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        await store.load()
        await store.mark_sent([SECOND])

    assert not github.responses
    payload = json.loads(github.requests[-1].content)
    assert payload["sha"] == "concurrent"
    sent = json.loads(base64.b64decode(payload["content"]))
    assert set(sent) == {FIRST, SECOND, THIRD}


async def test_timed_out_write_is_not_repeated_if_github_saved_it():
    github = ScriptedGitHub(
        ("GET", FILE, contents({}, "previous")),
        ("PUT", FILE, httpx.ReadTimeout("response lost")),
        ("GET", FILE, contents({FIRST: STAMP}, "updated")),
    )
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        await store.load()
        await store.mark_sent([FIRST])

    assert not github.responses


async def test_first_real_run_creates_branch_with_state_already_present():
    github = ScriptedGitHub(
        ("GET", FILE, httpx.Response(404)),
        ("GET", REF, httpx.Response(404)),
        ("POST", f"{GIT}/trees", httpx.Response(201, json={"sha": "tree"})),
        ("POST", f"{GIT}/commits", httpx.Response(201, json={"sha": "commit"})),
        ("POST", f"{GIT}/refs", httpx.Response(201)),
        ("GET", FILE, contents({}, "empty")),
    )
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        assert await store.load() == set()

    assert not github.responses
    tree = json.loads(github.requests[2].content)["tree"]
    assert [(entry["path"], json.loads(entry["content"])) for entry in tree] == [
        ("sent-issues.json", {})
    ]
    commit = json.loads(github.requests[3].content)
    assert commit["tree"] == "tree"
    assert not commit.get("parents")
    assert json.loads(github.requests[4].content) == {
        "ref": "refs/heads/notifier-state",
        "sha": "commit",
    }


async def test_preview_with_missing_branch_only_reads():
    github = ScriptedGitHub(("GET", FILE, httpx.Response(404)), ("GET", REF, httpx.Response(404)))
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        assert await store.load(dry_run=True) == set()

    assert not github.responses


async def test_missing_file_on_existing_branch_is_an_error():
    github = ScriptedGitHub(("GET", FILE, httpx.Response(404)), ("GET", REF, httpx.Response(200)))
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        with pytest.raises(RuntimeError, match="missing"):
            await store.load()

    assert not github.responses


async def test_invalid_contents_are_not_replaced_with_an_empty_ledger():
    github = ScriptedGitHub(("GET", FILE, contents([FIRST], "broken")))
    async with github.client() as client:
        store = SentIssueStore(REPOSITORY, "test-token", client)
        with pytest.raises(ValueError, match="invalid"):
            await store.load()

    assert not github.responses
