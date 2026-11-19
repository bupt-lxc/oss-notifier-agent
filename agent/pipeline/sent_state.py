"""Persist sent Issue URLs across GitHub Actions runs."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

STATE_BRANCH = "notifier-state"
STATE_FILE = "sent-issues.json"
WRITE_ATTEMPTS = 3


class SentIssueStore:
    """Read and update the shared notification history on a dedicated branch."""

    def __init__(
        self,
        repository: str,
        token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._repo_url = f"https://api.github.com/repos/{repository}"
        self._file_url = f"{self._repo_url}/contents/{STATE_FILE}"
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
        }
        self._client = http_client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = http_client is None
        self._sent: dict[str, str] = {}
        self._sha: str | None = None
        self._writable = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def load(self, *, dry_run: bool = False) -> set[str]:
        self._writable = False
        if not await self._refresh():
            ref = await self._client.get(
                f"{self._repo_url}/git/ref/heads/{STATE_BRANCH}", headers=self._headers
            )
            if ref.status_code != 404:
                ref.raise_for_status()
                raise RuntimeError(f"{STATE_FILE} is missing from existing {STATE_BRANCH} branch")
            if dry_run:
                return set()
            await self._initialize()

        self._writable = not dry_run
        return set(self._sent)

    async def mark_sent(self, urls: Iterable[str]) -> None:
        if not self._writable or self._sha is None:
            raise RuntimeError("Load the sent Issue history for writing before recording a digest")

        additions = dict.fromkeys(urls, datetime.now(UTC).isoformat())
        for attempt in range(WRITE_ATTEMPTS):
            try:
                if attempt:
                    await asyncio.sleep(0.25 * 2 ** (attempt - 1))
                    if not await self._refresh():
                        raise RuntimeError(f"{STATE_FILE} disappeared while saving the digest")

                combined = additions | self._sent
                if combined == self._sent:
                    return

                content = json.dumps(combined, sort_keys=True, separators=(",", ":")) + "\n"
                response = await self._client.put(
                    self._file_url,
                    headers=self._headers,
                    json={
                        "branch": STATE_BRANCH,
                        "message": "Record sent Issue notifications",
                        "content": base64.b64encode(content.encode()).decode(),
                        "sha": self._sha,
                    },
                )
                response.raise_for_status()
                self._sha = response.json()["content"]["sha"]
                self._sent = combined
                return
            except httpx.TransportError:
                if attempt == WRITE_ATTEMPTS - 1:
                    raise
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                if attempt == WRITE_ATTEMPTS - 1 or (status not in {409, 429} and status < 500):
                    raise

    async def _refresh(self) -> bool:
        response = await self._client.get(
            self._file_url,
            headers=self._headers | {"Accept": "application/vnd.github.object+json"},
            params={"ref": STATE_BRANCH},
        )
        if response.status_code == 404:
            return False
        response.raise_for_status()
        file = response.json()
        if not isinstance(file, dict) or file.get("type") != "file" or not file.get("sha"):
            raise ValueError(f"GitHub returned invalid metadata for {STATE_FILE}")
        sha = file["sha"]
        if file.get("encoding") == "none":
            # Contents responses omit inline content above 1 MB. Read the same blob by SHA.
            blob = await self._client.get(
                f"{self._repo_url}/git/blobs/{sha}", headers=self._headers
            )
            blob.raise_for_status()
            file = blob.json()
        if file.get("encoding") != "base64" or not isinstance(file.get("content"), str):
            raise ValueError(f"GitHub returned invalid content for {STATE_FILE}")

        sent = json.loads(base64.b64decode("".join(file["content"].split()), validate=True))
        if not isinstance(sent, dict) or not all(
            url and isinstance(timestamp, str) and timestamp for url, timestamp in sent.items()
        ):
            raise ValueError(f"{STATE_FILE} contains an invalid sent Issue history")

        self._sha = sha
        self._sent = sent
        return True

    async def _initialize(self) -> None:
        # Publish the branch only when its initial commit already contains the history.
        root = await self._client.post(
            f"{self._repo_url}/git/trees",
            headers=self._headers,
            json={
                "tree": [{"path": STATE_FILE, "mode": "100644", "type": "blob", "content": "{}\n"}]
            },
        )
        root.raise_for_status()
        commit = await self._client.post(
            f"{self._repo_url}/git/commits",
            headers=self._headers,
            json={"message": "Initialize sent Issue history", "tree": root.json()["sha"]},
        )
        commit.raise_for_status()
        ref = await self._client.post(
            f"{self._repo_url}/git/refs",
            headers=self._headers,
            json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit.json()["sha"]},
        )
        if ref.status_code not in {409, 422}:
            ref.raise_for_status()
        if not await self._refresh():
            ref.raise_for_status()
            raise RuntimeError(f"Could not read {STATE_FILE} after creating {STATE_BRANCH}")
