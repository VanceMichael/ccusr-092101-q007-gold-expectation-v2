"""数据授权与禁发控制的端到端测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from app.rules import INTERNAL_CHANNEL
from app.server import make_server
from app.store import Store, StoreError


def tmp_store() -> Store:
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    return Store(handle.name)


CATEGORIES = [
    ("GOLD-SPOT", "spot", "USD/oz", "America/New_York", "2026-09-26T14:00:00-04:00"),
    ("GOLD-FUT-GCX", "futures", "USD/oz", "America/New_York", "2026-09-26T14:30:00-04:00"),
    ("FED-FUNDS", "rate", "%", "America/New_York", "2026-09-26T14:00:00-04:00"),
    ("WTI-OIL", "oil", "USD/bbl", "Asia/Shanghai", "2026-09-27T02:30:00+08:00"),
    ("PBoC-GOLD", "official_gold", "t", "Asia/Shanghai", "2026-09-26T16:00:00+08:00"),
]


class LicensingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = tmp_store()
        s = self.store
        s.create_source(source_id="term", name="行情终端", kind="terminal")
        s.create_contract_version(
            source_id="term", version_label="term-v1",
            valid_from="2026-09-01T00:00:00+08:00",
            allowed_sections=["finance", "macro"], allowed_regions=["CN", "HK"],
            allowed_channels=["web", "app"], attribution_template="数据来源：行情终端",
            derived_allowed=True, quota_limit=5)
        s.create_source(source_id="rsrch", name="研究机构", kind="research_pack")
        s.create_contract_version(
            source_id="rsrch", version_label="r-v1",
            valid_from="2026-09-01T00:00:00+08:00",
            allowed_sections=["finance"], allowed_regions=["CN"],
            allowed_channels=["web"], attribution_template="来源：研究机构（限时材料）",
            derived_allowed=False, quota_limit=None)
        s.create_source(source_id="pbc", name="官方购金", kind="official")
        s.create_contract_version(
            source_id="pbc", version_label="o-v1",
            valid_from="2026-09-01T00:00:00+08:00",
            allowed_sections=["*"], allowed_regions=["*"], allowed_channels=["*"],
            attribution_template="来源：官方公告",
            derived_allowed=True, quota_limit=None)
        s.create_article(title="决议点评", section="finance", article_id="art")

    # 原始数据：五类素材各自保留时区、单位、时间
    def test_categories_keep_original_tz_unit_time(self) -> None:
        for i, (series, category, unit, tz, event_time) in enumerate(CATEGORIES):
            mid = self.store.create_material(
                source_id="term" if category != "official_gold" else "pbc",
                series_ref=series, category=category, event_time=event_time,
                tz_name=tz, unit=unit, material_id=f"m{i}")
            self.assertEqual(mid["tz_name"], tz)
            self.assertEqual(mid["unit"], unit)
            self.assertEqual(mid["event_time"], event_time)

    def _spot(self) -> str:
        if not hasattr(self, "_spot_id"):
            self._spot_id = self.store.create_material(
                source_id="term", series_ref="GOLD-SPOT", category="spot",
                event_time="2026-09-26T14:00:00-04:00", tz_name="America/New_York",
                unit="USD/oz", material_id="spot")["material_id"]
        return self._spot_id

    def _version(self, **overrides):
        params = dict(
            article_id="art", planned_publish_at="2026-09-27T03:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"material_id": self._spot()}])
        params.update(overrides)
        return self.store.create_version(**params)

    # 创建素材即获得可用性判断
    def test_usable_plan(self) -> None:
        v = self._version()
        self.assertEqual(v["verdict"], "usable")
        self.assertIn("internal_review", {d["channel"] for d in v["decisions"]})

    def test_section_denied(self) -> None:
        self.store.create_article(title="娱乐稿", section="entertainment", article_id="art2")
        v = self._version(article_id="art2")
        self.assertEqual(v["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "section_denied" for f in v["findings"]))

    def test_region_and_channel_matrix(self) -> None:
        v = self._version(planned_channels=["app"], planned_regions=["US"])
        self.assertEqual(v["verdict"], "blocked")
        codes = {(f["code"], f["channel"], f["region"]) for f in v["findings"]
                 if f["severity"] == "block"}
        self.assertIn(("region_denied", "app", "US"), codes)
        # social 未被授权
        v2 = self._version(planned_channels=["social"], planned_regions=["CN"])
        self.assertEqual(v2["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "channel_denied" for f in v2["findings"]))

    # 禁发时点
    def test_embargo_blocks_before_and_allows_after(self) -> None:
        self.store.create_embargo(
            scope_type="category", scope_ref="spot",
            embargo_until="2026-09-27T02:00:00+08:00", reason="FOMC")
        before = self._version(planned_publish_at="2026-09-27T01:59:00+08:00")
        self.assertEqual(before["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "embargo_active" for f in before["findings"]))
        after = self._version(planned_publish_at="2026-09-27T02:01:00+08:00")
        self.assertEqual(after["verdict"], "usable")

    def test_embargo_scopes(self) -> None:
        other = self.store.create_material(
            source_id="pbc", series_ref="PBoC-GOLD", category="official_gold",
            event_time="2026-09-26T16:00:00+08:00", tz_name="Asia/Shanghai",
            unit="t", material_id="off")["material_id"]
        self.store.create_embargo(
            scope_type="material", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="单素材禁发")
        v_blocked = self._version()
        self.assertEqual(v_blocked["verdict"], "blocked")
        v_free = self._version(refs=[{"material_id": other}])
        self.assertEqual(v_free["verdict"], "usable")

    # 例外批准
    def test_exception_overrides_embargo(self) -> None:
        emb = self.store.create_embargo(
            scope_type="category", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="FOMC")
        self.store.grant_exception(
            target_type="material", target_id="spot", embargo_id=emb["embargo_id"],
            reason="快讯提前授权")
        v = self._version(planned_publish_at="2026-09-27T03:00:00+08:00")
        self.assertEqual(v["verdict"], "usable")
        self.assertTrue(any(f["code"] == "embargo_exception" for f in v["findings"]))

    def test_exception_other_target_does_not_cover(self) -> None:
        emb = self.store.create_embargo(
            scope_type="category", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="FOMC")
        self.store.grant_exception(
            target_type="chart", target_id="other-chart", embargo_id=emb["embargo_id"],
            reason="只给某图表")
        self.assertEqual(self._version()["verdict"], "blocked")

    def test_expired_exception_judged_at_publish_time(self) -> None:
        emb = self.store.create_embargo(
            scope_type="category", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="FOMC")
        self.store.grant_exception(
            target_type="material", target_id="spot", embargo_id=emb["embargo_id"],
            reason="短窗口", expires_at="2026-09-27T00:00:00+08:00")
        v = self._version(planned_publish_at="2026-09-27T03:00:00+08:00")
        self.assertEqual(v["verdict"], "blocked")

    def test_revoked_exception_no_longer_covers(self) -> None:
        emb = self.store.create_embargo(
            scope_type="category", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="FOMC")
        exc = self.store.grant_exception(
            target_type="material", target_id="spot", embargo_id=emb["embargo_id"],
            reason="授权")
        self.store.revoke_exception(exc["exception_id"])
        self.assertEqual(self._version()["verdict"], "blocked")

    # 衍生图表
    def test_derived_chart_requires_derived_allowed(self) -> None:
        mat = self.store.create_material(
            source_id="rsrch", series_ref="RSRCH-X", category="spot",
            event_time="2026-09-26T14:00:00-04:00", tz_name="America/New_York",
            unit="USD/oz", material_id="rmat")
        chart = self.store.create_chart(material_id="rmat", title="研究图", chart_id="rch")
        self.store.create_article(title="研究稿", section="finance", article_id="art2")
        v = self.store.create_version(
            article_id="art2", planned_publish_at="2026-09-27T03:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"chart_id": chart["chart_id"]}])
        self.assertEqual(v["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "derived_not_allowed" for f in v["findings"]))

    def test_chart_blocked_when_upstream_channel_denied(self) -> None:
        spot = self._spot()
        fut = self.store.create_material(
            source_id="rsrch", series_ref="RSRCH-X", category="futures",
            event_time="2026-09-26T14:30:00-04:00", tz_name="America/New_York",
            unit="USD/oz", material_id="fut")["material_id"]
        chart = self.store.create_chart(
            material_id=spot, upstream_material_ids=[fut], title="现货对比研究数据")
        v = self._version(refs=[{"chart_id": chart["chart_id"]}],
                          planned_channels=["app"], planned_regions=["CN"])
        # 研究机构素材只授权 web，app 渠道下链式阻断
        self.assertEqual(v["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "derived_upstream_denied" for f in v["findings"]))

    def test_chart_attribution_includes_upstream(self) -> None:
        spot = self._spot()
        fut = self.store.create_material(
            source_id="term", series_ref="GOLD-FUT", category="futures",
            event_time="2026-09-26T14:30:00-04:00", tz_name="America/New_York",
            unit="USD/oz", material_id="fut")["material_id"]
        chart = self.store.create_chart(
            material_id=spot, upstream_material_ids=[fut], title="对比图", chart_id="ch")
        v = self._version(refs=[{"chart_id": "ch"}])
        text = v["attributions"]["chart:ch"]
        self.assertIn("数据来源：行情终端", text)
        self.assertIn("图表：", text)

    # 额度
    def test_quota_only_referenced_materials_count(self) -> None:
        # 未引用素材不占额度：建很多素材，但版本只引用一个
        for i in range(10):
            self.store.create_material(
                source_id="term", series_ref=f"X-{i}", category="spot",
                event_time="2026-09-26T14:00:00-04:00", tz_name="America/New_York",
                unit="USD/oz", material_id=f"x{i}")
        v = self._version(refs=[{"material_id": "x0"}], commit=True)
        self.assertEqual(v["verdict"], "usable")

    def test_quota_multi_channel_counts_once(self) -> None:
        # 额度=5，单个素材在 2 渠道×2 地区发布只算 1 次占用
        v = self._version(planned_channels=["web", "app"], planned_regions=["CN", "HK"],
                          commit=True)
        self.assertEqual(v["verdict"], "usable")
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        n = conn.execute("SELECT COUNT(*) FROM quota_holds").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_quota_exhausted_by_other_articles(self) -> None:
        mats = [self.store.create_material(
            source_id="term", series_ref=f"S-{i}", category="spot",
            event_time="2026-09-26T14:00:00-04:00", tz_name="America/New_York",
            unit="USD/oz", material_id=f"s{i}")["material_id"] for i in range(6)]
        self.store.create_article(title="占额稿", section="finance", article_id="hold")
        self.store.create_version(
            article_id="hold", planned_publish_at="2026-09-27T03:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"material_id": m} for m in mats[:5]], commit=True)
        v = self._version(refs=[{"material_id": mats[5]}])
        self.assertEqual(v["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "quota_exceeded" for f in v["findings"]))

    def test_quota_released_when_new_version_committed(self) -> None:
        mats = [self.store.create_material(
            source_id="term", series_ref=f"S-{i}", category="spot",
            event_time="2026-09-26T14:00:00-04:00", tz_name="America/New_York",
            unit="USD/oz", material_id=f"s{i}")["material_id"] for i in range(6)]
        self.store.create_version(
            article_id="art", planned_publish_at="2026-09-27T03:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"material_id": m} for m in mats[:5]], commit=True)
        # 同一文章新版本只引用 1 个素材：旧版本 5 个占用应释放
        v = self.store.create_version(
            article_id="art", planned_publish_at="2026-09-27T04:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"material_id": mats[5]}], commit=True)
        self.assertEqual(v["verdict"], "usable")

    def test_concurrent_drafts_unreferenced_do_not_hold(self) -> None:
        # 两篇文章同时编辑，草稿（不提交）互不影响额度
        self.store.create_article(title="草稿B", section="finance", article_id="art2")
        self._version(commit=False)
        v = self.store.create_version(
            article_id="art2", planned_publish_at="2026-09-27T03:00:00+08:00",
            planned_channels=["web"], planned_regions=["CN"],
            refs=[{"material_id": "spot"}], commit=True)
        self.assertEqual(v["verdict"], "usable")

    # 合同收窄 / 撤回
    def test_contract_narrowing_generates_new_review_but_keeps_published(self) -> None:
        v = self._version(planned_channels=["web"], commit=True)
        crd = self.store.issue_credential(review_id=v["review_id"])
        pubs = self.store.publish(credential_id=crd["credential_id"])
        self.assertEqual(len(pubs), 1)
        # 收窄：web 被移除
        self.store.create_contract_version(
            source_id="term", version_label="term-v2",
            valid_from="2026-09-27T12:00:00+08:00",
            allowed_sections=["finance"], allowed_regions=["CN"],
            allowed_channels=["app"], attribution_template="数据来源：行情终端（新版）",
            derived_allowed=True, quota_limit=5)
        v2 = self._version(planned_channels=["web"])
        self.assertEqual(v2["verdict"], "blocked")
        # 审查历史保留两份，旧凭证固定旧规则
        rep = self.store.article_report("art")
        self.assertEqual(rep["publications"][0]["status"], "live")
        self.assertIn("行情终端", rep["credential"]["attributions"]["material:spot"])
        self.assertNotIn("新版", rep["credential"]["attributions"]["material:spot"])
        self.assertEqual(rep["credential"]["snapshot_digest"], crd["snapshot_digest"])

    def test_source_withdrawal_blocks_new_review(self) -> None:
        v = self._version(commit=True)
        self.store.issue_credential(review_id=v["review_id"])
        self.store.withdraw_source("term")
        v2 = self._version()
        self.assertEqual(v2["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "source_withdrawn" for f in v2["findings"]))

    def test_material_withdrawal_blocks_chart(self) -> None:
        spot = self._spot()
        chart = self.store.create_chart(material_id=spot, title="图", chart_id="c1")
        self.store.withdraw_material(spot)
        v = self._version(refs=[{"chart_id": "c1"}])
        self.assertEqual(v["verdict"], "blocked")
        self.assertTrue(any(f["code"] == "material_withdrawn" for f in v["findings"]))

    # 禁发改期：新行，旧结果不变
    def test_embargo_reschedule_supersedes(self) -> None:
        e = self.store.create_embargo(
            scope_type="material", scope_ref="spot",
            embargo_until="2026-09-27T02:00:00+08:00", reason="FOMC", embargo_id="e1")
        before = self._version(planned_publish_at="2026-09-27T03:00:00+08:00")
        self.assertEqual(before["verdict"], "usable")
        e2 = self.store.reschedule_embargo("e1", new_until="2026-09-27T05:00:00+08:00")
        self.assertEqual(e["embargo_id"], "e1")
        self.assertEqual(e2["supersedes_id"], "e1")
        after = self._version(planned_publish_at="2026-09-27T03:00:00+08:00")
        self.assertEqual(after["verdict"], "blocked")
        # 旧审查轮次仍可读取且结论不变
        self.assertEqual(self.store.get_review(before["review_id"])["verdict"], "usable")

    # 提交与凭证
    def test_commit_blocked_degrades_to_preview_and_no_hold(self) -> None:
        self.store.create_embargo(
            scope_type="material", scope_ref="spot",
            embargo_until="2026-09-28T00:00:00+08:00", reason="FOMC")
        v = self._version(commit=True)
        self.assertEqual(v["verdict"], "blocked")
        self.assertEqual(v["kind"], "preview")
        with self.assertRaises(StoreError):
            self.store.issue_credential(review_id=v["review_id"])

    def test_credential_requires_committed(self) -> None:
        v = self._version(commit=False)
        with self.assertRaises(StoreError):
            self.store.issue_credential(review_id=v["review_id"])

    def test_publish_confined_to_credential_scope(self) -> None:
        v = self._version(planned_channels=["web"], planned_regions=["CN"], commit=True)
        crd = self.store.issue_credential(review_id=v["review_id"])
        with self.assertRaises(StoreError):
            self.store.publish(credential_id=crd["credential_id"], channels=["social"])
        pubs = self.store.publish(credential_id=crd["credential_id"])
        self.assertEqual([(p["channel"], p["region"]) for p in pubs], [("web", "CN")])
        # 幂等
        again = self.store.publish(credential_id=crd["credential_id"])
        self.assertEqual(len(again), 1)

    # 已发布页面的处置闭环
    def test_post_publication_disposition_flow(self) -> None:
        v = self._version(commit=True)
        crd = self.store.issue_credential(review_id=v["review_id"])
        pub = self.store.publish(credential_id=crd["credential_id"])[0]
        d = self.store.open_disposition(
            publication_id=pub["publication_id"], action_type="add_attribution",
            reason="来源要求补充署名", assignee="night-editor")
        rep = self.store.article_report("art")
        self.assertEqual(len(rep["open_dispositions"]), 1)
        self.store.update_disposition(d["disposition_id"], status="in_progress", note="已联系值班")
        self.store.update_disposition(d["disposition_id"], status="closed", note="署名已补")
        rep2 = self.store.article_report("art")
        self.assertEqual(rep2["open_dispositions"], [])
        self.assertEqual(rep2["publications"][0]["dispositions"][0]["status"], "closed")

    def test_take_down_disposition_for_withdrawn_source(self) -> None:
        v = self._version(commit=True)
        crd = self.store.issue_credential(review_id=v["review_id"])
        pub = self.store.publish(credential_id=crd["credential_id"])[0]
        self.store.withdraw_source("term")
        d = self.store.open_disposition(
            publication_id=pub["publication_id"], action_type="take_down",
            reason="来源撤回，要求下架")
        rep = self.store.article_report("art")
        self.assertEqual(rep["open_dispositions"][0]["action_type"], "take_down")
        self.assertEqual(d["status"], "open")

    # 文章报告：许可依据 + 署名 + 渠道处置
    def test_article_report_lists_license_basis(self) -> None:
        v = self._version(commit=True)
        self.store.issue_credential(review_id=v["review_id"])
        rep = self.store.article_report("art")
        basis = rep["license_basis"][0]["license_basis"]
        self.assertEqual(basis["source_id"], "term")
        self.assertEqual(basis["contract_version_id"], v["rule_snapshot"]["targets"][0]["contract_version_id"])
        self.assertEqual(basis["terms_digest"], v["rule_snapshot"]["contracts"][0]["terms_digest"])
        self.assertEqual(basis["tz_name"], "America/New_York")
        self.assertEqual(basis["unit"], "USD/oz")
        self.assertIn("material:spot", rep["credential"]["attributions"])
        self.assertIsNone(rep["current_version_id"]) if False else None
        self.assertEqual(rep["current_version_id"], v["version_id"])

    def test_review_snapshot_is_frozen(self) -> None:
        v = self._version(commit=True)
        digest_before = v["rule_snapshot"]["contracts"][0]["terms_digest"]
        self.store.create_contract_version(
            source_id="term", version_label="term-v2",
            valid_from="2026-10-01T00:00:00+08:00",
            allowed_sections=["finance"], allowed_regions=["CN"],
            allowed_channels=["web"], attribution_template="改",
            derived_allowed=True, quota_limit=5, terms_digest="x" * 64)
        old = self.store.get_review(v["review_id"])
        self.assertEqual(
            old["rule_snapshot"]["contracts"][0]["terms_digest"], digest_before)

    # 服务边界：不输出行情结论或交易建议
    def test_no_market_advice_in_outputs(self) -> None:
        v = self._version()
        text = json.dumps(v, ensure_ascii=False)
        for banned in ("买入", "卖出", "目标价", "建议", "看涨", "看跌", "forecast", "recommend"):
            self.assertNotIn(banned, text)

    def test_validation_errors(self) -> None:
        with self.assertRaises(StoreError):
            self.store.create_version(
                article_id="art", planned_publish_at="2026-09-27T03:00:00+08:00",
                planned_channels=["web"], planned_regions=["CN"], refs=[])
        with self.assertRaises(StoreError):
            self._version(planned_channels=[INTERNAL_CHANNEL])


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        handle.close()
        self.db_path = handle.name
        self.server = make_server("127.0.0.1", 0, Store(self.db_path))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()

    def _call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_workflow_over_http(self) -> None:
        st, health = self._call("GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual(health["status"], "ok")

        st, src = self._call("POST", "/sources",
                             {"source_id": "term", "name": "终端", "kind": "terminal"})
        self.assertEqual(st, 200)
        st, ctr = self._call("POST", "/sources/term/contracts", {
            "version_label": "v1", "valid_from": "2026-09-01T00:00:00+08:00",
            "allowed_channels": ["web"], "allowed_regions": ["CN"],
            "allowed_sections": ["finance"], "attribution_template": "终端",
            "derived_allowed": True, "quota_limit": 3})
        self.assertEqual(st, 200)
        st, mat = self._call("POST", "/materials", {
            "source_id": "term", "series_ref": "GOLD-SPOT", "category": "spot",
            "event_time": "2026-09-26T14:00:00-04:00", "tz_name": "America/New_York",
            "unit": "USD/oz", "material_id": "spot"})
        self.assertEqual(st, 200)
        st, art = self._call("POST", "/articles",
                             {"article_id": "a1", "title": "稿", "section": "finance"})
        self.assertEqual(st, 200)
        st, emb = self._call("POST", "/embargoes", {
            "scope_type": "material", "scope_ref": "spot",
            "embargo_until": "2026-09-27T02:00:00+08:00", "reason": "FOMC"})
        self.assertEqual(st, 200)
        st, v1 = self._call("POST", "/articles/a1/versions", {
            "planned_publish_at": "2026-09-27T01:00:00+08:00",
            "planned_channels": ["web"], "planned_regions": ["CN"],
            "refs": [{"material_id": "spot"}]})
        self.assertEqual(st, 200)
        self.assertEqual(v1["verdict"], "blocked")

        st, v2 = self._call("POST", "/articles/a1/versions", {
            "planned_publish_at": "2026-09-27T03:00:00+08:00",
            "planned_channels": ["web"], "planned_regions": ["CN"],
            "refs": [{"material_id": "spot"}], "commit": True})
        self.assertEqual(st, 200)
        self.assertEqual(v2["verdict"], "usable")
        st, crd = self._call("POST", f"/reviews/{v2['review_id']}/credential", {})
        self.assertEqual(st, 200)
        st, pubs = self._call("POST", "/publish",
                              {"credential_id": crd["credential_id"]})
        self.assertEqual(st, 200)
        self.assertEqual(pubs[0]["status"], "live")
        st, dsp = self._call("POST",
                             f"/publications/{pubs[0]['publication_id']}/dispositions",
                             {"action_type": "note", "reason": "法务复核"})
        self.assertEqual(st, 200)
        st, rep = self._call("GET", "/articles/a1/report")
        self.assertEqual(st, 200)
        self.assertEqual(len(rep["open_dispositions"]), 1)
        self.assertEqual(rep["license_basis"][0]["attribution"], "终端")

    def test_bad_request_returns_400(self) -> None:
        st, body = self._call("POST", "/sources", {"kind": "terminal"})
        self.assertEqual(st, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
