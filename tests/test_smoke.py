import os
import unittest
from pathlib import Path

from jobhunter.config import load_sources
from jobhunter.sources import collect_from_source


@unittest.skipUnless(os.getenv("JOBHUNTER_RUN_LIVE"), "set JOBHUNTER_RUN_LIVE=1 to hit live sources")
class LiveSourceSmokeTests(unittest.TestCase):
    def test_default_sources_return_parseable_jobs(self):
        sources = [source for source in load_sources(Path("config/sources.json")) if source.enabled and source.type != "imap"]
        self.assertGreater(len(sources), 0)
        for source in sources:
            with self.subTest(source=source.id):
                jobs = collect_from_source(source)
                self.assertGreaterEqual(len(jobs), 1)
                complete = [job for job in jobs if job.title and job.url]
                self.assertGreaterEqual(len(complete) / float(len(jobs)), 0.8)


if __name__ == "__main__":
    unittest.main()
