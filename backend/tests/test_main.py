import os
import unittest
from unittest.mock import patch

from fastapi import Response

from app import main


class MainTests(unittest.TestCase):
    def test_vercel_dashboard_uses_shared_cdn_cache(self) -> None:
        payload = {"meta": {"isTrading": True}}
        response = Response()

        with (
            patch.dict(os.environ, {"VERCEL": "1"}),
            patch.object(main.service, "get_dashboard", return_value=payload),
        ):
            result = main.dashboard(response, refresh=False)

        self.assertEqual(result, payload)
        self.assertEqual(response.headers["cache-control"], "public, max-age=0, must-revalidate")
        self.assertEqual(
            response.headers["vercel-cdn-cache-control"],
            "s-maxage=8, stale-while-revalidate=60",
        )

    def test_vercel_manual_refresh_bypasses_cache(self) -> None:
        response = Response()

        with (
            patch.dict(os.environ, {"VERCEL": "1"}),
            patch.object(main.service, "get_dashboard", return_value={"meta": {}}),
        ):
            main.dashboard(response, refresh=True)

        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("vercel-cdn-cache-control", response.headers)


if __name__ == "__main__":
    unittest.main()
