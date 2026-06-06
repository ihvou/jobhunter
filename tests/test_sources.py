import json
import unittest
from unittest import mock

from jobhunter.models import SourceConfig
from jobhunter import sources as source_module
from jobhunter.sources import HN_ALGOLIA_BASE, SourceError, collect_ashby, collect_ats, collect_hn, collect_link_page, collect_rss, collect_rss_proxy, enrich_job_from_url, fetch_source_text, infer_company, strip_html, validate_safe_url


RSS = """<?xml version="1.0"?>
<rss>
  <channel>
    <item>
      <title>Senior Python Engineer at ExampleCo</title>
      <link>https://example.com/jobs/1</link>
      <description><![CDATA[Remote role building AI products with Python.]]></description>
      <guid>job-1</guid>
      <pubDate>Fri, 01 May 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


class SourceTests(unittest.TestCase):
    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Hello <b>world</b></p>"), "Hello world")

    def test_collect_rss(self):
        source = SourceConfig(id="rss", name="RSS", type="rss", url="https://example.com/rss")
        with mock.patch("jobhunter.sources.fetch_text", return_value=RSS):
            jobs = collect_rss(source)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "ExampleCo")
        self.assertEqual(jobs[0].remote_policy, "remote")

    def test_collect_rss_proxy_uses_firecrawl_raw_html_and_extracts_real_titles(self):
        """Regression for DOU broken-titles: when fetched via Firecrawl markdown the
        link text is the apply-button label, not the title. rss_proxy must fetch
        raw XML so the RSS <title> element is preserved.
        """
        dou_rss_xml = """<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0">
          <channel>
            <title>DOU Product Manager Jobs</title>
            <link>https://jobs.dou.ua/vacancies/</link>
            <item>
              <title>Senior Product Manager</title>
              <link>https://jobs.dou.ua/companies/oriented-as/vacancies/359298/</link>
              <description><![CDATA[Oriented Soft. Take ownership of our core products. Remote.]]></description>
              <pubDate>Sun, 25 May 2026 12:00:00 +0000</pubDate>
              <guid>https://jobs.dou.ua/companies/oriented-as/vacancies/359298/</guid>
            </item>
            <item>
              <title>Lead Product Manager (FORMA)</title>
              <link>https://jobs.dou.ua/companies/universe/vacancies/359244/</link>
              <description><![CDATA[Universe. Lead PM for forma product.]]></description>
              <pubDate>Sat, 24 May 2026 12:00:00 +0000</pubDate>
              <guid>https://jobs.dou.ua/companies/universe/vacancies/359244/</guid>
            </item>
          </channel>
        </rss>"""
        source = SourceConfig(id="dou", name="DOU", type="rss_proxy", url="https://jobs.dou.ua/vacancies/feeds/?category=Product+Manager")
        with mock.patch("jobhunter.sources.firecrawl_available", return_value=True), \
             mock.patch("jobhunter.sources.firecrawl_scrape_raw_html", return_value=dou_rss_xml) as fetch:
            jobs = collect_rss_proxy(source)
        fetch.assert_called_once_with(source.url)
        self.assertEqual(len(jobs), 2)
        # Critical: titles are the real RSS <title>, not "Apply for vacancy"
        self.assertEqual(jobs[0].title, "Senior Product Manager")
        self.assertEqual(jobs[0].url, "https://jobs.dou.ua/companies/oriented-as/vacancies/359298/")
        self.assertEqual(jobs[1].title, "Lead Product Manager (FORMA)")
        # remote_policy inferred from description
        self.assertEqual(jobs[0].remote_policy, "remote")

    def test_collect_rss_proxy_requires_firecrawl_api_key(self):
        source = SourceConfig(id="dou", name="DOU", type="rss_proxy", url="https://jobs.dou.ua/vacancies/feeds/?category=Product+Manager")
        with mock.patch("jobhunter.sources.firecrawl_available", return_value=False):
            with self.assertRaises(SourceError) as cm:
                collect_rss_proxy(source)
        self.assertIn("FIRECRAWL_API_KEY", str(cm.exception))

    def test_collect_rss_proxy_wraps_firecrawl_errors(self):
        from jobhunter.firecrawl import FirecrawlError
        source = SourceConfig(id="dou", name="DOU", type="rss_proxy", url="https://jobs.dou.ua/vacancies/feeds/?category=Product+Manager")
        with mock.patch("jobhunter.sources.firecrawl_available", return_value=True), \
             mock.patch("jobhunter.sources.firecrawl_scrape_raw_html", side_effect=FirecrawlError("HTTP 403")):
            with self.assertRaises(SourceError) as cm:
                collect_rss_proxy(source)
        self.assertIn("Firecrawl raw fetch failed", str(cm.exception))

    def test_collect_ashby_retries_transient_read_timeout(self):
        source = SourceConfig(id="ashby", name="AshbyCo", type="ats", url="https://jobs.ashbyhq.com/ashbyco")
        response = mock.Mock()
        response.geturl.return_value = "https://api.ashbyhq.com/posting-api/job-board/ashbyco"
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"""
        {"jobs": [{
          "id": "job-1",
          "jobUrl": "https://jobs.ashbyhq.com/ashbyco/job-1",
          "title": "Senior Product Manager",
          "locationName": "Remote",
          "descriptionHtml": "<p>Build AI products.</p>",
          "publishedAt": "2026-05-31T12:00:00Z"
        }]}
        """
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("jobhunter.sources.wait_for_host_rate_limit"), mock.patch(
            "jobhunter.sources.urllib.request.urlopen",
            side_effect=[TimeoutError("read operation timed out"), response],
        ) as urlopen:
            jobs = collect_ashby(source, "ashbyco")

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Senior Product Manager")

    def test_fetch_source_text_wraps_timeout_as_source_error(self):
        source = SourceConfig(id="rss", name="RSS", type="rss", url="https://example.com/rss")
        with mock.patch("jobhunter.sources.wait_for_host_rate_limit"), mock.patch(
            "jobhunter.sources.urllib.request.urlopen",
            side_effect=TimeoutError("read operation timed out"),
        ):
            with self.assertRaises(SourceError) as cm:
                fetch_source_text(source)
        self.assertIn("Timeout fetching", str(cm.exception))

    def test_rejects_file_urls(self):
        with self.assertRaises(SourceError):
            validate_safe_url("file:///etc/passwd")

    def test_default_ignore_policy_does_not_check_robots(self):
        source = SourceConfig(
            id="blocked",
            name="Blocked",
            type="rss",
            url="https://blocked.example/jobs.xml",
            created_by="agent",
            risk_level="medium",
        )
        response = mock.Mock()
        response.geturl.return_value = source.url
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"ok"
        response.__enter__ = mock.Mock(return_value=response)
        response.__exit__ = mock.Mock(return_value=False)
        old_check = source_module.CHECK_ROBOTS
        old_respect = source_module.ROBOTS_TXT_RESPECT
        try:
            source_module.CHECK_ROBOTS = True
            source_module.ROBOTS_TXT_RESPECT = "ignore"
            with mock.patch("jobhunter.sources.validate_safe_url"), mock.patch(
                "jobhunter.sources.wait_for_host_rate_limit"
            ), mock.patch("jobhunter.sources.robots_allowed", return_value=False) as robots, mock.patch(
                "jobhunter.sources.urllib.request.urlopen", return_value=response
            ):
                self.assertEqual(fetch_source_text(source), "ok")
            robots.assert_not_called()
        finally:
            source_module.CHECK_ROBOTS = old_check
            source_module.ROBOTS_TXT_RESPECT = old_respect

    def test_strict_policy_keeps_existing_robots_block(self):
        source = SourceConfig(
            id="blocked",
            name="Blocked",
            type="rss",
            url="https://blocked.example/jobs.xml",
            created_by="agent",
            risk_level="medium",
        )
        old_check = source_module.CHECK_ROBOTS
        old_respect = source_module.ROBOTS_TXT_RESPECT
        try:
            source_module.CHECK_ROBOTS = True
            source_module.ROBOTS_TXT_RESPECT = "strict"
            with mock.patch("jobhunter.sources.validate_safe_url"), mock.patch(
                "jobhunter.sources.wait_for_host_rate_limit"
            ), mock.patch("jobhunter.sources.robots_allowed", return_value=False):
                with self.assertRaisesRegex(SourceError, "Robots.txt disallows"):
                    fetch_source_text(source)
        finally:
            source_module.CHECK_ROBOTS = old_check
            source_module.ROBOTS_TXT_RESPECT = old_respect

    def test_collect_link_page_extracts_job_links(self):
        source = SourceConfig(id="community", name="Community", type="community", url="https://example.com/jobs")
        html = '<a href="/roles/1">Senior AI Product Engineer</a><a href="/roles/2">Product Manager</a><a href="/about">About us</a>'
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            jobs = collect_link_page(source)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].url, "https://example.com/roles/1")

    def test_collect_link_page_uses_firecrawl_for_blocked_community_source(self):
        source = SourceConfig(
            id="dou-product",
            name="DOU Product",
            type="community",
            url="https://jobs.dou.ua/vacancies/?category=Product%20Manager",
        )
        markdown = """
# 244 vacancies in Product Manager

[RSS](https://jobs.dou.ua/vacancies/feeds/?category=Product%20Manager)
[Київ](https://jobs.dou.ua/vacancies?city=Kyiv&category=Product+Manager)
[1…3 роки](https://jobs.dou.ua/vacancies?category=Product+Manager&exp=1-3)
[Product manager / Product Owner цифрових продуктів SAP](https://jobs.dou.ua/companies/mod-of-ukraine/vacancies/353937/?from=list_hot)

Опис вакансії: We build digital tools for logistics.
"""
        with mock.patch("jobhunter.sources.fetch_source_text", side_effect=SourceError("HTTP 403")), mock.patch(
            "jobhunter.sources.firecrawl_available", return_value=True
        ), mock.patch("jobhunter.sources.validate_safe_url"), mock.patch(
            "jobhunter.sources.firecrawl_scrape_markdown", return_value={"text": markdown, "status": 200}
        ):
            jobs = collect_link_page(source)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product manager / Product Owner цифрових продуктів SAP")
        self.assertEqual(jobs[0].company, "Mod of Ukraine")
        self.assertIn("jobs.dou.ua/companies/mod-of-ukraine/vacancies/353937", jobs[0].url)

    def test_yc_link_page_skips_company_cards_and_derives_company_from_url(self):
        source = SourceConfig(
            id="yc-jobs-product-manager-remote",
            name="YC Product Jobs",
            type="community",
            url="https://www.ycombinator.com/jobs?role=product",
        )
        html = """
<a href="/companies/confido">Confido (S21) • AI-enabled financial automation</a>
<a href="/companies/confido/jobs/123-product-manager">Product Manager, AI Automation</a>
"""
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            jobs = collect_link_page(source)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Manager, AI Automation")
        self.assertEqual(jobs[0].company, "Confido")
        self.assertEqual(jobs[0].url, "https://www.ycombinator.com/companies/confido/jobs/123-product-manager")

    def test_weworkremotely_strips_company_prefix_when_company_is_known(self):
        source = SourceConfig(
            id="weworkremotely-product",
            name="We Work Remotely Product",
            type="community",
            url="https://weworkremotely.com/categories/remote-product-jobs",
        )
        html = '<a href="/remote-jobs/instacart-principal-product-manager">Instacart: Principal Product Manager</a>'
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            jobs = collect_link_page(source)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Principal Product Manager")
        self.assertEqual(jobs[0].company, "Instacart")

    def test_collect_greenhouse_ats(self):
        source = SourceConfig(id="gh", name="ExampleCo", type="ats", url="https://boards.greenhouse.io/exampleco")
        payload = '{"jobs":[{"id":1,"title":"Product Engineer","absolute_url":"https://boards.greenhouse.io/exampleco/jobs/1","location":{"name":"Remote"},"content":"Build AI products."}]}'
        with mock.patch("jobhunter.sources.fetch_text", return_value=payload):
            jobs = collect_ats(source)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "ExampleCo")

    def test_infer_company_handles_colon_and_fallback_garbage(self):
        self.assertEqual(infer_company("Toptal: QA Automation Engineer", ""), "Toptal")
        self.assertEqual(infer_company("Senior Engineer at Stripe", ""), "Stripe")
        self.assertEqual(infer_company("Webpt: Senior PM", "You will own analytics"), "Webpt")
        self.assertEqual(infer_company("Senior PM", "Wave is the ability to be useful"), "Unknown company")

    def test_spa_shell_is_reported_clearly(self):
        source = SourceConfig(id="spa", name="SPA", type="community", url="https://example.com/jobs")
        html = '<div id="root"></div><script src="bundle.js"></script>'
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            with self.assertRaisesRegex(SourceError, "JavaScript SPA"):
                collect_link_page(source)

    def test_enrich_job_from_url_uses_json_ld_jobposting(self):
        html = """
<html><head>
<script type="application/ld+json">
{"@type":"JobPosting","title":"AI Product Manager","description":"<p>Build AI workflows with product teams and agents. This description is intentionally long enough to prove enrichment replaced the short snippet.</p>","hiringOrganization":{"name":"ExampleCo"},"jobLocation":{"address":{"addressLocality":"Remote","addressCountry":"US"}},"baseSalary":{"currency":"USD","value":{"minValue":120000,"maxValue":160000}}}
</script>
</head><body></body></html>
"""
        row = {"url": "https://example.com/jobs/ai-pm", "description": "short"}
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            fields = enrich_job_from_url(row)

        self.assertEqual(fields["enrich_status"], "enriched")
        self.assertEqual(fields["title"], "AI Product Manager")
        self.assertEqual(fields["company"], "ExampleCo")
        self.assertEqual(fields["salary_min"], 120000)
        self.assertIn("Build AI workflows", fields["description"])


HN_JOBS_JSON = json.dumps({"hits": [
    {"objectID": "111", "title": "Acme (YC W23) Is Hiring a Senior Product Manager (Remote)",
     "url": "https://acme.com/careers/pm", "created_at": "2026-06-01T10:00:00Z"},
    {"objectID": "112", "title": "Globex Is Hiring", "created_at": "2026-06-01T11:00:00Z"},
    {"objectID": "113", "title": "", "url": "https://x.example", "created_at": "2026-06-01T12:00:00Z"},
]})
HN_WHOIS_SEARCH_JSON = json.dumps({"hits": [
    {"objectID": "900", "title": "Ask HN: Who wants to be hired? (June 2026)"},
    {"objectID": "901", "title": "Ask HN: Who is hiring? (June 2026)"},
]})
HN_THREAD_JSON = json.dumps({"id": 901, "children": [
    {"id": 1001, "created_at": "2026-06-01T12:00:00Z",
     "text": "<p>Initrode | Staff Product Manager | REMOTE (US)</p>"
             "<p>Build things. <a href=\"https://initrode.com/jobs/1\">Apply here</a></p>"},
    {"id": 1002, "created_at": "2026-06-01T12:30:00Z",
     "text": "<p>Onsite Co | NYC PM | Full-time</p>"},
    {"id": 1003, "created_at": "2026-06-01T13:00:00Z", "text": ""},
]})


def _fake_hn_fetch(url, **kwargs):
    if "tags=job" in url:
        return HN_JOBS_JSON
    if "search_by_date" in url and "author_whoishiring" in url:
        # Must be date-sorted to get the current month's thread, not a
        # high-points historical one.
        return HN_WHOIS_SEARCH_JSON
    if "/items/901" in url:
        return HN_THREAD_JSON
    raise AssertionError("unexpected HN url: %s" % url)


class HnSourceTests(unittest.TestCase):
    def test_hn_jobs_mode_extracts_structured_stories(self):
        source = SourceConfig(id="hn-jobs", name="HN Jobs", type="hn", url=HN_ALGOLIA_BASE, query="jobs")
        with mock.patch("jobhunter.sources.fetch_text", side_effect=_fake_hn_fetch):
            jobs = collect_hn(source)
        # The empty-title hit is dropped.
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].company, "Acme")
        self.assertEqual(jobs[0].url, "https://acme.com/careers/pm")
        self.assertEqual(jobs[0].remote_policy, "remote")
        # Missing url falls back to the HN item permalink.
        self.assertEqual(jobs[1].url, "https://news.ycombinator.com/item?id=112")

    def test_hn_whoishiring_picks_latest_thread_and_parses_comments(self):
        source = SourceConfig(id="hn-whos-hiring", name="HN WIH", type="hn", url=HN_ALGOLIA_BASE, query="whoishiring")
        with mock.patch("jobhunter.sources.fetch_text", side_effect=_fake_hn_fetch):
            jobs = collect_hn(source)
        # Skips the "who wants to be hired" thread; empty comment dropped.
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].title, "Initrode | Staff Product Manager | REMOTE (US)")
        self.assertEqual(jobs[0].company, "Initrode")
        self.assertEqual(jobs[0].url, "https://initrode.com/jobs/1")
        self.assertEqual(jobs[0].remote_policy, "remote")
        self.assertEqual(jobs[1].remote_policy, "unknown")

    def test_hn_whoishiring_remote_filters_to_remote_only(self):
        source = SourceConfig(id="hnhiring-remote", name="HN Remote", type="hn", url=HN_ALGOLIA_BASE, query="whoishiring-remote")
        with mock.patch("jobhunter.sources.fetch_text", side_effect=_fake_hn_fetch):
            jobs = collect_hn(source)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Initrode")


if __name__ == "__main__":
    unittest.main()
