import json
import os
import urllib.error
import urllib.request
from typing import Dict
from urllib.parse import urljoin, urlparse


class FirecrawlError(RuntimeError):
    pass


DEFAULT_FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"


def firecrawl_available(api_key: str = "") -> bool:
    return bool((api_key or os.getenv("FIRECRAWL_API_KEY", "")).strip())


def firecrawl_scrape_markdown(
    url: str,
    api_key: str = "",
    base_url: str = "",
    timeout_seconds: int = 30,
    max_age_ms: int = 86400000,
    max_chars: int = 200000,
) -> Dict:
    api_key = (api_key or os.getenv("FIRECRAWL_API_KEY", "")).strip()
    if not api_key:
        raise FirecrawlError("FIRECRAWL_API_KEY is not configured")
    endpoint = firecrawl_endpoint(base_url or os.getenv("FIRECRAWL_BASE_URL", DEFAULT_FIRECRAWL_BASE_URL), "/v2/scrape")
    body = {
        "url": url,
        "formats": ["markdown"],
        "onlyMainContent": True,
        "timeout": max(1, int(timeout_seconds or 30)) * 1000,
        "maxAge": max(0, int(max_age_ms or 0)),
        "proxy": "auto",
        "storeInCache": True,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer %s" % api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds or 30))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = read_error_detail(exc)
        raise FirecrawlError("Firecrawl HTTP %s: %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise FirecrawlError("Firecrawl URL error: %s" % exc.reason)
    except json.JSONDecodeError as exc:
        raise FirecrawlError("Firecrawl returned invalid JSON: %s" % exc)

    if payload.get("success") is False:
        raise FirecrawlError(str(payload.get("error") or payload.get("message") or "Firecrawl scrape failed"))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    markdown = data.get("markdown") or data.get("content") or data.get("html") or ""
    if not isinstance(markdown, str) or not markdown.strip():
        raise FirecrawlError("Firecrawl scrape returned no content")
    return {
        "url": url,
        "final_url": metadata.get("sourceURL") or data.get("url") or url,
        "status": metadata.get("statusCode") or data.get("statusCode"),
        "title": metadata.get("title") or "",
        "text": markdown[: max(1000, int(max_chars or 200000))],
    }


def firecrawl_endpoint(base_url: str, path: str) -> str:
    parsed = urlparse((base_url or DEFAULT_FIRECRAWL_BASE_URL).strip())
    if parsed.scheme != "https":
        raise FirecrawlError("Firecrawl base URL must use https")
    if parsed.username or parsed.password:
        raise FirecrawlError("Firecrawl base URL must not include credentials")
    base = parsed.geturl().rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def read_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(4096).decode("utf-8", errors="replace")
    except Exception:
        return exc.reason or "request failed"
    try:
        payload = json.loads(raw)
        if isinstance(payload, dict):
            return str(payload.get("error") or payload.get("message") or raw[:500])
    except json.JSONDecodeError:
        pass
    return raw[:500] or exc.reason or "request failed"
