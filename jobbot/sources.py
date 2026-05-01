import email
import email.header
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
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from .logging_setup import log_context
from .models import Job, SourceConfig

LOGGER = logging.getLogger(__name__)
MAX_BYTES = int(os.getenv("JOBBOT_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))
CHECK_ROBOTS = os.getenv("JOBBOT_CHECK_ROBOTS", "0").strip().lower() in ("1", "true", "yes", "on")
HOST_LAST_FETCH: Dict[str, float] = {}


DEFAULT_HEADERS = {
    "User-Agent": "jobhunter-openclaw-jobbot/0.1 (+human-in-the-loop; contact: local-user)",
    "Accept": "application/json, application/rss+xml, application/xml, text/xml, text/html;q=0.8",
}


class SourceError(RuntimeError):
    pass


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


def fetch_text(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 30) -> str:
    validate_safe_url(url)
    wait_for_host_rate_limit(url)
    if CHECK_ROBOTS and not robots_allowed(url):
        raise SourceError("Robots.txt disallows %s" % url)
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, headers=merged_headers)
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
        raise SourceError("URL error fetching %s: %s" % (url, exc.reason))


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
    if source_type == "ats":
        return collect_ats(source)
    if source_type == "community":
        return collect_link_page(source)
    if source_type in ("imap", "email_alert"):
        return collect_imap_alerts(source)
    raise SourceError("Unsupported source type: %s" % source.type)


def collect_rss(source: SourceConfig) -> List[Job]:
    text = fetch_text(source.url, source.headers)
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
    payload = json.loads(fetch_text(source.url, source.headers))
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
    payload = json.loads(fetch_text(source.url, source.headers))
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
    payload = json.loads(fetch_text(source.url, source.headers))
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


def collect_generic_json(source: SourceConfig) -> List[Job]:
    payload = json.loads(fetch_text(source.url, source.headers))
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
    payload = json.loads(fetch_text(url, source.headers))
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
    payload = json.loads(fetch_text(url, source.headers))
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
    payload = json.loads(fetch_text(url, source.headers))
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
    html = fetch_text(source.url, source.headers)
    parser = HTMLLinkExtractor()
    parser.feed(html)
    job_links = [(href, text) for href, text in parser.links if clean_title(text) and looks_like_job_link(text, href)]
    if len(job_links) < 2 and len(html.encode("utf-8")) < 8192 and looks_like_spa_shell(html):
        raise SourceError("Source appears to be a JavaScript SPA - not supported")
    jobs = []
    seen = set()
    for href, text in job_links:
        title = clean_title(text)
        url = urljoin(source.url, href)
        if url in seen:
            continue
        seen.add(url)
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=url,
                url=url,
                title=title[:180],
                company=infer_company(title, html[:2000]) if source.type == "community" else source.name,
                location=infer_location(title),
                remote_policy=infer_remote_policy(title),
                description=strip_html(title),
            )
        )
        if len(jobs) >= 30:
            break
    return jobs


def looks_like_job_link(title: str, href: str) -> bool:
    text = "%s %s" % (title.lower(), href.lower())
    return any(
        token in text
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


def looks_like_spa_shell(html: str) -> bool:
    lower = (html or "").lower()
    return (
        bool(re.search(r'<div[^>]+id=["\'](?:root|__next|app)["\'][^>]*>\s*</div>', lower))
        or ("<script" in lower and len(strip_html(html)) < 200)
    )


def collect_imap_alerts(source: SourceConfig) -> List[Job]:
    host = os.getenv("EMAIL_IMAP_HOST", "")
    username = os.getenv("EMAIL_IMAP_USERNAME", "")
    password = os.getenv("EMAIL_IMAP_PASSWORD", "")
    folder = os.getenv("EMAIL_IMAP_FOLDER", "job-alerts")
    if not host or not username or not password:
        raise SourceError("IMAP source configured but EMAIL_IMAP_HOST/USERNAME/PASSWORD are missing")

    mailbox = imaplib.IMAP4_SSL(host)
    try:
        mailbox.login(username, password)
        mailbox.select(folder, readonly=True)
        search_args = ["UID", "SEARCH", None, "UID", "%s:*" % (int(source.imap_last_uid or 0) + 1)]
        if source.query:
            search_args.extend(parse_imap_query(source.query))
        status, ids = mailbox.uid(*search_args[1:])
        if status != "OK":
            return []
        jobs = []
        max_uid = source.imap_last_uid or 0
        for message_id in ids[0].split()[:50]:
            try:
                uid_int = int(message_id)
                max_uid = max(max_uid, uid_int)
            except ValueError:
                pass
            status, data = mailbox.uid("FETCH", message_id, "(RFC822)")
            if status != "OK" or not data:
                continue
            message = email.message_from_bytes(data[0][1])
            jobs.extend(jobs_from_email(source, message))
        source.last_seen_uid = max_uid
        return jobs
    finally:
        try:
            mailbox.close()
        except Exception:
            pass
        mailbox.logout()


def jobs_from_email(source: SourceConfig, message) -> List[Job]:
    subject = str(email.header.make_header(email.header.decode_header(message.get("Subject", ""))))
    sender = str(email.header.make_header(email.header.decode_header(message.get("From", ""))))
    body = email_body(message)
    urls = extract_urls(body)
    jobs = []
    for idx, url in enumerate(urls[:10]):
        title = subject
        company = infer_company(subject + " " + sender, body)
        jobs.append(
            Job(
                source_id=source.id,
                source_name=source.name,
                external_id=message.get("Message-ID", "") + str(idx),
                url=url,
                title=clean_title(title),
                company=company,
                location=infer_location(body),
                remote_policy=infer_remote_policy(body),
                description=strip_html(body[:4000]),
                posted_at=parse_date(message.get("Date")),
            )
        )
    return jobs


def email_body(message) -> str:
    if message.is_multipart():
        parts = []
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)
    payload = message.get_payload(decode=True)
    if payload:
        return payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    return str(message.get_payload() or "")


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
