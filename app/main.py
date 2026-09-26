"""服务入口：装配存储、路由与 HTTP 服务器。"""

import os
from http.server import ThreadingHTTPServer

from app.licensing.api import register_routes
from app.licensing.store import Store
from app.web import Router, make_handler
from scripts.migrate import migrate


def build_server(store: Store, port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    router = Router()
    register_routes(router, store)
    return ThreadingHTTPServer((host, port), make_handler(router))


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    migrate(database_path)
    store = Store(database_path)
    build_server(store, port).serve_forever()


if __name__ == "__main__":
    main()
