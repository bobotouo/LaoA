import unittest

from app.finshare_provider import FinshareProvider


class FinshareProviderTests(unittest.TestCase):
    def test_series_uses_previous_trading_close(self) -> None:
        bars = [
            {"datetime": "2026-07-16 15:00", "open": 99, "close": 100},
            {"datetime": "2026-07-17 09:30", "open": 101, "close": 102},
            {"datetime": "2026-07-17 09:31", "open": 102, "close": 103},
        ]

        series = FinshareProvider.series_from_bars(bars)

        self.assertEqual(len(series), 2)
        self.assertEqual(series[0]["time"], "09:30")
        self.assertEqual(series[0]["changePct"], 2.0)
        self.assertEqual(series[1]["changePct"], 3.0)

    def test_a_share_code_filter_excludes_indices(self) -> None:
        self.assertTrue(FinshareProvider._is_a_share(1, "600519"))
        self.assertTrue(FinshareProvider._is_a_share(0, "300750"))
        self.assertFalse(FinshareProvider._is_a_share(0, "399001"))
        self.assertFalse(FinshareProvider._is_a_share(1, "000001"))


if __name__ == "__main__":
    unittest.main()
