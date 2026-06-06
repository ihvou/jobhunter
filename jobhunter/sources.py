import email
import email.header
import html as html_lib
import ipaddress
import imaplib
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from .firecrawl import (
    FirecrawlError,
    firecrawl_available,
    firecrawl_scrape_markdown,
    firecrawl_scrape_raw_html,
)
from .logging_setup import log_context
from .models import Job, SourceConfig, utc_now_iso

LOGGER = logging.getLogger(__name__)
MAX_BYTES = int(os.getenv("JOBHUNTER_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
CHECK_ROBOTS = os.getenv("JOBHUNTER_CHECK_ROBOTS", "0").strip().lower() in ("1", "true", "yes", "on")
ROBOTS_TXT_RESPECT = os.getenv("JOBHUNTER_ROBOTS_TXT_RESPECT", "ignore").strip().lower()
EMAIL_SAMPLE_MAX_BYTES = int(os.getenv("JOBHUNTER_EMAIL_SAMPLE_MAX_BYTES", str(256 * 1024)))
EMAIL_SAMPLE_KEEP_PER_SENDER = int(os.getenv("JOBHUNTER_EMAIL_SAMPLE_KEEP_PER_SENDER", "20"))
HOST_LAST_FETCH: Dict[str, float] = {}
VALID_SOURCE_TYPES = {"rss", "rss_proxy", "json_api", "ats", "community", "imap", "hn"}
LEGACY_SOURCE_TYPE_ALIASES = {
    "email_alert": "imap",
    "remotive": "json_api",
    "remoteok": "json_api",
    "arbeitnow": "json_api",
}


DEFAULT_HEADERS = {
    "User-Agent": "jobhunter-openclaw-jobhunter/0.1 (+human-in-the-loop; contact: local-user)",
    "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html;q=0.8",
}


class SourceError(RuntimeError):
    pass


def normalize_source_type(value: str) -> str:
    source_type = str(value or "").strip().lower()
    return LEGACY_SOURCE_TYPE_ALIASES.get(source_type, source_type)


class HTMLTextExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.parts = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return " ".join(self.parts)


class HTMLLinkExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.links = []
        self._href = ""
        self._text = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "a":
            attrs_dict = dict(attrs)
            self._href = attrs_dict.get("href", "")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            text = " ".join(" ".join(self._text).split())
            self.links.append((self._href, text))
            self._href = ""
            self._text = []


class HTMLMetadataExtractor(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.meta = {}
        self.ld_json = []
        self._in_ld_json = False
        self._ld_parts = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = {str(key).lower(): value for key, value in attrs}
        if tag.lower() == "meta":
            name = (attrs_dict.get("property") or attrs_dict.get("name") or "").lower()
            content = attrs_dict.get("content") or ""
            if name and content:
                self.meta[name] = content
        if tag.lower() == "script" and "ld+json" in (attrs_dict.get("type") or "").lower():
            self._in_ld_json = True
            self._ld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_ld_json:
            text = "".join(self._ld_parts).strip()
            if text:
                self.ld_json.append(text)
            self._in_ld_json = False
            self._ld_parts = []


def strip_html(value: str) -> str:
    parser = HTMLTextExtractor()
    try:
        parser.feed(value or "")
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", value or "")


def parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = str(value).strip()
    if value.isdigit() and len(value) >= 10:
        timestamp = int(value[:10])
        try:
            return datetime.utcfromtimestamp(timestamp).replace(microsecond=0).isoformat() + "Z"
        except (OverflowError, OSError, ValueError):
            pass
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.replace(microsecond=0).isoformat()
    except Exception:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(microsecond=0).isoformat()
    except Exception:
        return None


def fetch_text(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    robots_check: bool = True,
    attempts: int = 1,
    retry_delay: float = 0,
) -> str:
    validate_safe_url(url)
    wait_for_host_rate_limit(url)
    if robots_check and CHECK_ROBOTS and not robots_allowed(url):
        raise SourceError("Robots.txt disallows %s" % url)
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, headers=merged_headers)
    attempts = max(1, int(attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final_url = response.geturl()
                validate_safe_url(final_url)
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise SourceError("Response too large for %s" % url)
                log_context(LOGGER, logging.DEBUG, "source_fetch_ok", url=url, final_url=final_url, bytes=len(body))
                return body.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise SourceError("HTTP %s fetching %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            if _is_timeout_error(exc.reason) and attempt < attempts:
                log_fetch_retry(url, attempt, attempts, "URL timeout: %s" % exc.reason, retry_delay)
                continue
            raise SourceError("URL error fetching %s: %s" % (url, exc.reason))
        except (TimeoutError, socket.timeout) as exc:
            if attempt < attempts:
                log_fetch_retry(url, attempt, attempts, "timeout: %s" % exc, retry_delay)
                continue
            raise SourceError("Timeout fetching %s after %s attempt(s): %s" % (url, attempts, exc))
    raise SourceError("Failed fetching %s" % url)


def _is_timeout_error(exc) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower()


def log_fetch_retry(url: str, attempt: int, attempts: int, error: str, retry_delay: float) -> None:
    log_context(LOGGER, logging.WARNING, "source_fetch_retry", url=url, attempt=attempt, attempts=attempts, error=error)
    if retry_delay > 0:
        time.sleep(retry_delay)


def fetch_source_text(
    source: SourceConfig,
    url: Optional[str] = None,
    timeout: int = 30,
    attempts: int = 1,
    retry_delay: float = 0,
) -> str:
    return fetch_text(
        url or source.url,
        source.headers,
        timeout=timeout,
        robots_check=robots_check_for_source(source),
        attempts=attempts,
        retry_delay=retry_delay,
    )


def robots_check_for_source(source: SourceConfig) -> bool:
    if source.robots_check is not None:
        return bool(source.robots_check)
    policy = (ROBOTS_TXT_RESPECT or "ignore").strip().lower()
    if policy == "ignore":
        return False
    if policy == "strict":
        return True
    return not (source.created_by == "user" or source.risk_level == "low")


def collect_from_source(source: SourceConfig) -> List[Job]:
    source_type = source.type.lower()
    if source_type == "rss":
        return collect_rss(source)
    if source_type == "remotive":
        return collect_remotive(source)
    if source_type == "remoteok":
        return collect_remoteok(source)
    if source_type == "arbeitnow":
        return collect_arbeitnow(source)
    if source_type == "json_api":
        return collect_generic_json(source)
    if source_type == "hn":
        return collect_hn(source)
    if source_type == "ats":
        return collect_ats(source)
    if source_type == "community":
        return collect_link_page(source)
    if source_type == "rss_proxy":
        return collect_rss_proxy(source)
    if source_type in ("imap", "email_alert"):
        return collect_imap_alerts(source)
    raise SourceError("Unsupported source type: %s" % source.type)


def collect_rss(source: SourceConfig) -> List[Job]:
    """Direct RSS fetch via stdlib urllib. Use `rss_proxy` instead when the
    upstream is Cloudflare/anti-bot gated (e.g. DOU returns 403 to urllib but
    200 to Firecrawl's stealth proxies).
    """
    text = fetch_source_text(source)
    return _parse_rss_xml(text, source)


def collect_rss_proxy(source: SourceConfig) -> List[Job]:
    """RSS via Firecrawl's raw HTML fetch, then standard RSS XML parse.

    Used for RSS feeds behind Cloudflare/anti-bot that block direct urllib
    requests with HTTP 403. The Firecrawl `rawHtml` format returns the
    upstream XML untransformed (vs `markdown` which would render link text
    into the title field — see DOU regression where every job was titled
    'Відгукнутись на вакансію' / 'Apply for vacancy').
    """
    if not firecrawl_available():
        raise SourceError("rss_proxy source requires FIRECRAWL_API_KEY")
    try:
        raw = firecrawl_scrape_raw_html(source.url)
    except FirecrawlError as exc:
        raise SourceError("Firecrawl raw fetch failed for %s: %s" % (source.id, exc))
    return _parse_rss_xml(raw, source)


def _parse_rss_xml(text: str, source: SourceConfig) -> List[Job]:
    """Shared RSS 2.0 / Atom 1.0 parser used by both collect_rss and collect_rss_proxy."""
    root = ET.fromstring(text)
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    jobs = []
    for item in items:
        title = xml_text(item, ["title"])
        link = xml_text(item, ["link"])
        if not link:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
            if link_node is not None:
                link = link_node.attrib.get("href", "")
        description = xml_text(item, ["description", "summary", "content"])
        company = infer_company(title, description)
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=xml_text(item, ["guid", "id"]) or link,
                url=link,
                title=clean_title(title),
                company=company,
                location=infer_location(title + " " + description),
                remote_policy=infer_remote_policy(title + " " + description),
                description=strip_html(description),
                posted_at=parse_date(xml_text(item, ["pubDate", "updated", "published"])),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_remotive(source: SourceConfig) -> List[Job]:
    payload = json.loads(fetch_source_text(source))
    jobs = []
    for raw in payload.get("jobs", []):
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("url") or ""),
                url=raw.get("url", ""),
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                location=raw.get("candidate_required_location", ""),
                remote_policy="remote",
                salary_min=None,
                salary_max=None,
                currency=None,
                description=strip_html(raw.get("description", "")),
                posted_at=parse_date(raw.get("publication_date")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_remoteok(source: SourceConfig) -> List[Job]:
    payload = json.loads(fetch_source_text(source))
    jobs = []
    if isinstance(payload, list):
        rows = payload[1:] if payload and isinstance(payload[0], dict) and "legal" in payload[0] else payload
    else:
        rows = payload.get("jobs", [])
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("slug") or raw.get("url") or ""),
                url=raw.get("url") or raw.get("apply_url") or "",
                title=raw.get("position") or raw.get("title") or "",
                company=raw.get("company") or "",
                location=raw.get("location") or "",
                remote_policy="remote",
                salary_min=parse_int(raw.get("salary_min")),
                salary_max=parse_int(raw.get("salary_max")),
                currency=raw.get("currency") or "USD",
                description=strip_html(raw.get("description") or ""),
                posted_at=parse_date(raw.get("date") or raw.get("created_at")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_arbeitnow(source: SourceConfig) -> List[Job]:
    payload = json.loads(fetch_source_text(source))
    jobs = []
    for raw in payload.get("data", []):
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("slug") or raw.get("url") or ""),
                url=raw.get("url", ""),
                title=raw.get("title", ""),
                company=raw.get("company_name", ""),
                location=raw.get("location", ""),
                remote_policy="remote" if raw.get("remote") else "unknown",
                description=strip_html(raw.get("description", "")),
                posted_at=parse_date(str(raw.get("created_at") or "")),
            )
        )
    return [job for job in jobs if job.title and job.url]


HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"


def collect_hn(source: SourceConfig) -> List[Job]:
    """Hacker News jobs via the free Algolia API — no key, no scraping, no
    Firecrawl. The mode is chosen by ``source.query``:

    - ``"jobs"``: HN front-page job posts (tag=job), e.g. "Acme (YC X) Is Hiring".
    - ``"whoishiring"`` / ``"whoishiring-remote"``: top-level comments of the
      latest monthly "Ask HN: Who is hiring?" thread; each comment is one job
      post. The ``-remote`` variant keeps only comments that mention remote.

    Replaces the old ``community`` HTML scrapes of news.ycombinator.com /
    hnhiring.com, which needed Firecrawl to render and burned credits.
    """
    mode = (source.query or "jobs").strip().lower()
    if mode.startswith("whoishiring"):
        return _hn_whoishiring(source, remote_only=mode.endswith("remote"))
    return _hn_job_stories(source)


def _hn_get(path: str) -> Dict:
    payload = json.loads(fetch_text("%s/%s" % (HN_ALGOLIA_BASE, path), robots_check=False))
    return payload if isinstance(payload, dict) else {}


def _hn_company_from_title(title: str) -> str:
    """"Acme (YC W23) Is Hiring ..." / "Acme | Senior PM | Remote" -> "Acme"."""
    head = re.split(r"\s*(?:\(YC\b|\bis hiring\b|\||–|—| - )", title, maxsplit=1, flags=re.I)[0]
    return head.strip()[:80]


def _hn_job_stories(source: SourceConfig) -> List[Job]:
    payload = _hn_get("search_by_date?tags=job&hitsPerPage=50")
    jobs = []
    for hit in payload.get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        object_id = str(hit.get("objectID") or "")
        url = (hit.get("url") or "").strip() or "https://news.ycombinator.com/item?id=%s" % object_id
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id="hn-%s" % object_id,
                url=url,
                title=title,
                company=_hn_company_from_title(title),
                remote_policy=infer_remote_policy(title),
                description=title,
                posted_at=parse_date(hit.get("created_at")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def _hn_first_block(text_html: str) -> str:
    """First non-empty line of a HN comment — the job headline ("Co | Role |
    Location"). HN comments separate the headline with <p>/<br>, which the
    plain-text extractor would otherwise collapse into one run-on string."""
    for chunk in re.split(r"</p>|<p>|<br\s*/?>", text_html, flags=re.I):
        cleaned = strip_html(chunk).strip()
        if cleaned:
            return cleaned[:180]
    return strip_html(text_html).strip()[:120]


def _hn_whoishiring(source: SourceConfig, remote_only: bool = False) -> List[Job]:
    # search_by_date (newest first), NOT search (ranks by points — a 2020
    # "Who is hiring right now?" thread outranks the current month's).
    search = _hn_get("search_by_date?tags=story,author_whoishiring&hitsPerPage=8")
    thread_id = ""
    for hit in search.get("hits", []):
        if "who is hiring" in (hit.get("title") or "").lower():
            thread_id = str(hit.get("objectID") or "")
            break
    if not thread_id:
        return []
    thread = _hn_get("items/%s" % thread_id)
    jobs = []
    for child in thread.get("children") or []:
        text_html = child.get("text") or ""
        if not text_html:
            continue
        plain = strip_html(text_html).strip()
        if not plain:
            continue
        is_remote = "remote" in plain.lower()
        if remote_only and not is_remote:
            continue
        title = _hn_first_block(text_html)
        match = re.search(r'href="(https?://[^"]+)"', text_html)
        link = match.group(1) if match else "https://news.ycombinator.com/item?id=%s" % child.get("id")
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id="hn-%s" % child.get("id"),
                url=link,
                title=title,
                company=_hn_company_from_title(title),
                remote_policy="remote" if is_remote else "unknown",
                description=plain[:4000],
                posted_at=parse_date(child.get("created_at")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_generic_json(source: SourceConfig) -> List[Job]:
    payload = json.loads(fetch_source_text(source))
    if isinstance(payload, dict):
        rows = payload.get("jobs") or payload.get("data") or payload.get("results") or []
    else:
        rows = payload
    jobs = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        title = raw.get("title") or raw.get("position") or raw.get("name") or ""
        company = raw.get("company") or raw.get("company_name") or raw.get("organization") or ""
        url = raw.get("url") or raw.get("apply_url") or raw.get("job_url") or ""
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("slug") or url),
                url=url,
                title=title,
                company=company,
                location=raw.get("location") or raw.get("candidate_required_location") or "",
                remote_policy=infer_remote_policy(json.dumps(raw)[:2000]),
                salary_min=parse_int(raw.get("salary_min")),
                salary_max=parse_int(raw.get("salary_max")),
                currency=raw.get("currency"),
                description=strip_html(raw.get("description") or raw.get("body") or ""),
                posted_at=parse_date(raw.get("posted_at") or raw.get("created_at") or raw.get("date")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_ats(source: SourceConfig) -> List[Job]:
    parsed = urlparse(source.url)
    host = parsed.netloc.lower()
    parts = [part for part in parsed.path.split("/") if part]
    if "greenhouse.io" in host and parts:
        return collect_greenhouse(source, parts[0])
    if "lever.co" in host and parts:
        return collect_lever(source, parts[0])
    if "ashbyhq.com" in host and parts:
        return collect_ashby(source, parts[0])
    return collect_link_page(source)


def collect_greenhouse(source: SourceConfig, board: str) -> List[Job]:
    url = "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % board
    payload = json.loads(fetch_source_text(source, url))
    jobs = []
    for raw in payload.get("jobs", []):
        location = raw.get("location") or {}
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("absolute_url") or ""),
                url=raw.get("absolute_url") or "",
                title=raw.get("title") or "",
                company=source.name,
                location=location.get("name", "") if isinstance(location, dict) else str(location or ""),
                remote_policy=infer_remote_policy(json.dumps(raw)[:2000]),
                description=strip_html(raw.get("content") or ""),
                posted_at=parse_date(raw.get("updated_at")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_lever(source: SourceConfig, company: str) -> List[Job]:
    url = "https://api.lever.co/v0/postings/%s?mode=json" % company
    payload = json.loads(fetch_source_text(source, url))
    rows = payload if isinstance(payload, list) else []
    jobs = []
    for raw in rows:
        categories = raw.get("categories") or {}
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("hostedUrl") or ""),
                url=raw.get("hostedUrl") or raw.get("applyUrl") or "",
                title=raw.get("text") or "",
                company=source.name,
                location=categories.get("location", ""),
                remote_policy=infer_remote_policy(json.dumps(raw)[:2000]),
                description=strip_html(raw.get("descriptionPlain") or raw.get("description") or ""),
                posted_at=parse_date(raw.get("createdAt")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_ashby(source: SourceConfig, organization: str) -> List[Job]:
    url = "https://api.ashbyhq.com/posting-api/job-board/%s" % organization
    payload = json.loads(fetch_source_text(source, url, timeout=45, attempts=2))
    jobs = []
    for raw in payload.get("jobs", []):
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=str(raw.get("id") or raw.get("jobUrl") or ""),
                url=raw.get("jobUrl") or raw.get("applyUrl") or "",
                title=raw.get("title") or "",
                company=source.name,
                location=raw.get("locationName") or "",
                remote_policy=infer_remote_policy(json.dumps(raw)[:2000]),
                description=strip_html(raw.get("descriptionHtml") or raw.get("description") or ""),
                posted_at=parse_date(raw.get("publishedAt") or raw.get("updatedAt")),
            )
        )
    return [job for job in jobs if job.title and job.url]


def collect_link_page(source: SourceConfig) -> List[Job]:
    text, used_firecrawl = fetch_link_page_text(source)
    parser = HTMLLinkExtractor()
    parser.feed(text)
    links = parser.links + markdown_links(text)
    job_links = [(href, link_text) for href, link_text in links if clean_title(link_text) and looks_like_job_link(link_text, href)]
    if not used_firecrawl and len(job_links) < 2 and len(text.encode("utf-8")) < 8192 and looks_like_spa_shell(text):
        raise SourceError("Source appears to be a JavaScript SPA - not supported")
    jobs = []
    seen = set()
    for href, link_text in job_links:
        title = clean_title(strip_markdown(link_text))
        url = urljoin(source.url, href)
        if url in seen:
            continue
        description = surrounding_text(text, title, 1200)
        job = link_page_job(source, url, title, description, text)
        if not job:
            continue
        seen.add(url)
        jobs.append(job)
        if len(jobs) >= 30:
            break
    return jobs


def link_page_job(source: SourceConfig, url: str, title: str, description: str, page_text: str) -> Optional[Job]:
    parsed = urlparse(url)
    if is_yc_source(source):
        company = company_from_yc_job_url(parsed.path)
        if not company:
            return None
    elif is_dou_source(source):
        company = company_from_dou_job_url(parsed.path)
        if not company:
            return None
    elif source.type == "community":
        company = infer_company(title, page_text[:4000])
    else:
        company = source.name
    if is_weworkremotely_source(source, parsed.netloc):
        title = strip_company_prefix(title, company)
    return Job(
        source_id=source.id,
        source_name=source.name,
        external_id=url,
        url=url,
        title=title[:180],
        company=company,
        location=infer_location(title + " " + description),
        remote_policy=infer_remote_policy(title + " " + description),
        description=strip_html(strip_markdown(description or title))[:4000],
    )


def fetch_link_page_text(source: SourceConfig) -> tuple:
    try:
        return fetch_source_text(source), False
    except SourceError as exc:
        if source.type != "community" or not firecrawl_available():
            raise
        try:
            validate_safe_url(source.url)
            result = firecrawl_scrape_markdown(source.url)
            log_context(
                LOGGER,
                logging.INFO,
                "community_source_firecrawl_fetch_succeeded",
                source_id=source.id,
                url=source.url,
                status=result.get("status"),
            )
            return result["text"], True
        except (FirecrawlError, SourceError) as firecrawl_exc:
            log_context(
                LOGGER,
                logging.WARNING,
                "community_source_firecrawl_fetch_failed",
                source_id=source.id,
                url=source.url,
                direct_error=str(exc),
                firecrawl_error=str(firecrawl_exc),
            )
            raise exc


def markdown_links(text: str) -> List[tuple]:
    links = []
    for match in re.finditer(r"(?<!!)\[([^\]]{2,300})\]\((https?://[^)\s]+)\)", text or ""):
        title = strip_markdown(match.group(1))
        url = match.group(2).rstrip(".,;]")
        if title and url:
            links.append((url, title))
    return links


def strip_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text or "")
    text = re.sub(r"[*_`]+", "", text)
    return clean_title(text)


def looks_like_job_link(title: str, href: str) -> bool:
    title_lower = title.lower()
    href_lower = href.lower()
    if is_navigation_link_title(title_lower):
        return False
    title_matches = any(
        token in title_lower
        for token in (
            "job",
            "career",
            "hiring",
            "product",
            "engineer",
            "manager",
            "designer",
            "developer",
            "remote",
            "ai",
            "llm",
        )
    )
    detail_url_matches = any(token in href_lower for token in ("/jobs/", "/careers/", "/positions/", "/job/"))
    detail_url_matches = detail_url_matches or ("/companies/" in href_lower and "/vacancies/" in href_lower)
    return title_matches or detail_url_matches


def is_yc_source(source: SourceConfig) -> bool:
    haystack = "%s %s %s" % (source.id, source.name, source.url)
    return "ycombinator.com" in haystack.lower() or source.id.lower().startswith("yc-")


def is_dou_source(source: SourceConfig) -> bool:
    haystack = "%s %s %s" % (source.id, source.name, source.url)
    return "jobs.dou.ua" in haystack.lower() or source.id.lower().startswith("dou-")


def is_weworkremotely_source(source: SourceConfig, host: str = "") -> bool:
    haystack = "%s %s %s %s" % (source.id, source.name, source.url, host)
    return "weworkremotely.com" in haystack.lower() or source.id.lower().startswith(("wwr-", "weworkremotely"))


def company_from_yc_job_url(path: str) -> str:
    match = re.search(r"/companies/([^/]+)/jobs/[^/?#]+", path or "", re.IGNORECASE)
    return titleize_slug(match.group(1)) if match else ""


def company_from_dou_job_url(path: str) -> str:
    match = re.search(r"/companies/([^/]+)/vacancies/[^/?#]+", path or "", re.IGNORECASE)
    return titleize_slug(match.group(1)) if match else ""


def titleize_slug(value: str) -> str:
    words = [word for word in re.split(r"[-_]+", value or "") if word]
    small_words = {"a", "an", "and", "for", "in", "of", "or", "the", "to"}
    titled = []
    for index, word in enumerate(words):
        lower = word.lower()
        if index and lower in small_words:
            titled.append(lower)
        else:
            titled.append(lower.upper() if len(lower) <= 3 and lower in {"ai", "ml", "ui", "ux", "api"} else lower.capitalize())
    return " ".join(titled)


def strip_company_prefix(title: str, company: str) -> str:
    title = clean_title(title)
    company = clean_title(company)
    if not title or not company:
        return title
    pattern = r"^%s\s*:\s*(.+)$" % re.escape(company)
    match = re.match(pattern, title, re.IGNORECASE)
    if match:
        return clean_title(match.group(1))
    return title


def is_navigation_link_title(title_lower: str) -> bool:
    compact = clean_title(title_lower).lower()
    if compact in {"rss", "remote", "віддалено", "без досвіду"}:
        return True
    if re.fullmatch(r"<?\s*\d+\s*(?:року|роки|years?)", compact):
        return True
    if re.fullmatch(r"\d+…\d+\s*(?:роки|років|years?)", compact):
        return True
    if re.fullmatch(r"\d+\+\s*(?:років|years?)", compact):
        return True
    return compact in {"київ", "львів", "дніпро", "odesa", "warsaw", "berlin", "london"}


def looks_like_spa_shell(html: str) -> bool:
    lower = (html or "").lower()
    return (
        bool(re.search(r'<div[^>]+id=["\'](?:root|__next|app)["\'][^>]*>\s*</div>', lower))
        or ("<script" in lower and len(strip_html(html)) < 200)
    )


def collect_imap_alerts(source: SourceConfig) -> List[Job]:
    """Fetch IMAP messages, prioritizing newest UIDs (highest first).

    Why newest-first: Gmail label folders assign UIDs at *labeling time*, not
    *receipt time*. When a user bulk-applies a label to historical
    conversations (e.g. via "Also apply filter to N matching conversations"),
    those messages get fresh UIDs interleaved with current arrivals. The old
    high-water-mark scheme would drain UIDs in ascending order, so recent
    alerts could sit behind years of archive emails. We now skip per UID via
    the `imap_processed_uids` tracker (schema v16) and pick the newest
    unprocessed UIDs each run.

    Cap of 50 UIDs/run is preserved to bound wall time (each FETCH is a
    network round-trip; 50 ≈ 25s).
    """
    host = os.getenv("EMAIL_IMAP_HOST", "")
    username = os.getenv("EMAIL_IMAP_USERNAME", "")
    password = os.getenv("EMAIL_IMAP_PASSWORD", "")
    folder = os.getenv("EMAIL_IMAP_FOLDER", "job-alerts")
    if not host or not username or not password:
        raise SourceError("IMAP source configured but EMAIL_IMAP_HOST/USERNAME/PASSWORD are missing")

    processed_uids_loader = getattr(source, "processed_uids_loader", None)
    processed_uids_recorder = getattr(source, "processed_uids_recorder", None)
    already_processed = set()
    if processed_uids_loader:
        try:
            already_processed = set(int(u) for u in processed_uids_loader(source.id))
        except Exception as exc:
            log_context(LOGGER, logging.WARNING, "imap_processed_uids_load_failed", source_id=source.id, error=str(exc))
            already_processed = set()
    # Legacy HWM compat: any UID at or below source.imap_last_uid is treated as
    # already processed even if not present in the per-UID tracker. This keeps
    # the old per-source-query test path working (it never used the tracker).
    legacy_hwm = int(source.imap_last_uid or 0)
    if legacy_hwm > 0:
        already_processed.update(range(1, legacy_hwm + 1))
    # Date cutoff: ignore emails older than JOBHUNTER_EMAIL_MAX_AGE_DAYS (default 30).
    # Implemented as an IMAP SEARCH `SINCE` filter so old emails are excluded
    # server-side — bulk-labeled historical archives (e.g. 2022 Djinni alerts
    # that get a label applied in 2026) stay out of our scope without being
    # individually fetched-and-discarded. 0 = no cutoff (process everything).
    try:
        max_age_days = int(os.getenv("JOBHUNTER_EMAIL_MAX_AGE_DAYS", "30"))
    except ValueError:
        max_age_days = 30
    since_date = None
    if max_age_days > 0:
        since_date = (datetime.utcnow() - timedelta(days=max_age_days)).strftime("%d-%b-%Y")

    mailbox = imaplib.IMAP4_SSL(host)
    try:
        mailbox.login(username, password)
        mailbox.select(folder, readonly=True)
        # Build IMAP SEARCH preserving source.query, optional SINCE cutoff, and
        # legacy UID range when source.query is set.
        if source.query:
            search_args = ["SEARCH", None, "UID", "%s:*" % (legacy_hwm + 1)]
            if since_date:
                search_args.extend(["SINCE", since_date])
            search_args.extend(parse_imap_query(source.query))
            status, ids = mailbox.uid(*search_args)
        elif since_date:
            status, ids = mailbox.uid("SEARCH", None, "SINCE", since_date)
        else:
            status, ids = mailbox.uid("SEARCH", None, "ALL")
        if status != "OK" or not ids or not ids[0]:
            return []
        all_uids = set()
        for token in ids[0].split():
            try:
                all_uids.add(int(token))
            except ValueError:
                continue
        unprocessed = sorted(all_uids - already_processed, reverse=True)
        target_uids = unprocessed[:50]
        if not target_uids:
            source.last_seen_uid = source.imap_last_uid or 0
            return []
        jobs = []
        max_uid = source.imap_last_uid or 0
        processed_now = []
        for uid_int in target_uids:
            max_uid = max(max_uid, uid_int)
            status, data = mailbox.uid("FETCH", str(uid_int), "(RFC822)")
            if status != "OK" or not data:
                continue
            try:
                raw_bytes = data[0][1] if (data and isinstance(data[0], tuple) and len(data[0]) > 1) else b""
            except Exception:
                raw_bytes = b""
            if not raw_bytes:
                # Still record as processed so we don't refetch on every run.
                processed_now.append(uid_int)
                continue
            message = email.message_from_bytes(raw_bytes)
            persist_email_sample(source, message, str(uid_int))
            raw_id, raw_inserted = persist_raw_email(source, message, str(uid_int))
            if raw_id:
                log_context(LOGGER, logging.DEBUG, "email_alert_raw_saved", source_id=source.id, email_alert_id=raw_id, inserted=raw_inserted, uid=uid_int)
            processed_now.append(uid_int)
        if processed_now and processed_uids_recorder:
            try:
                processed_uids_recorder(source.id, processed_now)
            except Exception as exc:
                log_context(LOGGER, logging.WARNING, "imap_processed_uids_record_failed", source_id=source.id, error=str(exc))
        log_context(
            LOGGER,
            logging.INFO,
            "imap_collection_summary",
            source_id=source.id,
            folder=folder,
            total_in_folder=len(all_uids),
            already_processed=len(already_processed),
            unprocessed_total=len(unprocessed),
            processed_this_run=len(processed_now),
            newest_uid_processed=max(processed_now) if processed_now else None,
            oldest_uid_processed=min(processed_now) if processed_now else None,
        )
        source.last_seen_uid = max_uid
        return jobs
    finally:
        try:
            mailbox.close()
        except Exception:
            pass
        mailbox.logout()


def persist_raw_email(source: SourceConfig, message, sample_id: str = "") -> tuple:
    writer = getattr(source, "raw_email_writer", None)
    if not writer:
        return None, False
    html, text = email_body_parts(message)
    message_id = message.get("Message-ID") or sample_id or ""
    received_at = parse_date(message.get("Date")) or utc_now_iso_text()
    return writer(
        source.id,
        message_id,
        decoded_header(message.get("From", "")),
        decoded_header(message.get("Subject", "")),
        received_at,
        html,
        text,
    )


def decoded_header(value: str) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return str(value or "")


def persist_email_sample(source: SourceConfig, message, sample_id: str = "") -> Optional[Path]:
    try:
        subject = str(email.header.make_header(email.header.decode_header(message.get("Subject", ""))))
        sender = str(email.header.make_header(email.header.decode_header(message.get("From", ""))))
        body = email_body(message)
        if not body.strip():
            return None
        sender_address = parseaddr(sender)[1] or sender or source.id
        message_id = message.get("Message-ID") or sample_id or datetime.utcnow().isoformat()
        directory = email_samples_dir() / slug_for_path(sender_address, "unknown-sender")
        directory.mkdir(parents=True, exist_ok=True)
        filename = "%s-%s.html" % (
            slug_for_path(subject, "no-subject")[:80],
            slug_for_path(message_id, sample_id or "message")[:48],
        )
        path = directory / filename
        path.write_text(body[:EMAIL_SAMPLE_MAX_BYTES], encoding="utf-8")
        trim_email_samples(directory)
        log_context(LOGGER, logging.DEBUG, "email_sample_saved", source_id=source.id, path=str(path))
        return path
    except Exception as exc:
        log_context(LOGGER, logging.WARNING, "email_sample_save_failed", source_id=source.id, error=str(exc))
        return None


def email_samples_dir() -> Path:
    return Path(os.getenv("JOBHUNTER_EMAIL_SAMPLES_DIR", str(Path(os.getenv("JOBHUNTER_DATA_DIR", "data")) / "email_samples")))


def trim_email_samples(directory: Path) -> None:
    keep = max(1, EMAIL_SAMPLE_KEEP_PER_SENDER)
    files = sorted((path for path in directory.glob("*.html") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    for old_path in files[keep:]:
        try:
            old_path.unlink()
        except OSError:
            pass


def slug_for_path(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return text[:120] or fallback


def surrounding_text(body: str, needle: str, size: int) -> str:
    plain = strip_html(body)
    idx = plain.lower().find((needle or "").lower())
    if idx < 0:
        return plain[:size]
    start = max(0, idx - size // 2)
    return plain[start : start + size]


def utc_now_iso_text() -> str:
    return utc_now_iso()


def email_body_parts(message) -> tuple:
    html_parts = []
    text_parts = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            else:
                decoded = str(part.get_payload() or "")
            if content_type == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)
        return "\n".join(html_parts), "\n".join(text_parts)
    payload = message.get_payload(decode=True)
    if payload:
        decoded = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    else:
        decoded = str(message.get_payload() or "")
    content_type = message.get_content_type()
    if content_type == "text/html":
        return decoded, ""
    return "", decoded


def email_body(message) -> str:
    html, text = email_body_parts(message)
    return "\n".join(part for part in (html, text) if part)


# Detector helper: catalog of known job-posting URL patterns we can deterministically
# count in an email body. Used by audit_email_extraction to decide whether the
# Codex-extracted job count for an email is suspiciously low vs the link count.
# Add new patterns conservatively — false positives here cause unnecessary
# re-extraction (idempotent, but wastes Codex turns); false negatives let
# under-extracted emails go undetected.
JOB_URL_PATTERNS = [
    re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.IGNORECASE),
    re.compile(r"boards\.greenhouse\.io/[^/\"?>]+/jobs/(\d+)", re.IGNORECASE),
    re.compile(r"jobs\.lever\.co/[^/\"?>]+/([a-f0-9-]{30,})", re.IGNORECASE),
    re.compile(r"ashbyhq\.com/[^/\"?>]+/([a-f0-9-]{30,})", re.IGNORECASE),
    re.compile(r"wellfound\.com/jobs/(\d+)", re.IGNORECASE),
    re.compile(r"angel\.co/jobs/(\d+)", re.IGNORECASE),
    re.compile(r"djinni\.co/(?:[a-z]+/)?jobs/([a-z0-9-]+)", re.IGNORECASE),
    re.compile(r"weworkremotely\.com/(?:remote-jobs|listings)/([a-z0-9-]+)", re.IGNORECASE),
]


def count_known_job_links_in_html(html: str) -> int:
    """Count distinct job-posting URLs across known source patterns in a raw email.
    A coarse lower-bound on how many jobs that email actually contains."""
    if not html:
        return 0
    found = set()
    for pattern in JOB_URL_PATTERNS:
        for match in pattern.findall(html):
            found.add((pattern.pattern, match))
    return len(found)


def enrich_job_from_url(job_row) -> Dict:
    url = row_get(job_row, "url")
    snippet = row_get(job_row, "description")
    if not url:
        return {"description": snippet or "", "enrich_status": "skipped"}
    try:
        html = fetch_text(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; Jobhunter/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=10,
            robots_check=False,
        )
    except Exception as exc:
        log_context(LOGGER, logging.WARNING, "job_enrichment_fetch_failed", url=url, error=str(exc))
        return {"description": snippet or "", "enrich_status": "failed", "error": str(exc)}
    fields = (
        extract_greenhouse_job(url, html)
        or extract_lever_job(url, html)
        or extract_ashby_job(url, html)
        or extract_linkedin_job(url, html)
        or extract_generic_job_posting(html)
        or extract_open_graph_job(html)
    )
    if not fields:
        fields = {"description": strip_html(html)[:4000]}
    if snippet and len(fields.get("description") or "") < len(snippet):
        fields["description"] = snippet
    fields["enrich_status"] = "enriched" if fields.get("description") else "failed"
    return fields


def row_get(row, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def extract_greenhouse_job(url: str, html: str) -> Dict:
    parsed = urlparse(url or "")
    if "greenhouse.io" not in parsed.netloc.lower():
        return {}
    data = extract_generic_job_posting(html)
    if data:
        return data
    return extract_open_graph_job(html)


def extract_lever_job(url: str, html: str) -> Dict:
    parsed = urlparse(url or "")
    if "lever.co" not in parsed.netloc.lower():
        return {}
    data = extract_generic_job_posting(html)
    if data:
        return data
    return extract_open_graph_job(html)


def extract_ashby_job(url: str, html: str) -> Dict:
    parsed = urlparse(url or "")
    if "ashbyhq.com" not in parsed.netloc.lower():
        return {}
    data = extract_generic_job_posting(html)
    if data:
        return data
    return extract_open_graph_job(html)


def extract_linkedin_job(url: str, html: str) -> Dict:
    parsed = urlparse(url or "")
    if "linkedin.com" not in parsed.netloc.lower() or "/jobs/view/" not in parsed.path.lower() and "/comm/jobs/view/" not in parsed.path.lower():
        return {}
    patterns = [
        r'<div[^>]+class=["\'][^"\']*description__text[^"\']*["\'][^>]*>(?P<body>.*?)</div>\s*</div>',
        r'<div[^>]+class=["\'][^"\']*show-more-less-html__markup[^"\']*["\'][^>]*>(?P<body>.*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html or "", re.IGNORECASE | re.DOTALL)
        if match:
            description = strip_html(html_lib.unescape(match.group("body")))
            if description:
                return {"description": description[:4000], "remote_policy": infer_remote_policy(description), "location": infer_location(description)}
    return extract_generic_job_posting(html) or extract_open_graph_job(html)


def extract_generic_job_posting(html: str) -> Dict:
    metadata = html_metadata(html)
    for raw in metadata.ld_json:
        for item in flatten_json_ld(raw):
            if not is_job_posting(item):
                continue
            fields = {}
            description = strip_html(str(item.get("description") or ""))
            if description:
                fields["description"] = description[:4000]
                fields["remote_policy"] = infer_remote_policy(description)
            title = clean_title(str(item.get("title") or ""))
            if title:
                fields["title"] = title[:180]
            company = organization_name(item.get("hiringOrganization"))
            if company:
                fields["company"] = company
            location = job_location_text(item.get("jobLocation") or item.get("applicantLocationRequirements"))
            if location:
                fields["location"] = location
            salary = salary_fields(item.get("baseSalary"))
            fields.update(salary)
            if fields:
                return fields
    return {}


def extract_open_graph_job(html: str) -> Dict:
    metadata = html_metadata(html)
    description = metadata.meta.get("og:description") or metadata.meta.get("description") or ""
    title = metadata.meta.get("og:title") or metadata.meta.get("twitter:title") or ""
    fields = {}
    if description:
        fields["description"] = strip_html(html_lib.unescape(description))[:4000]
        fields["remote_policy"] = infer_remote_policy(description)
    if title:
        fields["title"] = clean_title(html_lib.unescape(title))[:180]
    return fields


def html_metadata(html: str) -> HTMLMetadataExtractor:
    parser = HTMLMetadataExtractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    return parser


def flatten_json_ld(raw: str) -> List[Dict]:
    try:
        parsed = json.loads(html_lib.unescape(raw))
    except json.JSONDecodeError:
        return []
    items = []
    stack = parsed if isinstance(parsed, list) else [parsed]
    while stack:
        item = stack.pop(0)
        if isinstance(item, dict):
            items.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
        elif isinstance(item, list):
            stack.extend(item)
    return items


def is_job_posting(item: Dict) -> bool:
    kind = item.get("@type") or item.get("type") or ""
    if isinstance(kind, list):
        return any(str(value).lower() == "jobposting" for value in kind)
    return str(kind).lower() == "jobposting"


def organization_name(value) -> str:
    if isinstance(value, dict):
        return clean_title(str(value.get("name") or ""))
    if isinstance(value, str):
        return clean_title(value)
    return ""


def job_location_text(value) -> str:
    if isinstance(value, list):
        return "; ".join(part for part in (job_location_text(item) for item in value) if part)[:300]
    if not isinstance(value, dict):
        return clean_title(str(value or ""))
    address = value.get("address")
    if isinstance(address, dict):
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        text = ", ".join(str(part) for part in parts if part)
        if text:
            return clean_title(text)
    return clean_title(str(value.get("name") or value.get("address") or ""))


def salary_fields(value) -> Dict:
    if not isinstance(value, dict):
        return {}
    currency = value.get("currency") or value.get("salaryCurrency")
    raw_value = value.get("value")
    if isinstance(raw_value, dict):
        min_value = parse_int(raw_value.get("minValue") or raw_value.get("min"))
        max_value = parse_int(raw_value.get("maxValue") or raw_value.get("max"))
        unit_currency = raw_value.get("currency") or raw_value.get("salaryCurrency")
    else:
        min_value = parse_int(raw_value)
        max_value = None
        unit_currency = None
    fields = {}
    if min_value is not None:
        fields["salary_min"] = min_value
    if max_value is not None:
        fields["salary_max"] = max_value
    if currency or unit_currency:
        fields["currency"] = str(currency or unit_currency)[:12]
    return fields


def extract_urls(text: str) -> List[str]:
    candidates = re.findall(r"https?://[^\s<>'\")]+", text or "")
    cleaned = []
    for url in candidates:
        url = url.rstrip(".,;]")
        if url not in cleaned:
            cleaned.append(url)
    return cleaned


def xml_text(item: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        found = item.find(name)
        if found is not None and found.text:
            return found.text.strip()
        found = item.find("{http://www.w3.org/2005/Atom}%s" % name)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def clean_title(title: str) -> str:
    return " ".join((title or "").replace("\n", " ").split())


def infer_company(title: str, description: str) -> str:
    title = strip_html(title or "")
    separator_policies = [
        (": ", "left"),
        (" at ", "right"),
        (" - ", "right"),
        (" | ", "right"),
    ]
    for separator, side in separator_policies:
        if separator in title:
            parts = title.split(separator, 1)
            part = parts[0] if side == "left" else parts[-1]
            part = part.strip()
            if 2 <= len(part) <= 80:
                return part
    match = re.search(r"(?:at|company:)\s+([A-Z][A-Za-z0-9 .&-]{2,60})", description or "")
    if match and is_plausible_company_match(match.group(1)):
        return match.group(1).strip()
    return "Unknown company"


def is_plausible_company_match(value: str) -> bool:
    candidate = " ".join((value or "").split()).strip(" .,-")
    if len(candidate) < 3:
        return False
    if candidate.split()[0] in {"You", "We", "This", "That", "It", "There", "Here", "Our", "Their"}:
        return False
    if re.search(r"\s+(is|will|can|may|should|are|were|has|have)\s+", candidate, re.IGNORECASE):
        return False
    return True


def infer_location(text: str) -> str:
    lower = (text or "").lower()
    if "remote" in lower:
        if "europe" in lower or "emea" in lower:
            return "Remote, Europe/EMEA"
        if "asia" in lower or "apac" in lower:
            return "Remote, Asia/APAC"
        if "worldwide" in lower or "anywhere" in lower:
            return "Remote, worldwide"
        return "Remote"
    return ""


def infer_remote_policy(text: str) -> str:
    lower = (text or "").lower()
    if "remote" in lower or "work from anywhere" in lower:
        return "remote"
    if "hybrid" in lower:
        return "hybrid"
    if "onsite" in lower or "on-site" in lower:
        return "onsite"
    return "unknown"


def parse_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def validate_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SourceError("Unsafe URL scheme: %s" % url)
    host = parsed.hostname
    if not host:
        raise SourceError("Missing URL host: %s" % url)
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SourceError("DNS error for %s: %s" % (host, exc))
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise SourceError("Unsafe resolved IP for %s: %s" % (host, ip))


def wait_for_host_rate_limit(url: str) -> None:
    host = urlparse(url).hostname or ""
    if not host:
        return
    now = time.time()
    last = HOST_LAST_FETCH.get(host, 0)
    delay = 2.0 - (now - last)
    if delay > 0:
        time.sleep(delay)
    HOST_LAST_FETCH[host] = time.time()


def robots_allowed(url: str) -> bool:
    parsed = urlparse(url)
    robots_url = urljoin("%s://%s" % (parsed.scheme, parsed.netloc), "/robots.txt")
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        parser.read()
    except Exception:
        return True
    return parser.can_fetch(DEFAULT_HEADERS["User-Agent"], url)


def parse_imap_query(query: str) -> List[str]:
    if not query:
        return []
    # Keep this deliberately small. Operators like: FROM "x", SUBJECT "jobs".
    tokens = re.findall(r'"[^"]+"|\S+', query)
    return [token.strip() for token in tokens if token.strip()]
