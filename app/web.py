"""极简 HTTP 路由层：仅依赖标准库。

服务边界：本服务只管理数据使用权与发布边界，任何携带行情结论或
交易建议语义的字段都会在入口被统一拒绝。
"""

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, extra: dict | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}


def bad_request(code: str, detail: str, extra: dict | None = None) -> ApiError:
    return ApiError(400, code, detail, extra)


def not_found(code: str, detail: str, extra: dict | None = None) -> ApiError:
    return ApiError(404, code, detail, extra)


def conflict(code: str, detail: str, extra: dict | None = None) -> ApiError:
    return ApiError(409, code, detail, extra)


# 本服务不承载行情结论或交易建议；命中这些字段名的请求体一律拒绝
ADVISORY_FIELDS = {
    "recommendation",
    "trading_advice",
    "investment_advice",
    "market_outlook",
    "forecast",
    "advice",
    "signal",
}


def _reject_advisory(node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.strip().lower() in ADVISORY_FIELDS:
                raise bad_request(
                    "advisory_content_not_accepted",
                    f"服务只管理使用权与发布边界，不承载行情结论或交易建议字段：{key}",
                )
            _reject_advisory(value)
    elif isinstance(node, list):
        for item in node:
            _reject_advisory(item)


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        regex = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
        self._routes.append((method, re.compile(f"^{regex}$"), handler))

    def dispatch(self, method: str, path: str, query: dict, body: object):
        if body is not None:
            _reject_advisory(body)
        for route_method, regex, handler in self._routes:
            if route_method != method:
                continue
            match = regex.match(path)
            if match:
                return handler(body=body, query=query, **match.groupdict())
        raise not_found("route_not_found", f"未定义的路由：{method} {path}")


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = None
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._respond(
                        400, {"error": {"code": "invalid_json", "detail": "请求体不是合法的 JSON"}}
                    )
            try:
                status, payload = router.dispatch(method, parsed.path, query, body)
            except ApiError as err:
                payload = {"error": {"code": err.code, "detail": err.detail}}
                payload["error"].update(err.extra)
                status, payload = err.status, payload
            except Exception as err:  # noqa: BLE001 - 兜底，避免连接悬挂
                status, payload = 500, {"error": {"code": "internal_error", "detail": str(err)}}
            self._respond(status, payload)

        def _respond(self, status: int, payload: object) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._respond(405, {"error": {"code": "method_not_allowed", "detail": "仅支持 GET/POST"}})

        def do_PATCH(self) -> None:
            self._respond(405, {"error": {"code": "method_not_allowed", "detail": "仅支持 GET/POST"}})

        def do_DELETE(self) -> None:
            self._respond(405, {"error": {"code": "method_not_allowed", "detail": "仅支持 GET/POST"}})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler
