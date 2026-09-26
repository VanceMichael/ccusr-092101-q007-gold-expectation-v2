"""HTTP 接口：数据授权与禁发控制。

仅做请求解析与响应编码，业务规则在 app.rules，事务在 app.store。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .store import Store, StoreError


def _require(body: dict[str, Any], key: str) -> Any:
    if key not in body or body[key] in (None, ""):
        raise StoreError(f"缺少必填字段：{key}")
    return body[key]


def _opt(body: dict[str, Any], key: str, default: Any = None) -> Any:
    return body.get(key, default)


class Handler(BaseHTTPRequestHandler):
    store: Store  # 由 make_server 注入（类属性）

    # ------------------------------------------------------------ 基础框架

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise StoreError("请求体必须是 JSON 对象")
        return data

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            body = self._read_json() if method == "POST" else {}
            for pattern, verbs, handler in ROUTES:
                match = pattern.fullmatch(self.path)
                if match and method in verbs:
                    self._send(200, handler(self.store, body, **match.groupdict()))
                    return
            self._send(404, {"error": f"未找到：{method} {self.path}"})
        except StoreError as exc:
            self._send(400, {"error": str(exc)})
        except sqlite3.OperationalError as exc:
            # 并发写入抢锁（如多人同时提交占用额度）：请调用方重试，产生新一轮审查
            self._send(409, {"error": f"并发冲突，请重试：{exc}"})
        except Exception as exc:  # noqa: BLE001 - 边界处兜底，避免连接挂死
            self._send(500, {"error": f"内部错误：{exc}"})

    # ------------------------------------------------------------ 健康检查

    @staticmethod
    def health(store: Store, body: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "service": "data-licensing-embargo"}


# ============================================================== 端点处理函数

def h_create_source(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.create_source(
        source_id=_opt(body, "source_id"), name=_require(body, "name"),
        kind=_require(body, "kind"), actor=_opt(body, "actor", "editor"))


def h_withdraw_source(store: Store, body: dict[str, Any], source_id: str) -> dict[str, Any]:
    return store.withdraw_source(source_id, actor=_opt(body, "actor", "legal"))


def h_get_source(store: Store, body: dict[str, Any], source_id: str) -> dict[str, Any]:
    return store.get_source(source_id)


def h_create_contract(store: Store, body: dict[str, Any], source_id: str) -> dict[str, Any]:
    return store.create_contract_version(
        source_id=source_id, version_label=_require(body, "version_label"),
        valid_from=_require(body, "valid_from"), valid_to=_opt(body, "valid_to"),
        allowed_sections=_opt(body, "allowed_sections", ["*"]),
        allowed_regions=_opt(body, "allowed_regions", ["*"]),
        allowed_channels=_opt(body, "allowed_channels", ["*"]),
        attribution_template=_require(body, "attribution_template"),
        derived_allowed=_opt(body, "derived_allowed", False),
        derived_attribution_required=_opt(body, "derived_attribution_required", True),
        quota_limit=_opt(body, "quota_limit"),
        terms_digest=_opt(body, "terms_digest"),
        contract_version_id=_opt(body, "contract_version_id"),
        actor=_opt(body, "actor", "legal"))


def h_get_contract(store: Store, body: dict[str, Any], contract_version_id: str) -> dict[str, Any]:
    return store.get_contract_version(contract_version_id)


def h_create_material(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.create_material(
        source_id=_require(body, "source_id"), series_ref=_require(body, "series_ref"),
        category=_require(body, "category"), event_time=_require(body, "event_time"),
        tz_name=_require(body, "tz_name"), unit=_require(body, "unit"),
        controlled_ref=_opt(body, "controlled_ref"),
        payload_digest=_opt(body, "payload_digest"),
        contract_version_id=_opt(body, "contract_version_id"),
        material_id=_opt(body, "material_id"), actor=_opt(body, "actor", "editor"))


def h_withdraw_material(store: Store, body: dict[str, Any], material_id: str) -> dict[str, Any]:
    return store.withdraw_material(material_id, actor=_opt(body, "actor", "legal"))


def h_get_material(store: Store, body: dict[str, Any], material_id: str) -> dict[str, Any]:
    return store.get_material(material_id)


def h_create_chart(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.create_chart(
        material_id=_require(body, "material_id"), title=_require(body, "title"),
        upstream_material_ids=_opt(body, "upstream_material_ids", []),
        spec_digest=_opt(body, "spec_digest"), chart_id=_opt(body, "chart_id"),
        actor=_opt(body, "actor", "editor"))


def h_withdraw_chart(store: Store, body: dict[str, Any], chart_id: str) -> dict[str, Any]:
    return store.withdraw_chart(chart_id, actor=_opt(body, "actor", "legal"))


def h_get_chart(store: Store, body: dict[str, Any], chart_id: str) -> dict[str, Any]:
    return store.get_chart(chart_id)


def h_create_embargo(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.create_embargo(
        scope_type=_require(body, "scope_type"), scope_ref=_require(body, "scope_ref"),
        embargo_until=_require(body, "embargo_until"), reason=_require(body, "reason"),
        embargo_id=_opt(body, "embargo_id"), created_by=_opt(body, "actor", "legal"))


def h_reschedule_embargo(store: Store, body: dict[str, Any], embargo_id: str) -> dict[str, Any]:
    return store.reschedule_embargo(
        embargo_id, new_until=_require(body, "new_until"),
        actor=_opt(body, "actor", "legal"))


def h_lift_embargo(store: Store, body: dict[str, Any], embargo_id: str) -> dict[str, Any]:
    return store.lift_embargo(embargo_id, actor=_opt(body, "actor", "legal"))


def h_grant_exception(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.grant_exception(
        target_type=_require(body, "target_type"), target_id=_opt(body, "target_id", "*"),
        embargo_id=_opt(body, "embargo_id"), reason=_require(body, "reason"),
        expires_at=_opt(body, "expires_at"), exception_id=_opt(body, "exception_id"),
        granted_by=_opt(body, "actor", "legal"))


def h_revoke_exception(store: Store, body: dict[str, Any], exception_id: str) -> dict[str, Any]:
    return store.revoke_exception(exception_id, actor=_opt(body, "actor", "legal"))


def h_create_article(store: Store, body: dict[str, Any]) -> dict[str, Any]:
    return store.create_article(
        title=_require(body, "title"), section=_require(body, "section"),
        article_id=_opt(body, "article_id"), actor=_opt(body, "actor", "editor"))


def h_create_version(store: Store, body: dict[str, Any], article_id: str) -> dict[str, Any]:
    refs = _require(body, "refs")
    if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
        raise StoreError("refs 必须是对象数组：{material_id} 或 {chart_id}")
    return store.create_version(
        article_id=article_id, planned_publish_at=_require(body, "planned_publish_at"),
        planned_channels=_require(body, "planned_channels"),
        planned_regions=_require(body, "planned_regions"), refs=refs,
        created_by=_opt(body, "actor", "editor"),
        commit=bool(_opt(body, "commit", False)))


def h_get_review(store: Store, body: dict[str, Any], review_id: str) -> dict[str, Any]:
    return store.get_review(review_id)


def h_issue_credential(store: Store, body: dict[str, Any], review_id: str) -> dict[str, Any]:
    return store.issue_credential(review_id=review_id, issued_by=_opt(body, "actor", "legal"))


def h_get_credential(store: Store, body: dict[str, Any], credential_id: str) -> dict[str, Any]:
    return store.get_credential(credential_id)


def h_publish(store: Store, body: dict[str, Any]) -> list[dict[str, Any]]:
    return store.publish(
        credential_id=_require(body, "credential_id"),
        channels=_opt(body, "channels"), regions=_opt(body, "regions"),
        actor=_opt(body, "actor", "editor"))


def h_open_disposition(store: Store, body: dict[str, Any], publication_id: str) -> dict[str, Any]:
    return store.open_disposition(
        publication_id=publication_id, action_type=_require(body, "action_type"),
        reason=_require(body, "reason"), assignee=_opt(body, "assignee"),
        actor=_opt(body, "actor", "legal"))


def h_update_disposition(store: Store, body: dict[str, Any], disposition_id: str) -> dict[str, Any]:
    return store.update_disposition(
        disposition_id, status=_require(body, "status"), note=_opt(body, "note"),
        actor=_opt(body, "actor", "legal"))


def h_article_report(store: Store, body: dict[str, Any], article_id: str) -> dict[str, Any]:
    return store.article_report(article_id)


Route = tuple[re.Pattern[str], set[str], Callable[..., Any]]

ROUTES: list[Route] = [
    (re.compile(r"/health"), {"GET"}, Handler.health),
    (re.compile(r"/sources"), {"POST"}, h_create_source),
    (re.compile(r"/sources/(?P<source_id>[^/]+)$"), {"GET"}, h_get_source),
    (re.compile(r"/sources/(?P<source_id>[^/]+)/withdraw$"), {"POST"}, h_withdraw_source),
    (re.compile(r"/sources/(?P<source_id>[^/]+)/contracts$"), {"POST"}, h_create_contract),
    (re.compile(r"/contracts/(?P<contract_version_id>[^/]+)$"), {"GET"}, h_get_contract),
    (re.compile(r"/materials$"), {"POST"}, h_create_material),
    (re.compile(r"/materials/(?P<material_id>[^/]+)$"), {"GET"}, h_get_material),
    (re.compile(r"/materials/(?P<material_id>[^/]+)/withdraw$"), {"POST"}, h_withdraw_material),
    (re.compile(r"/charts$"), {"POST"}, h_create_chart),
    (re.compile(r"/charts/(?P<chart_id>[^/]+)$"), {"GET"}, h_get_chart),
    (re.compile(r"/charts/(?P<chart_id>[^/]+)/withdraw$"), {"POST"}, h_withdraw_chart),
    (re.compile(r"/embargoes$"), {"POST"}, h_create_embargo),
    (re.compile(r"/embargoes/(?P<embargo_id>[^/]+)/reschedule$"), {"POST"}, h_reschedule_embargo),
    (re.compile(r"/embargoes/(?P<embargo_id>[^/]+)/lift$"), {"POST"}, h_lift_embargo),
    (re.compile(r"/exceptions$"), {"POST"}, h_grant_exception),
    (re.compile(r"/exceptions/(?P<exception_id>[^/]+)/revoke$"), {"POST"}, h_revoke_exception),
    (re.compile(r"/articles$"), {"POST"}, h_create_article),
    (re.compile(r"/articles/(?P<article_id>[^/]+)/versions$"), {"POST"}, h_create_version),
    (re.compile(r"/articles/(?P<article_id>[^/]+)/report$"), {"GET"}, h_article_report),
    (re.compile(r"/reviews/(?P<review_id>[^/]+)$"), {"GET"}, h_get_review),
    (re.compile(r"/reviews/(?P<review_id>[^/]+)/credential$"), {"POST"}, h_issue_credential),
    (re.compile(r"/credentials/(?P<credential_id>[^/]+)$"), {"GET"}, h_get_credential),
    (re.compile(r"/publish$"), {"POST"}, h_publish),
    (re.compile(r"/publications/(?P<publication_id>[^/]+)/dispositions$"), {"POST"}, h_open_disposition),
    (re.compile(r"/dispositions/(?P<disposition_id>[^/]+)$"), {"POST"}, h_update_disposition),
]


def make_server(host: str, port: int, store: Store) -> ThreadingHTTPServer:
    Handler.store = store
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    store = Store(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    server = make_server("0.0.0.0", port, store)
    server.serve_forever()


if __name__ == "__main__":
    main()
