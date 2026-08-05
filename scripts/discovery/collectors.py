#!/usr/bin/env python3
"""Collectors — v1 market sources + placeholders (design §4.3).

M2 wires the five low-hanging, no-auth collectors (github, hn, reddit, pypi,
pricing) and registers the complex-source placeholders (SEC EDGAR, Reddit
OAuth, job boards, cloud marketplaces) behind the same ``Collector``
contract. Placeholders are disabled and raise ``NotImplementedError`` if
fetched — no special-casing in the orchestrator.

Contract for every collector:

* stdlib only: ``urllib.request`` with an injectable opener so tests never
  touch the network (C5);
* stable ``source_key`` per item — dedupe lives in ``SignalStore.upsert``;
* idempotent per fetch (re-polls overwrite, never duplicate);
* never touches the LLM — triage is a later stage (M4);
* pure w.r.t. time: ``fetch(now)`` derives every query from the injected
  timestamp (C5), so a given ``now`` yields a given result set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol, runtime_checkable

from config import (
    DiscoveryConfig,
    GithubSettings,
    HNSettings,
    PricingSettings,
    PyPISettings,
    RedditSettings,
)
from store import Signal

DEFAULT_USER_AGENT = "openjarvis-trend-seeker/0.2 (+local market research)"
GITHUB_API = "https://api.github.com"
GITHUB_SEARCH = "https://api.github.com/search/repositories"
HN_SEARCH = "https://hn.algolia.com/api/v1/search"
PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"
PYPI_STATS = "https://pypistats.org/api/packages/{pkg}/recent"
REDDIT_RSS = "https://www.reddit.com/r/{sub}/.rss"

_ATOM = "{http://www.w3.org/2005/Atom}"


class FetchError(RuntimeError):
    """A source was reachable but returned an unusable response (non-2xx)."""


@runtime_checkable
class Collector(Protocol):
    """One market source (design §4.3). ``enabled`` is False for placeholders."""

    name: str
    enabled: bool

    def fetch(self, now: str) -> list[Signal]:
        """Return this cycle's candidates. ``now`` is an ISO timestamp so
        collectors stay pure w.r.t. time (C5)."""
        ...


def _parse_now(now: str) -> datetime:
    dt = datetime.fromisoformat(now)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class BaseCollector:
    """Shared opener plumbing: injectable opener, UA, and timeout (stdlib)."""

    name = ""
    enabled = True

    def __init__(
        self,
        *,
        opener: Any | None = None,
        timeout: float = 15.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._opener = opener if opener is not None else urllib.request.build_opener()
        self._timeout = timeout
        self._user_agent = user_agent

    def _get(self, url: str, *, headers: Optional[dict[str, str]] = None) -> bytes:
        """GET ``url``; raises FetchError/URLError/HTTPError on failure."""
        req = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = self._opener.open(req, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            raise FetchError(f"HTTP {exc.code} for {url}") from exc
        body = resp.read()
        status = getattr(resp, "status", None)
        if status is not None and status >= 400:
            raise FetchError(f"HTTP {status} for {url}")
        return body


class GithubCollector(BaseCollector):
    """GitHub search velocity — star acceleration input (design §4.3).

    One search request per cycle: ``q=<topics> created:>… stars:>…`` sorted by
    stars. ``GITHUB_TOKEN`` env (gitignored) raises the unauth 10 req/min cap;
    never committed.
    """

    name = "github"

    def __init__(
        self,
        settings: GithubSettings,
        *,
        opener: Any | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(opener=opener, timeout=timeout)
        self._settings = settings
        self._token = os.environ.get("GITHUB_TOKEN", "")

    def fetch(self, now: str) -> list[Signal]:
        dt = _parse_now(now)
        created_after = (
            (dt - timedelta(days=self._settings.created_days)).date().isoformat()
        )
        q = (
            f"{self._settings.q} created:>{created_after}"
            f" stars:>{self._settings.min_stars}"
        )
        url = (
            f"{GITHUB_SEARCH}?q={urllib.parse.quote(q)}"
            f"&sort=stars&order=desc&per_page={self._settings.max_repos}"
        )
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        payload = json.loads(self._get(url, headers=headers).decode("utf-8"))

        signals: list[Signal] = []
        for item in payload.get("items", []):
            full_name = item.get("full_name")
            if not full_name:
                continue
            lic = item.get("license") or {}
            signals.append(
                Signal(
                    source=self.name,
                    source_key=full_name,
                    title=full_name,
                    url=item.get("html_url", ""),
                    metrics={
                        "stars": item.get("stargazers_count"),
                        "forks": item.get("forks_count"),
                        "open_issues": item.get("open_issues_count"),
                        "created_at": item.get("created_at"),
                        "pushed_at": item.get("pushed_at"),
                        "language": item.get("language"),
                        "license": lic.get("spdx_id"),
                        "owner_type": (item.get("owner") or {}).get("type"),
                    },
                )
            )

        # Contributor headroom (design §4.4 contributor_spike): the search API
        # does not expose contributor counts, so fetch them per repo. One
        # bounded request per result (max_repos <= 20). Best-effort (D6): a
        # rate-limited/failed repo keeps no contributors metric and simply
        # cannot pre-qualify on that rule.
        for sig in signals:
            owner, _, repo = sig.source_key.partition("/")
            url = f"{GITHUB_API}/repos/{owner}/{repo}/contributors?per_page=30"
            try:
                body = self._get(url, headers=headers).decode("utf-8")
                items = json.loads(body)
                if isinstance(items, list):
                    # len caps at 30 (per_page); the >15 threshold is intact.
                    sig.metrics["contributors"] = len(items)
            except Exception:
                pass  # best-effort: no contributors metric (D6)
        return signals


class HNCollector(BaseCollector):
    """Hacker News keyword velocity (Algolia search, no auth) — design §4.3."""

    name = "hn"

    def __init__(
        self, settings: HNSettings, *, opener: Any | None = None, timeout: float = 15.0
    ) -> None:
        super().__init__(opener=opener, timeout=timeout)
        self._settings = settings

    def fetch(self, now: str) -> list[Signal]:
        del now  # freshness-agnostic: Algolia search ranks recent stories
        url = (
            f"{HN_SEARCH}?query={urllib.parse.quote(self._settings.q)}"
            f"&tags=story&numericFilters={urllib.parse.quote(f'points>{self._settings.min_points}')}"
            f"&hitsPerPage={self._settings.max_items}"
        )
        payload = json.loads(self._get(url).decode("utf-8"))

        signals: list[Signal] = []
        for hit in payload.get("hits", []):
            oid = hit.get("objectID")
            if not oid:
                continue
            signals.append(
                Signal(
                    source=self.name,
                    source_key=f"story_{oid}",
                    title=hit.get("title") or f"hn story {oid}",
                    url=hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    metrics={
                        "points": hit.get("points"),
                        "num_comments": hit.get("num_comments"),
                        "created_at": hit.get("created_at"),
                        "author": hit.get("author"),
                    },
                )
            )
        return signals


class RedditRSSCollector(BaseCollector):
    """Subreddit RSS feeds (no auth) — design §4.3. Churn-phrase matching on
    titles is a rule (M3), not a collector concern."""

    name = "reddit"

    def __init__(
        self,
        settings: RedditSettings,
        *,
        opener: Any | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(opener=opener, timeout=timeout)
        self._settings = settings

    def fetch(self, now: str) -> list[Signal]:
        del now  # RSS is freshness-ordered by the source
        signals: list[Signal] = []
        for sub in self._settings.subreddits:
            body = self._get(REDDIT_RSS.format(sub=sub)).decode(
                "utf-8", errors="replace"
            )
            root = ET.fromstring(body)
            entries = root.findall(f"{_ATOM}entry")[: self._settings.max_items]
            for entry in entries:
                link = entry.findtext(f"{_ATOM}id") or ""
                title = entry.findtext(f"{_ATOM}title") or ""
                if not link or not title:
                    continue
                author_el = entry.find(f"{_ATOM}author/{_ATOM}name")
                signals.append(
                    Signal(
                        source=self.name,
                        source_key=link,
                        title=title,
                        url=link,
                        metrics={
                            "author": author_el.text if author_el is not None else "",
                            "subreddit": sub,
                            "updated": entry.findtext(f"{_ATOM}updated") or "",
                        },
                    )
                )
        return signals


def _latest_release_date(releases: dict[str, Any]) -> str:
    """Max ``upload_time`` across all release files (lexicographic ISO works)."""
    best = ""
    for files in releases.values():
        for file_ in files or []:
            stamp = (file_ or {}).get("upload_time") or ""
            if stamp > best:
                best = stamp
    return best


class PyPICollector(BaseCollector):
    """PyPI metadata + best-effort download stats — design §4.3.

    ``pypi.org/pypi/<pkg>/json`` carries release metadata but no download
    counts, so ADOPTION_SPIKE deltas come from pypistats.org ``/recent``.
    pypistats is best-effort: a failure drops the metric (the download_delta
    rule degrades honestly, D6) instead of killing the package.
    """

    name = "pypi"

    def __init__(
        self,
        settings: PyPISettings,
        *,
        opener: Any | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(opener=opener, timeout=timeout)
        self._settings = settings

    def fetch(self, now: str) -> list[Signal]:
        del now
        signals: list[Signal] = []
        errors: list[Exception] = []
        for pkg in self._settings.packages:
            try:
                payload = json.loads(
                    self._get(PYPI_JSON.format(pkg=pkg)).decode("utf-8")
                )
            except Exception as exc:  # per-package resilience; all-fail raises
                errors.append(exc)
                continue
            info = payload.get("info", {})
            metrics: dict[str, Any] = {
                "version": info.get("version"),
                "summary": (info.get("summary") or "")[:120],
                "requires_python": info.get("requires_python"),
                "releases_count": len(payload.get("releases", {})),
                "latest_release": _latest_release_date(payload.get("releases", {})),
            }
            try:
                stats = json.loads(
                    self._get(PYPI_STATS.format(pkg=pkg)).decode("utf-8")
                )
                data = stats.get("data") or {}
                if data:
                    metrics["downloads_last_week"] = data.get("last_week")
            except Exception:  # pypistats is best-effort (D6)
                pass
            signals.append(
                Signal(
                    source=self.name,
                    source_key=pkg,
                    title=pkg,
                    url=f"https://pypi.org/project/{pkg}/",
                    metrics=metrics,
                )
            )
        if not signals and errors:
            raise errors[-1]
        return signals


_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class PricingCollector(BaseCollector):
    """Pricing page diff — normalized-content hash per watched URL (§4.3).

    The collector always emits the page as a candidate; the PRICING_DIFF
    pre-qualification (hash vs. the store's prior row) is a rule (M3). Static
    HTML only; JS-rendered pages are the headless-browser placeholder.
    """

    name = "pricing"

    def __init__(
        self,
        settings: PricingSettings,
        *,
        opener: Any | None = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(opener=opener, timeout=timeout)
        self._settings = settings

    @staticmethod
    def normalize(body: bytes) -> str:
        """Stable digest input: strip scripts/styles/tags, collapse whitespace.

        Ad-serving HTML churn (scripts, styles, whitespace) is the main
        false-positive source for a raw-page hash; text-only normalization
        keeps it honest.
        """
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        text = _SCRIPT_RE.sub(" ", text)
        text = _TAG_RE.sub(" ", text)
        return _WS_RE.sub(" ", text).strip()

    def fetch(self, now: str) -> list[Signal]:
        del now
        signals: list[Signal] = []
        errors: list[Exception] = []
        for url in self._settings.urls:
            try:
                body = self._get(url)
            except Exception as exc:  # per-url resilience; all-fail raises
                errors.append(exc)
                continue
            normalized = self.normalize(body)
            signals.append(
                Signal(
                    source=self.name,
                    source_key=url,
                    title=_pricing_title(url),
                    url=url,
                    metrics={
                        "content_hash": hashlib.sha256(
                            normalized.encode("utf-8")
                        ).hexdigest(),
                        "bytes": len(body),
                        "normalized_len": len(normalized),
                    },
                )
            )
        if not signals and errors:
            raise errors[-1]
        return signals


def _pricing_title(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc
    return f"pricing: {host}" if host else f"pricing: {url}"


class _PlaceholderCollector:
    """Disabled stub behind the Collector contract (design §4.3): fetching
    raises NotImplementedError — no special-casing in the orchestrator."""

    enabled = False

    def __init__(self, name: str, note: str) -> None:
        self.name = name
        self._note = note

    def fetch(self, now: str) -> list[Signal]:
        raise NotImplementedError(
            f"{self.name} collector is not wired yet"
            f" (design §4.3 placeholder: {self._note})"
        )


def placeholders() -> dict[str, _PlaceholderCollector]:
    """Complex-source stubs — registered, disabled, documented (design §4.3)."""
    return {
        "sec_edgar": _PlaceholderCollector(
            "sec_edgar",
            "SEC EDGAR 10-K/10-Q risk-factor diffing (data.sec.gov, rate-limited)",
        ),
        "reddit_oauth": _PlaceholderCollector(
            "reddit_oauth",
            "Reddit OAuth JSON API for full-text sentiment (auth required)",
        ),
        "job_boards": _PlaceholderCollector(
            "job_boards",
            "job-board keyword-spike tracking (auth varies by board)",
        ),
        "cloud_marketplaces": _PlaceholderCollector(
            "cloud_marketplaces",
            "cloud marketplace listing/price changes (auth varies)",
        ),
    }


def build_registry(cfg: DiscoveryConfig) -> dict[str, Collector]:
    """Collector instances for a config — the five enabled v1 sources plus the
    disabled placeholders (design §4.3)."""
    registry: dict[str, Collector] = {
        "github": GithubCollector(cfg.github),
        "hn": HNCollector(cfg.hn),
        "reddit": RedditRSSCollector(cfg.reddit),
        "pypi": PyPICollector(cfg.pypi),
        "pricing": PricingCollector(cfg.pricing),
    }
    registry.update(placeholders())
    return registry


__all__ = [
    "BaseCollector",
    "Collector",
    "FetchError",
    "GithubCollector",
    "HNCollector",
    "PricingCollector",
    "PyPICollector",
    "RedditRSSCollector",
    "build_registry",
    "placeholders",
]
