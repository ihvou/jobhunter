import unittest
import email
from unittest import mock

from jobhunter.models import SourceConfig
from jobhunter import sources as source_module
from jobhunter.sources import SourceError, collect_ats, collect_link_page, collect_rss, fetch_source_text, infer_company, jobs_from_email, strip_html, validate_safe_url


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

    def test_email_template_parses_distinct_job_rows(self):
        source = SourceConfig(id="email", name="Email", type="imap", url="imap://job-alerts")
        source.email_templates = [
            {
                "id": "linkedin",
                "source_id": "email",
                "sender_pattern": "linkedin",
                "subject_pattern": "jobs",
                "parser_config": {"max_jobs": 5},
            }
        ]
        message = email.message_from_string(
            """From: jobs-noreply@linkedin.com
Subject: 2 new product jobs
Message-ID: <m1>
Content-Type: text/html; charset=utf-8

<a href="https://www.linkedin.com/jobs/view/1">Senior Product Manager</a>
<a href="https://www.linkedin.com/jobs/view/2">AI Product Lead</a>
"""
        )

        jobs = jobs_from_email(source, message)

        self.assertEqual([job.title for job in jobs], ["Senior Product Manager", "AI Product Lead"])
        self.assertEqual(jobs[0].url, "https://www.linkedin.com/jobs/view/1")

    def test_email_template_invalid_regex_falls_back_to_generic(self):
        source = SourceConfig(id="email", name="Email", type="imap", url="imap://job-alerts")
        source.email_templates = [
            {
                "id": "bad",
                "source_id": "email",
                "sender_pattern": "alerts",
                "subject_pattern": "jobs",
                "parser_config": {"title_pattern": "(", "max_jobs": "many"},
            }
        ]
        message = email.message_from_string(
            """From: alerts@example.com
Subject: 1 new jobs
Message-ID: <m2>
Content-Type: text/html; charset=utf-8

<a href="https://example.com/jobs/1">Product Builder</a>
"""
        )

        jobs = jobs_from_email(source, message)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Builder")

    def test_email_alert_parser_drops_linkedin_wrapper_rows(self):
        source = SourceConfig(id="email-job-alerts", name="Email Alerts", type="imap", url="imap://job-alerts")
        source.email_templates = [
            {
                "id": "linkedin",
                "source_id": "email-job-alerts",
                "sender_pattern": "linkedin",
                "subject_pattern": "jobs",
                "parser_config": {"max_jobs": 10},
            }
        ]
        message = email.message_from_string(
            """From: jobs-noreply@linkedin.com
Subject: Product Manager jobs
Message-ID: <linkedin-wrapper>
Content-Type: text/html; charset=utf-8

<a href="https://www.linkedin.com/jobs/view/1">Senior Product Manager</a>
<a href="https://www.linkedin.com/jobs/view/2">Read more</a>
<a href="https://www.linkedin.com/jobs/search/?currentJobId=3">30+ new jobs match your preferences</a>
<a href="https://www.linkedin.com/jobs/view/4">Top job picks for you</a>
<a href="https://www.linkedin.com/jobs/view/5">PM</a>
"""
        )

        jobs = jobs_from_email(source, message)

        self.assertEqual([job.title for job in jobs], ["Senior Product Manager"])
        self.assertEqual(jobs[0].url, "https://www.linkedin.com/jobs/view/1")

    def test_linkedin_email_artifacts_are_removed_from_title_and_company(self):
        source = SourceConfig(id="email-job-alerts", name="LinkedIn Email Alerts", type="imap", url="imap://job-alerts")
        source.email_templates = [
            {
                "id": "linkedin",
                "source_id": "email-job-alerts",
                "sender_pattern": "linkedin",
                "subject_pattern": "jobs",
                "parser_config": {"max_jobs": 5},
            }
        ]
        message = email.message_from_string(
            """From: jobs-noreply@linkedin.com
Subject: Product jobs
Message-ID: <linkedin-artifacts>
Content-Type: text/html; charset=utf-8

<a href="https://www.linkedin.com/jobs/view/42">Senior Product Manager role at Anthropic is available</a>
"""
        )

        jobs = jobs_from_email(source, message)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Senior Product Manager")
        self.assertEqual(jobs[0].company, "Anthropic")


if __name__ == "__main__":
    unittest.main()
