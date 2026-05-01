import email
import email.header
import imaplib
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional

from .models import Job, SourceConfig


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
    value = value.strip()
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
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    request = urllib.request.Request(url, headers=merged_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
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
    if source_type == "imap":
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
        status, ids = mailbox.search(None, "UNSEEN")
        if status != "OK":
            return []
        jobs = []
        for message_id in ids[0].split()[:50]:
            status, data = mailbox.fetch(message_id, "(RFC822)")
            if status != "OK" or not data:
                continue
            message = email.message_from_bytes(data[0][1])
            jobs.extend(jobs_from_email(source, message))
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
    for separator in [" at ", " - ", " | "]:
        if separator in title:
            part = title.split(separator)[-1].strip()
            if 2 <= len(part) <= 80:
                return part
    match = re.search(r"(?:at|company:)\s+([A-Z][A-Za-z0-9 .&-]{2,60})", description or "")
    if match:
        return match.group(1).strip()
    return "Unknown company"


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
