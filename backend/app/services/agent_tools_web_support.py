import asyncio
import re
from urllib.parse import urlparse

from app.config import get_settings


async def _search_duckduckgo(query: str, max_results: int) -> str:
    """Search via DuckDuckGo HTML (free, no API key)."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=10,
        )

    results = []
    blocks = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        resp.text,
        re.DOTALL,
    )
    for url, title, snippet in blocks[:max_results]:
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        if "uddg=" in url:
            from urllib.parse import parse_qs, unquote, urlparse

            parsed = parse_qs(urlparse(url).query)
            url = unquote(parsed.get("uddg", [url])[0])
        results.append(f"**{title}**\n{url}\n{snippet}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 DuckDuckGo results for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _get_jina_api_key() -> str:
    """Read Jina API key from DB system_settings first, then fall back to env."""
    try:
        from app.database import async_session
        from app.models.system_settings import SystemSetting
        from sqlalchemy import select

        async with async_session() as db:
            result = await db.execute(select(SystemSetting).where(SystemSetting.key == "jina_api_key"))
            setting = result.scalar_one_or_none()
            if setting and setting.value.get("api_key"):
                return setting.value["api_key"]
    except Exception:
        pass
    return get_settings().JINA_API_KEY


async def _jina_search(arguments: dict) -> str:
    """Search via Jina AI Search API (s.jina.ai). Returns full content per result, not just snippets."""
    import httpx

    query = arguments.get("query", "").strip()
    if not query:
        return "❌ Please provide search keywords"

    max_results = min(arguments.get("max_results", 5), 10)
    api_key = await _get_jina_api_key()

    headers: dict = {
        "Accept": "application/json",
        "X-Respond-With": "no-content",
        "X-Return-Format": "markdown",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                f"https://s.jina.ai/{__import__('urllib.parse', fromlist=['quote']).quote(query)}",
                headers=headers,
            )

        if resp.status_code != 200:
            return f"❌ Jina Search error HTTP {resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        items = data.get("data", [])[:max_results]

        if not items:
            return f'🔍 No results found for "{query}"'

        parts = []
        for i, item in enumerate(items, 1):
            title = item.get("title", "Untitled")
            url = item.get("url", "")
            description = item.get("description", "") or item.get("content", "")[:500]
            parts.append(f"**{i}. {title}**\n{url}\n{description}")

        return f'🔍 Jina Search results for "{query}" ({len(items)} items):\n\n' + "\n\n---\n\n".join(parts)

    except Exception as e:
        return f"❌ Jina Search error: {str(e)[:300]}"


async def _jina_read(arguments: dict) -> str:
    """Read web page via Jina AI Reader API (r.jina.ai). Returns clean structured markdown."""
    import httpx

    url = arguments.get("url", "").strip()
    if not url:
        return "❌ Please provide a URL"
    if not url.startswith("http"):
        url = "https://" + url

    max_chars = min(arguments.get("max_chars", 8000), 20000)
    api_key = await _get_jina_api_key()

    headers: dict = {
        "Accept": "text/plain, text/markdown, */*",
        "X-Return-Format": "markdown",
        "X-Remove-Selector": "header, footer, nav, aside, .ads, .advertisement",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(
                f"https://r.jina.ai/{url}",
                headers=headers,
            )

        if resp.status_code != 200:
            return f"❌ Jina Reader error HTTP {resp.status_code}: {resp.text[:200]}"

        text = resp.text.strip()
        if not text or len(text) < 100:
            return f"❌ Jina Reader returned empty content for {url}"

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... truncated at {max_chars} chars]"

        return f"📄 **Content from: {url}**\n\n{text}"

    except Exception as e:
        return f"❌ Jina Reader error: {str(e)[:300]}"


async def _validate_public_http_url(url: str) -> tuple[str | None, str | None]:
    """Normalize a URL and reject local/private network targets."""
    import ipaddress
    import socket

    url = (url or "").strip()
    if not url:
        return None, "❌ Please provide a URL"
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None, "❌ Only HTTP and HTTPS URLs are supported"
    if not parsed.hostname:
        return None, "❌ URL must include a hostname"

    hostname = parsed.hostname
    try:
        ipaddress.ip_address(hostname)
        host_is_ip = True
    except ValueError:
        host_is_ip = False

    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return None, "❌ Localhost URLs are blocked for safety"

    try:
        if host_is_ip:
            addresses = [hostname]
        else:
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                ),
            )
            addresses = [info[4][0] for info in infos]
    except Exception as exc:
        return None, f"❌ Could not resolve hostname {hostname}: {str(exc)[:160]}"

    for address in set(addresses):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None, f"❌ Could not validate resolved address: {address}"
        is_proxy_test_range = (not host_is_ip) and ip in ipaddress.ip_network("198.18.0.0/15")
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
            or (ip.is_private and not is_proxy_test_range)
        ):
            return None, f"❌ Private, local, reserved, or internal network URLs are blocked ({address})"

    return url, None


def _fallback_extract_visible_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "canvas", "header", "footer", "nav", "aside"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_page_links(html: str, base_url: str, limit: int = 30) -> list[str]:
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))[:80] or href
        seen.add(href)
        links.append(f"- {label}: {href}")
        if len(links) >= limit:
            break
    return links


async def _read_webpage(arguments: dict) -> str:
    """Fetch and extract readable content from a public webpage without a third-party reader API."""
    import httpx
    import trafilatura
    from bs4 import BeautifulSoup

    url, validation_error = await _validate_public_http_url(arguments.get("url", ""))
    if validation_error:
        return validation_error

    max_chars = min(max(int(arguments.get("max_chars", 12000)), 500), 50000)
    include_links = bool(arguments.get("include_links", False))
    max_bytes = 2_000_000
    headers = {
        "User-Agent": "PlatformBot/1.0 Mozilla/5.0",
        "Accept": "text/html, text/plain, application/json, application/xml;q=0.9, text/*;q=0.8, */*;q=0.5",
    }

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                content_length = resp.headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    return f"❌ Page is too large to read safely ({content_length} bytes, limit {max_bytes} bytes)"

                chunks: list[bytes] = []
                total = 0
                truncated_bytes = False
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        remaining = max_bytes - sum(len(part) for part in chunks)
                        if remaining > 0:
                            chunks.append(chunk[:remaining])
                        truncated_bytes = True
                        break
                    chunks.append(chunk)

                status_code = resp.status_code
                final_url = str(resp.url)
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                encoding = resp.encoding or "utf-8"

        raw = b"".join(chunks)
        if status_code >= 400:
            return f"❌ Webpage fetch failed HTTP {status_code}: {final_url}"

        text = raw.decode(encoding, errors="replace").strip()
        if not text:
            return f"❌ Empty response from {final_url}"

        title = ""
        description = ""
        extracted = text
        links: list[str] = []

        if content_type in {"", "text/html", "application/xhtml+xml"} or "<html" in text[:500].lower():
            soup = BeautifulSoup(text, "html.parser")
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            meta_description = soup.find("meta", attrs={"name": "description"})
            if meta_description and meta_description.get("content"):
                description = meta_description["content"].strip()

            extracted = trafilatura.extract(
                text,
                url=final_url,
                output_format="markdown",
                include_links=include_links,
                include_comments=False,
                include_tables=True,
            ) or _fallback_extract_visible_text(text)
            if include_links:
                links = _extract_page_links(text, final_url)
        elif content_type.startswith("text/") or content_type in {"application/json", "application/xml", "text/xml"}:
            title = final_url
        else:
            return f"❌ Unsupported content type: {content_type or 'unknown'}"

        extracted = extracted.strip()
        if not extracted:
            return f"❌ Could not extract readable content from {final_url}"

        truncated_chars = len(extracted) > max_chars
        if truncated_chars:
            extracted = extracted[:max_chars].rstrip() + f"\n\n[... truncated at {max_chars} chars]"

        meta_lines = [
            f"URL: {final_url}",
            f"Status: HTTP {status_code}",
        ]
        if title:
            meta_lines.append(f"Title: {title}")
        if description:
            meta_lines.append(f"Description: {description}")
        if truncated_bytes:
            meta_lines.append(f"Note: response body truncated at {max_bytes} bytes before extraction")
        if truncated_chars:
            meta_lines.append(f"Note: extracted text truncated at {max_chars} characters")

        result = "🌐 **Webpage content**\n\n" + "\n".join(meta_lines) + "\n\n---\n\n" + extracted
        if links:
            result += "\n\n---\n\nLinks:\n" + "\n".join(links)
        return result

    except httpx.TimeoutException:
        return f"❌ Webpage fetch timed out: {url}"
    except Exception as e:
        return f"❌ Webpage read error: {str(e)[:300]}"


async def _search_tavily(query: str, api_key: str, max_results: int) -> str:
    """Search via Tavily API (AI-optimized search)."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": max_results, "search_depth": "basic"},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        data = resp.json()

    if "results" not in data:
        return f"❌ Tavily search failed: {data.get('error', str(data)[:200])}"

    results = []
    for r in data["results"][:max_results]:
        results.append(f"**{r.get('title', '')}**\n{r.get('url', '')}\n{r.get('content', '')[:200]}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Tavily search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_google(query: str, api_key: str, max_results: int, language: str) -> str:
    """Search via Google Custom Search JSON API."""
    import httpx

    parts = api_key.split(":", 1)
    if len(parts) != 2:
        return "❌ Google search requires API key in format 'API_KEY:SEARCH_ENGINE_ID'"

    gapi_key, cx = parts
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": gapi_key, "cx": cx, "q": query, "num": max_results, "lr": f"lang_{language[:2]}"},
            timeout=10,
        )
        data = resp.json()

    results = []
    for item in data.get("items", [])[:max_results]:
        results.append(f"**{item.get('title', '')}**\n{item.get('link', '')}\n{item.get('snippet', '')}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Google search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_bing(query: str, api_key: str, max_results: int, language: str) -> str:
    """Search via Bing Web Search API."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results, "mkt": language},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=10,
        )
        data = resp.json()

    results = []
    for item in data.get("webPages", {}).get("value", [])[:max_results]:
        results.append(f"**{item.get('name', '')}**\n{item.get('url', '')}\n{item.get('snippet', '')}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Bing search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)


async def _search_exa(query: str, api_key: str, max_results: int) -> str:
    """Search via Exa AI API (exa.ai). Used by the web_search engine selector."""
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.exa.ai/search",
            json={
                "query": query,
                "type": "auto",
                "numResults": max_results,
                "contents": {"text": {"maxCharacters": 1000}},
            },
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

    results = []
    for r in data.get("results", [])[:max_results]:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        text = (r.get("text") or "")[:300]
        results.append(f"**{title}**\n{url}\n{text}")

    if not results:
        return f'🔍 No results found for "{query}"'
    return f'🔍 Exa search for "{query}" ({len(results)} items):\n\n' + "\n\n---\n\n".join(results)
