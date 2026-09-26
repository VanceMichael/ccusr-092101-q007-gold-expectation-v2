"""测试基础设施：临时数据库上的真实 HTTP 服务与常用造数函数。"""

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from app.licensing.store import Store
from app.main import build_server
from scripts.migrate import migrate


class Client:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"

    def request(self, method: str, path: str, body: object = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, body: object = None):
        return self.request("POST", path, body if body is not None else {})


class ServerTestCase(unittest.TestCase):
    """每个测试类一套独立数据库与独立服务实例。"""

    client: Client
    store: Store

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.mkdtemp(prefix="licensing-test-")
        database = os.path.join(cls._tmpdir, "test.sqlite3")
        migrate(database)
        cls.store = Store(database)
        cls._server = build_server(cls.store, 0, host="127.0.0.1")
        cls._thread = threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls.client = Client(cls._server.server_address[1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()
        cls.store.close()
        shutil.rmtree(cls._tmpdir, ignore_errors=True)


def make_source(client: Client, ref: str, **overrides):
    payload = {"source_ref": ref, "kind": "terminal", "display_name": f"来源{ref}"}
    payload.update(overrides)
    return client.post("/sources", payload)


def make_contract(client: Client, ref: str, source_ref: str, version: int = 1, **overrides):
    payload = {
        "contract_ref": ref,
        "version": version,
        "source_ref": source_ref,
        "effective_from": "2026-01-01T00:00:00+00:00",
        "allowed_sections": ["gold", "macro"],
        "allowed_regions": ["CN", "US"],
        "allowed_channels": ["web", "app", "social"],
        "attribution_required": True,
        "attribution_template": "数据来源：{source}（{acquired_at}）",
    }
    payload.update(overrides)
    return client.post("/contracts", payload)


def make_material(client: Client, ref: str, source_ref: str, contract_ref: str, **overrides):
    payload = {
        "material_ref": ref,
        "source_ref": source_ref,
        "contract_ref": contract_ref,
        "kind": "spot",
        "acquired_at": "2026-09-16T09:30:00-04:00",
        "timezone": "America/New_York",
        "unit": "USD/oz",
        "payload_sha256": "0" * 64,
    }
    payload.update(overrides)
    return client.post("/materials", payload)


def make_derivative(client: Client, ref: str, parents: list, **overrides):
    payload = {"derivative_ref": ref, "parents": parents}
    payload.update(overrides)
    return client.post("/derivatives", payload)


def make_article_version(client: Client, article_ref: str, references: list, **overrides):
    client.post("/articles", {"article_ref": article_ref})
    payload = {
        "channels": ["web"],
        "section": "gold",
        "region": "CN",
        "planned_publish_at": "2026-10-01T09:00:00+08:00",
        "references": references,
    }
    payload.update(overrides)
    return client.post(f"/articles/{article_ref}/versions", payload)


def publish_flow(client: Client, article_ref: str, references: list, **version_overrides):
    """建版本 → 签发凭证 → 发布，返回 (version, voucher, publish_response)。"""
    status, version = make_article_version(client, article_ref, references, **version_overrides)
    assert status == 201, version
    status, voucher = client.post(
        "/vouchers",
        {
            "article_ref": article_ref,
            "version_no": version["version_no"],
            "issued_by": "LEGAL-1",
        },
    )
    assert status == 201, voucher
    status, published = client.post(
        f"/articles/{article_ref}/versions/{version['version_no']}/publish",
        {"voucher_ref": voucher["voucher_ref"]},
    )
    return version, voucher, (status, published)
