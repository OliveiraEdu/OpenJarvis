"""Web search tool — provider-configurable (Tavily API or DuckDuckGo).

The active provider is controlled by ``[tools.web_search] provider`` in
``config.toml`` (see ``openjarvis.core.config.WebSearchConfig``):

- ``"duckduckgo"`` (the default) — free DuckDuckGo search, no API key.
  Tavily is never contacted, even when ``TAVILY_API_KEY`` is set.
- ``"tavily"`` — Tavily API (requires ``TAVILY_API_KEY`` via env or the
  credential store). Falls back to DuckDuckGo automatically if the key is
  missing or the API call errors.

The constructor accepts an explicit ``provider`` override for callers that
need to pin the backend regardless of the global config.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.security.ssrf import check_ssrf
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

# Valid provider values and the fallback for unknown/missing config.
PROVIDERS = ("duckduckgo", "tavily")
DEFAULT_PROVIDER = "duckduckgo"

# Appended when the combined results exceed the per-call context budget.
_TRUNCATION_NOTE = (
    "\n\n[More results truncated — re-search with a more specific query if needed]"
)


@ToolRegistry.register("web_search")
class WebSearchTool(BaseTool):
    """Search the web — DuckDuckGo by default, Tavily API when enabled."""

    tool_id = "web_search"
    is_local = False

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        provider: str | None = None,
    ):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY")
        self._max_results = max_results
        self._provider = provider or self._resolve_provider()

    @staticmethod
    def _resolve_provider() -> str:
        """Resolve the search provider from global config (with fallbacks).

        Unknown or unreadable config values degrade to DuckDuckGo instead of
        failing, so a typo in config.toml can never take search offline.
        """
        try:
            from openjarvis.core.config import load_config

            provider = load_config().tools.web_search.provider
        except Exception as exc:  # pragma: no cover - config always loads in prod
            logger.debug(
                "web_search: config unreadable, using %s (%s)", DEFAULT_PROVIDER, exc
            )
            return DEFAULT_PROVIDER
        if provider not in PROVIDERS:
            logger.warning(
                "web_search: unknown provider %r in config, using %s",
                provider,
                DEFAULT_PROVIDER,
            )
            return DEFAULT_PROVIDER
        return provider

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information (DuckDuckGo by default,"
                " Tavily when enabled via config). Returns relevant search results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return.",
                    },
                },
                "required": ["query"],
            },
            category="search",
            metadata={"requires_api_key": "TAVILY_API_KEY", "fallback": "duckduckgo"},
        )

    @staticmethod
    def _is_url(text: str) -> bool:
        """Check if text is a URL."""
        stripped = text.strip()
        return stripped.startswith("http://") or stripped.startswith("https://")

    @staticmethod
    def _extract_url(text: str) -> str | None:
        """Extract the first URL from text, if any."""
        import re as _re

        match = _re.search(r"https?://[^\s,;\"'<>]+", text)
        return match.group(0).rstrip(".,;)") if match else None

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Convert known PDF URLs to their HTML equivalents."""
        import re as _re

        # arxiv: /pdf/ID → /abs/ID (abstract page with full metadata)
        m = _re.match(r"(https?://arxiv\.org)/pdf/(.+?)(?:\.pdf)?$", url)
        if m:
            return f"{m.group(1)}/abs/{m.group(2)}"
        return url

    @staticmethod
    def _fetch_url(url: str, max_chars: int = 4000) -> str:
        """Fetch a URL and return extracted text content."""
        import re as _re

        import httpx

        url = WebSearchTool._normalize_url(url)
        ssrf_error = check_ssrf(url)
        if ssrf_error:
            raise ValueError(ssrf_error)
        resp = httpx.get(
            url.strip(),
            follow_redirects=True,
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; OpenJarvis/1.0; +https://github.com/openjarvis)"
            },
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "application/pdf" in content_type:
            return (
                "[This URL points to a PDF file which"
                f" cannot be read directly. URL: {url}]"
            )
        html = resp.text
        # Strip script/style tags and their contents
        html = _re.sub(
            r"<(script|style)[^>]*>.*?</\1>",
            "",
            html,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        # Strip HTML tags
        text = _re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = _re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n\n[Content truncated]"
        return text

    def _duckduckgo_search(self, query: str, max_results: int) -> str:
        """Search using DuckDuckGo as fallback."""
        from ddgs import DDGS

        # Bound the total search text: the on-device 8K-context models cannot
        # keep working after several multi-KB tool results, so keep every
        # search result compact (snippet-level only).
        _MAX_SNIPPET = 400
        _MAX_TOTAL = 2200

        ddgs = DDGS()
        raw_results = list(ddgs.text(query, max_results=max_results))
        results = []
        for r in raw_results:
            title = r.get("title", "Untitled")
            url = r.get("href", "")
            snippet = r.get("body", "")
            if len(snippet) > _MAX_SNIPPET:
                snippet = snippet[:_MAX_SNIPPET] + "…"
            results.append(f"### {title}\nSource: {url}\nSummary: {snippet}")

        formatted = "\n\n---\n\n".join(results)
        if len(formatted) > _MAX_TOTAL:
            formatted = formatted[:_MAX_TOTAL] + _TRUNCATION_NOTE
        return formatted

    def execute(self, **params: Any) -> ToolResult:
        query = params.get("query", "")
        if not query:
            return ToolResult(
                tool_name="web_search",
                content="No query provided.",
                success=False,
            )

        # If the query contains a URL, fetch it directly instead of searching
        url = self._extract_url(query) if not self._is_url(query) else query.strip()
        if url:
            try:
                content = self._fetch_url(url)
                return ToolResult(
                    tool_name="web_search",
                    content=content or "No content found at URL.",
                    success=True,
                    metadata={"url": url, "mode": "fetch"},
                )
            except Exception as exc:
                return ToolResult(
                    tool_name="web_search",
                    content=f"Failed to fetch URL: {exc}",
                    success=False,
                )

        max_results = params.get("max_results", self._max_results)

        # Tavily is used only when explicitly enabled via config/provider
        # override; otherwise (default) search runs DuckDuckGo and Tavily is
        # never contacted — even if TAVILY_API_KEY happens to be set.
        if self._provider == "tavily":
            try:
                from tavily import TavilyClient

                client = TavilyClient(api_key=self._api_key)
                response = client.search(
                    query,
                    max_results=max_results,
                    search_depth="advanced",
                    include_usage=True,
                )
                results = response.get("results", [])
                # Keep results compact for on-device 8K-context models.
                _MAX_SNIPPET = 600
                _MAX_TOTAL = 2200
                formatted_parts = []
                for r in results:
                    title = r.get("title", "Untitled")
                    url = r.get("url", "")
                    content = r.get("content", "") or r.get("snippet", "")
                    if len(content) > _MAX_SNIPPET:
                        content = content[:_MAX_SNIPPET] + "…"
                    formatted_parts.append(
                        f"### {title}\nSource: {url}\nSummary: {content}"
                    )

                formatted = "\n\n---\n\n".join(formatted_parts)
                if len(formatted) > _MAX_TOTAL:
                    formatted = formatted[:_MAX_TOTAL] + _TRUNCATION_NOTE
                return ToolResult(
                    tool_name="web_search",
                    content=formatted or "No results found.",
                    success=True,
                    metadata={
                        "num_results": len(results),
                        "engine": "tavily",
                        "credits": (response.get("usage") or {}).get("credits"),
                    },
                )
            except Exception as exc:
                logger.debug(
                    "Tavily error (%s), falling back to DuckDuckGo", type(exc).__name__
                )

        try:
            formatted = self._duckduckgo_search(query, max_results)
            return ToolResult(
                tool_name="web_search",
                content=formatted or "No results found.",
                success=True,
                metadata={"engine": "duckduckgo"},
            )
        except ImportError:
            return ToolResult(
                tool_name="web_search",
                content=(
                    "ddgs not available. Install with: pip install ddgs"
                    + (
                        " (and tavily-python if using the tavily provider)"
                        if self._provider == "tavily"
                        else ""
                    )
                ),
                success=False,
            )
        except Exception as exc:
            return ToolResult(
                tool_name="web_search",
                content=f"Search error: {exc}",
                success=False,
            )


__all__ = ["WebSearchTool"]
