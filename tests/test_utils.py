import unittest
from datetime import datetime, timezone

from scripts.radar_lib import make_item_id, normalize_url, parse_date_any


class UtilsTests(unittest.TestCase):
    def test_normalize_url_removes_tracking(self):
        raw = "https://example.com/path?a=1&utm_source=x&fbclid=abc"
        self.assertEqual(normalize_url(raw), "https://example.com/path?a=1")

    def test_make_item_id_stable(self):
        a = make_item_id("site", "src", "Title", "https://a.com?p=1&utm_source=x")
        b = make_item_id("site", "src", "Title", "https://a.com?p=1")
        self.assertEqual(a, b)

    def test_parse_date_rfc822_with_kst_offset(self):
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        dt = parse_date_any("Mon, 16 Nov 2026 19:33:02 +0900", now)
        self.assertEqual(dt, datetime(2026, 11, 16, 10, 33, 2, tzinfo=timezone.utc))

    def test_parse_date_named_us_timezone(self):
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        dt = parse_date_any("Mon, 16 Nov 2026 05:00:00 EST", now)
        self.assertEqual(dt, datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
