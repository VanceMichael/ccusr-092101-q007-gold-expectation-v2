
import unittest

from app.server import Handler


class HealthTest(unittest.TestCase):
    def test_handler_dispatch_methods_exist(self) -> None:
        self.assertTrue(callable(Handler.do_GET))
        self.assertTrue(callable(Handler.do_POST))


if __name__ == "__main__":
    unittest.main()
