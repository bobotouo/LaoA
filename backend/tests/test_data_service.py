import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.data_service import MarketDataError, MarketDataService


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketDataServiceTests(unittest.TestCase):
    def test_format_limit_time(self) -> None:
        self.assertEqual(MarketDataService._format_limit_time(92500), "09:25:00")
        self.assertEqual(MarketDataService._format_limit_time(130501), "13:05:01")
        self.assertEqual(MarketDataService._format_limit_time(None), "--")

    def test_trading_session_detection(self) -> None:
        open_time = datetime(2026, 7, 20, 10, 30, tzinfo=SHANGHAI)
        closed_time = datetime(2026, 7, 20, 16, 0, tzinfo=SHANGHAI)
        weekend = datetime(2026, 7, 19, 10, 30, tzinfo=SHANGHAI)

        self.assertTrue(MarketDataService._is_trading_time(open_time))
        self.assertFalse(MarketDataService._is_trading_time(closed_time))
        self.assertFalse(MarketDataService._is_trading_time(weekend))

    def test_stale_fallback_uses_last_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_dashboard.json"
            service = MarketDataService(last_dashboard_path=path)
            snapshot = {
                "meta": {
                    "mode": "live",
                    "stale": False,
                    "source": "test",
                    "updatedAt": "2026-07-20T10:00:00+08:00",
                    "isTrading": True,
                    "message": "ok",
                },
                "indices": [{"code": "000001", "name": "上证指数"}],
                "sectors": [{"code": "BK0001", "name": "测试板块"}],
                "breadth": {"up": 1, "flat": 0, "down": 0, "distribution": []},
                "turnover": {"current": 1},
                "limitPool": [],
            }
            service._save_last_dashboard(snapshot)
            service._build_live_dashboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

            result = service.get_dashboard(refresh=True)

            self.assertTrue(result["meta"]["stale"])
            self.assertEqual(result["sectors"][0]["name"], "测试板块")
            self.assertIn("最近一次成功行情", result["meta"]["message"])

    def test_failure_without_snapshot_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            service = MarketDataService(last_dashboard_path=path)
            service._build_live_dashboard = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

            with self.assertRaises(MarketDataError):
                service.get_dashboard(refresh=True)

    def test_breadth_distribution_covers_each_change_bucket(self) -> None:
        service = MarketDataService()
        values = [-10, -9, -7, -4, -2, -0.5, 0, 0.5, 2, 4, 6, 8.5, 10]
        spots = [{"f3": value, "f6": 0} for value in values]

        breadth, _ = service._summarize_spots(spots, {})

        self.assertEqual(sum(item["count"] for item in breadth["distribution"]), len(values))
        self.assertEqual([item["count"] for item in breadth["distribution"]], [1] * len(values))

    def test_eastmoney_spot_fallback_fetches_every_capped_page(self) -> None:
        service = MarketDataService()
        service.finshare.available = False
        calls: list[tuple[int, int]] = []

        def request_page(_url: str, params: dict) -> dict:
            page = int(params["pn"])
            page_size = 100 if page == 1 else int(params["pz"])
            calls.append((page, int(params["pz"])))
            start = (page - 1) * page_size
            end = min(start + page_size, 250)
            rows = [
                {"f12": f"{index:06d}", "f3": index / 100, "f6": index}
                for index in range(start, end)
            ]
            return {"data": {"total": 250, "diff": rows}}

        service._request_json = request_page

        rows = service._fetch_spot_market()

        self.assertEqual(len(rows), 250)
        self.assertEqual({page for page, _ in calls}, {1, 2, 3})
        self.assertEqual(dict(calls)[2], 100)


if __name__ == "__main__":
    unittest.main()
