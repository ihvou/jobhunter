import unittest
from unittest import mock

from jobbot.models import SourceConfig
from jobbot.sources import collect_rss, strip_html


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
        with mock.patch("jobbot.sources.fetch_text", return_value=RSS):
            jobs = collect_rss(source)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "ExampleCo")
        self.assertEqual(jobs[0].remote_policy, "remote")


if __name__ == "__main__":
    unittest.main()

