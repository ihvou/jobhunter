import unittest
from unittest import mock

from jobhunter.models import SourceConfig
from jobhunter.sources import SourceError, collect_ats, collect_link_page, collect_rss, infer_company, strip_html, validate_safe_url


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

    def test_collect_link_page_extracts_job_links(self):
        source = SourceConfig(id="community", name="Community", type="community", url="https://example.com/jobs")
        html = '<a href="/roles/1">Senior AI Product Engineer</a><a href="/roles/2">Product Manager</a><a href="/about">About us</a>'
        with mock.patch("jobhunter.sources.fetch_text", return_value=html):
            jobs = collect_link_page(source)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].url, "https://example.com/roles/1")

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


if __name__ == "__main__":
    unittest.main()
