"""Tests for the cross-link expansion URL → handle parser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from expand import _extract_one


class HandleExtraction(unittest.TestCase):
    def test_twitter(self):
        self.assertEqual(_extract_one("https://twitter.com/alice"), "alice")
        self.assertEqual(_extract_one("https://x.com/alice"), "alice")
        self.assertEqual(_extract_one("https://www.x.com/@alice"), "alice")

    def test_instagram(self):
        self.assertEqual(_extract_one("https://www.instagram.com/foo.bar/"), "foo.bar")

    def test_tiktok_requires_at_sign(self):
        self.assertEqual(_extract_one("https://www.tiktok.com/@foo"), "foo")
        # No @ prefix → not a profile URL.
        self.assertIsNone(_extract_one("https://www.tiktok.com/foo"))

    def test_github(self):
        self.assertEqual(_extract_one("https://github.com/torvalds"), "torvalds")
        # Multi-segment paths shouldn't return a handle.
        self.assertIsNone(_extract_one("https://github.com/torvalds/linux"))

    def test_reddit(self):
        self.assertEqual(_extract_one("https://reddit.com/u/alice"), "alice")
        self.assertEqual(_extract_one("https://www.reddit.com/user/alice/"), "alice")

    def test_bandcamp_subdomain(self):
        self.assertEqual(_extract_one("https://acme.bandcamp.com/"), "acme")
        self.assertIsNone(_extract_one("https://bandcamp.com/"))

    def test_unknown_host_returns_none(self):
        self.assertIsNone(_extract_one("https://example.com/foo"))

    def test_youtube_handle_form(self):
        self.assertEqual(_extract_one("https://www.youtube.com/@MrBeast"), "MrBeast")

    def test_handles_bare_url_without_scheme(self):
        self.assertEqual(_extract_one("twitter.com/alice"), "alice")


if __name__ == "__main__":
    unittest.main()
