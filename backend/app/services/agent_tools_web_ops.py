"""Web-search dispatch tools extracted from the unified agent tool service."""

import uuid

from app.config import get_settings
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_tools_web_support import (
    _search_bing,
    _search_duckduckgo,
    _search_exa,
    _search_google,
    _search_tavily,
)


async def _web_search(arguments: dict, agent_id: uuid.UUID | None = None) -> str:
    """Search the web using a configurable search engine.

    Config resolution priority: Agent config > Company config > Defaults.
    """
    import httpx
    import re

    query = arguments.get("query", "")
    if not query:
        return "❌ Please provide search keywords"

    # Use the standard _get_tool_config helper (Agent > Company, cached, decrypted)
    config = await _get_tool_config(agent_id, "web_search") or {}

    engine = config.get("search_engine", "duckduckgo")
    api_key = config.get("api_key", "")
    max_results = min(arguments.get("max_results", config.get("max_results", 5)), 10)
    language = config.get("language", "zh-CN")

    try:
        if engine == "tavily" and api_key:
            return await _search_tavily(query, api_key, max_results)
        elif engine == "google" and api_key:
            return await _search_google(query, api_key, max_results, language)
        elif engine == "bing" and api_key:
            return await _search_bing(query, api_key, max_results, language)
        elif engine == "exa" and api_key:
            return await _search_exa(query, api_key, max_results)
        else:
            return await _search_duckduckgo(query, max_results)
    except Exception as e:
        return f"❌ Search error ({engine}): {str(e)[:200]}"


async def _exa_search(arguments: dict, agent_id: uuid.UUID | None = None) -> str:
    """Full-featured Exa AI search with category filtering, domain filtering, and content modes."""
    import httpx

    query = arguments.get("query", "").strip()
    if not query:
        return "❌ Please provide search keywords"

    config = await _get_tool_config(agent_id, "exa_search") or {}
    api_key = config.get("api_key", "") or get_settings().EXA_API_KEY
    if not api_key:
        return "❌ Exa API key is required. Set it in tool settings or the EXA_API_KEY environment variable."

    max_results = min(arguments.get("max_results", 5), 10)
    search_type = arguments.get("search_type", "auto")
    category = arguments.get("category") or None
    content_mode = arguments.get("content_mode", "text")
    include_domains = arguments.get("include_domains")
    exclude_domains = arguments.get("exclude_domains")

    body: dict = {
        "query": query,
        "type": search_type,
        "numResults": max_results,
        "contents": {},
    }

    if category:
        body["category"] = category
    if include_domains:
        body["includeDomains"] = [d.strip() for d in include_domains.split(",") if d.strip()]
    if exclude_domains:
        body["excludeDomains"] = [d.strip() for d in exclude_domains.split(",") if d.strip()]

    if content_mode == "highlights":
        body["contents"]["highlights"] = {"numSentences": 3}
    elif content_mode == "summary":
        body["contents"]["summary"] = {}
    else:
        body["contents"]["text"] = {"maxCharacters": 1000}

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.exa.ai/search",
                json=body,
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                    "x-exa-integration": "clawith",
                },
                timeout=15,
            )
            data = resp.json()

        if resp.status_code != 200:
            return f"❌ Exa search failed: {data.get('error', data.get('message', str(data)[:200]))}"

        items = data.get("results", [])[:max_results]
        if not items:
            return f'🔍 No results found for "{query}"'

        parts = []
        for i, r in enumerate(items, 1):
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            content = ""
            if content_mode == "highlights" and r.get("highlights"):
                content = " ... ".join(r["highlights"])
            elif content_mode == "summary" and r.get("summary"):
                content = r["summary"]
            elif r.get("text"):
                content = r["text"][:500]
            parts.append(f"**{i}. {title}**\n{url}\n{content}")

        return f'🔍 Exa search for "{query}" ({len(items)} items):\n\n' + "\n\n---\n\n".join(parts)

    except Exception as e:
        return f"❌ Exa search error: {str(e)[:300]}"


# ── Standalone search engine tool wrappers ───────────────────────────────────
# Each function reads its own tool config (agent > company > defaults) and
# delegates to the existing private search implementations above.


async def _duckduckgo_search_tool(arguments: dict) -> str:
    """Standalone DuckDuckGo search tool (no API key required)."""
    query = arguments.get("query", "").strip()
    if not query:
        return "Please provide search keywords"
    max_results = min(arguments.get("max_results", 5), 10)
    return await _search_duckduckgo(query, max_results)


async def _tavily_search_tool(arguments: dict, agent_id: uuid.UUID | None = None) -> str:
    """Standalone Tavily search tool (API key read from per-tool config)."""
    query = arguments.get("query", "").strip()
    if not query:
        return "Please provide search keywords"
    config = await _get_tool_config(agent_id, "tavily_search") or {}
    api_key = config.get("api_key", "").strip()
    if not api_key:
        return "Tavily API key is required. Set it in the tool settings."
    max_results = min(arguments.get("max_results", 5), 10)
    try:
        return await _search_tavily(query, api_key, max_results)
    except Exception as e:
        return f"Tavily search error: {str(e)[:200]}"


async def _google_search_tool(arguments: dict, agent_id: uuid.UUID | None = None) -> str:
    """Standalone Google Custom Search tool (API key read from per-tool config)."""
    query = arguments.get("query", "").strip()
    if not query:
        return "Please provide search keywords"
    config = await _get_tool_config(agent_id, "google_search") or {}
    api_key = config.get("api_key", "").strip()
    if not api_key:
        return "Google Search API key is required (format: API_KEY:SEARCH_ENGINE_ID). Set it in the tool settings."
    # Allow per-call language override; fall back to tool config, then default
    language = arguments.get("language") or config.get("language", "en")
    max_results = min(arguments.get("max_results", 5), 10)
    try:
        return await _search_google(query, api_key, max_results, language)
    except Exception as e:
        return f"Google search error: {str(e)[:200]}"


async def _bing_search_tool(arguments: dict, agent_id: uuid.UUID | None = None) -> str:
    """Standalone Bing Web Search tool (API key read from per-tool config)."""
    query = arguments.get("query", "").strip()
    if not query:
        return "Please provide search keywords"
    config = await _get_tool_config(agent_id, "bing_search") or {}
    api_key = config.get("api_key", "").strip()
    if not api_key:
        return "Bing Search API key is required. Set it in the tool settings."
    language = arguments.get("language") or config.get("language", "en-US")
    max_results = min(arguments.get("max_results", 5), 10)
    try:
        return await _search_bing(query, api_key, max_results, language)
    except Exception as e:
        return f"Bing search error: {str(e)[:200]}"

__all__ = [name for name in globals() if not name.startswith("__")]
