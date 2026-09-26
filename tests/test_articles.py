"""授权额度只按已发布版本实际引用计算；放行凭证固定签发时规则。"""

from tests.helper import (
    ServerTestCase,
    make_contract,
    make_derivative,
    make_material,
    make_source,
    publish_flow,
)


def usage(client, contract_ref):
    status, body = client.get(f"/contracts/{contract_ref}/usage")
    assert status == 200, body
    return body


class QuotaTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-Q")
        make_contract(cls.client, "CTR-Q1", "SRC-Q", quota=1)
        make_material(cls.client, "MAT-Q1", "SRC-Q", "CTR-Q1")
        make_material(cls.client, "MAT-Q2", "SRC-Q", "CTR-Q1")

    def test_editing_and_drafts_do_not_consume_quota(self) -> None:
        # 多人同时编辑同一图表：只产生编辑记录
        make_derivative(self.client, "DRV-Q1", ["MAT-Q1"], created_by="ED-1")
        self.client.post("/derivatives/DRV-Q1/editors", {"editor_ref": "ED-2"})
        _, derivative = self.client.get("/derivatives/DRV-Q1")
        self.assertEqual(derivative["editors"], ["ED-1", "ED-2"])
        self.assertEqual(usage(self.client, "CTR-Q1")["used"], 0)

        # 草稿版本引用了素材也不占用额度
        self.client.post("/articles", {"article_ref": "ART-Q0"})
        status, _ = self.client.post(
            "/articles/ART-Q0/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "references": [{"subject_type": "derivative", "subject_ref": "DRV-Q1"}],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(usage(self.client, "CTR-Q1")["used"], 0)

    def test_quota_counts_only_published_references(self) -> None:
        make_derivative(self.client, "DRV-Q2", ["MAT-Q1"], attribution_text="图表：编辑部")
        _, _, (status, published) = publish_flow(
            self.client,
            "ART-Q1",
            [{"subject_type": "derivative", "subject_ref": "DRV-Q2"}],
        )
        self.assertEqual(status, 200, published)
        body = usage(self.client, "CTR-Q1")
        self.assertEqual(body["used"], 1)
        self.assertEqual(body["materials"][0]["material_ref"], "MAT-Q1")
        self.assertEqual(body["materials"][0]["articles"], ["ART-Q1"])

        # 额度为 1，另一篇文章再引用同合同新素材：凭证签发即被拦下
        self.client.post("/articles", {"article_ref": "ART-Q2"})
        _, version = self.client.post(
            "/articles/ART-Q2/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-Q2"}],
            },
        )
        status, body = self.client.post(
            "/vouchers",
            {"article_ref": "ART-Q2", "version_no": version["version_no"], "issued_by": "LEGAL-1"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "voucher_blocked")
        self.assertIn("quota_exceeded", body["error"]["blocked"][0]["reasons"])

    def test_publish_time_quota_check_catches_concurrent_publish(self) -> None:
        make_contract(self.client, "CTR-Q2", "SRC-Q", quota=2)
        for ref in ("MAT-Q3", "MAT-Q4", "MAT-Q5"):
            make_material(self.client, ref, "SRC-Q", "CTR-Q2")

        _, _, (status, _) = publish_flow(
            self.client, "ART-Q3", [{"subject_type": "material", "subject_ref": "MAT-Q3"}]
        )
        self.assertEqual(status, 200)

        # 两篇文章先后拿到凭证（当时均未超限），再相继发布
        vouchers = {}
        for article_ref, material_ref in (("ART-Q4", "MAT-Q4"), ("ART-Q5", "MAT-Q5")):
            self.client.post("/articles", {"article_ref": article_ref})
            _, version = self.client.post(
                f"/articles/{article_ref}/versions",
                {
                    "channels": ["web"],
                    "section": "gold",
                    "region": "CN",
                    "planned_publish_at": "2026-10-01T09:00:00+08:00",
                    "references": [{"subject_type": "material", "subject_ref": material_ref}],
                },
            )
            status, voucher = self.client.post(
                "/vouchers",
                {
                    "article_ref": article_ref,
                    "version_no": version["version_no"],
                    "issued_by": "LEGAL-1",
                },
            )
            self.assertEqual(status, 201, voucher)
            vouchers[article_ref] = (version["version_no"], voucher["voucher_ref"])

        version_no, voucher_ref = vouchers["ART-Q4"]
        status, _ = self.client.post(
            f"/articles/ART-Q4/versions/{version_no}/publish", {"voucher_ref": voucher_ref}
        )
        self.assertEqual(status, 200)

        version_no, voucher_ref = vouchers["ART-Q5"]
        status, body = self.client.post(
            f"/articles/ART-Q5/versions/{version_no}/publish", {"voucher_ref": voucher_ref}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "quota_exceeded")
        self.assertEqual(body["error"]["quota"], 2)


class VoucherTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-V")
        make_contract(cls.client, "CTR-V", "SRC-V")
        make_material(cls.client, "MAT-V1", "SRC-V", "CTR-V")

    def _draft(self, article_ref):
        self.client.post("/articles", {"article_ref": article_ref})
        _, version = self.client.post(
            f"/articles/{article_ref}/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-V1"}],
            },
        )
        return version

    def test_publish_requires_matching_voucher(self) -> None:
        version = self._draft("ART-V1")
        status, body = self.client.post(
            f"/articles/ART-V1/versions/{version['version_no']}/publish",
            {"voucher_ref": "VCH-NOPE"},
        )
        self.assertEqual(status, 404)

        # 凭证属于别的文章版本时不能混用
        other = self._draft("ART-V2")
        status, voucher = self.client.post(
            "/vouchers",
            {"article_ref": "ART-V2", "version_no": other["version_no"], "issued_by": "LEGAL-1"},
        )
        self.assertEqual(status, 201)
        status, body = self.client.post(
            f"/articles/ART-V1/versions/{version['version_no']}/publish",
            {"voucher_ref": voucher["voucher_ref"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "voucher_mismatch")

    def test_voucher_pins_rules_and_detects_staleness(self) -> None:
        version = self._draft("ART-V3")
        status, voucher = self.client.post(
            "/vouchers",
            {"article_ref": "ART-V3", "version_no": version["version_no"], "issued_by": "LEGAL-9"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(voucher["rule_snapshot"]["contracts"], {"CTR-V": 1})
        self.assertEqual(voucher["rule_snapshot"]["sources"], {"SRC-V": "active"})
        self.assertEqual(len(voucher["rule_snapshot"]["review_refs"]), 1)

        # 凭证签发后合同收窄 → 凭证失效，发布被拒绝
        self.client.post(
            "/contracts",
            {
                "contract_ref": "CTR-V",
                "version": 2,
                "source_ref": "SRC-V",
                "effective_from": "2026-01-01T00:00:00+00:00",
                "allowed_sections": ["gold", "macro"],
                "allowed_regions": ["CN", "US"],
                "allowed_channels": ["web"],
                "attribution_required": True,
                "attribution_template": "数据来源：{source}（{acquired_at}）",
                "change_reason": "渠道收窄",
            },
        )
        status, body = self.client.post(
            f"/articles/ART-V3/versions/{version['version_no']}/publish",
            {"voucher_ref": voucher["voucher_ref"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "voucher_stale")
        self.assertIn("contract:CTR-V", body["error"]["stale"])

        # 凭证本身固定的规则快照不变，可供审计
        _, fetched = self.client.get(f"/vouchers/{voucher['voucher_ref']}")
        self.assertEqual(fetched["rule_snapshot"]["contracts"], {"CTR-V": 1})

    def test_voucher_blocked_when_review_fails(self) -> None:
        self.client.post("/articles", {"article_ref": "ART-V4"})
        _, version = self.client.post(
            "/articles/ART-V4/versions",
            {
                "channels": ["print"],  # 合同未授权该渠道
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-V1"}],
            },
        )
        status, body = self.client.post(
            "/vouchers",
            {"article_ref": "ART-V4", "version_no": version["version_no"], "issued_by": "LEGAL-1"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "voucher_blocked")
        self.assertIn("channel_not_licensed", body["error"]["blocked"][0]["reasons"])


if __name__ == "__main__":
    import unittest

    unittest.main()
