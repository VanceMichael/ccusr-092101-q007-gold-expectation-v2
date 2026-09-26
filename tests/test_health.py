from tests.helper import ServerTestCase


class HealthTest(ServerTestCase):
    def test_health(self) -> None:
        status, body = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_unknown_route(self) -> None:
        status, body = self.client.get("/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "route_not_found")


if __name__ == "__main__":
    import unittest

    unittest.main()
