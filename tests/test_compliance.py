"""任一文章可列出许可依据、应展示署名与尚未闭环的渠道处置。"""

from tests.helper import (
    ServerTestCase,
    make_contract,
    make_derivative,
    make_material,
    make_source,
    publish_flow,
)


class ComplianceTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-Z", display_name="官方购金数据方")
        make_contract(cls.client, "CTR-Z", "SRC-Z")
        make_material(
            cls.client,
            "MAT-Z1",
            "SRC-Z",
            "CTR-Z",
            kind="official_purchase",
            unit="t",
            timezone="Europe/London",
            acquired_at="2026-09-15T08:00:00+01:00",
        )
        make_derivative(
            cls.client,
            "DRV-Z1",
            ["MAT-Z1"],
            attribution_text="制图：财经编辑部；数据：官方购金数据方",
        )
        cls.version, cls.voucher, (status, cls.published) = publish_flow(
            cls.client,
            "ART-Z1",
            [{"subject_type": "derivative", "subject_ref": "DRV-Z1"}],
            channels=["web", "social"],
        )
        assert status == 200, cls.published

    def test_compliance_lists_license_basis_attribution_and_voucher(self) -> None:
        status, body = self.client.get("/articles/ART-Z1/compliance")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "published")
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["subject_ref"], "DRV-Z1")
        self.assertEqual(item["voucher_ref"], self.voucher["voucher_ref"])
        self.assertEqual(
            item["license_basis"],
            [
                {
                    "material_ref": "MAT-Z1",
                    "source_ref": "SRC-Z",
                    "contract_ref": "CTR-Z",
                    "contract_version": 1,
                }
            ],
        )
        # 应展示的署名来自凭证签发时固定的审查结果
        self.assertEqual(item["attributions"], ["制图：财经编辑部；数据：官方购金数据方"])
        self.assertEqual(body["open_remediations"], [])

    def test_narrowing_surfaces_open_channel_remediation_until_closed(self) -> None:
        status, narrowed = self.client.post(
            "/contracts",
            {
                "contract_ref": "CTR-Z",
                "version": 2,
                "source_ref": "SRC-Z",
                "effective_from": "2026-01-01T00:00:00+00:00",
                "allowed_sections": ["gold", "macro"],
                "allowed_regions": ["CN", "US"],
                "allowed_channels": ["web"],
                "attribution_required": True,
                "attribution_template": "数据来源：{source}（{acquired_at}）",
                "change_reason": "社媒授权到期",
            },
        )
        self.assertEqual(status, 201)

        _, body = self.client.get("/articles/ART-Z1/compliance")
        self.assertEqual(len(body["open_remediations"]), 1)
        case = body["open_remediations"][0]
        self.assertEqual(case["channel"], "social")
        self.assertEqual(case["action_type"], "takedown")

        # 许可依据仍固定在凭证签发时的合同版本
        self.assertEqual(body["items"][0]["license_basis"][0]["contract_version"], 1)

        status, closed = self.client.post(
            f"/remediations/{case['case_ref']}/close",
            {"resolution_note": "社媒渠道页面已下架"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")
        self.assertIsNotNone(closed["closed_at"])

        _, body = self.client.get("/articles/ART-Z1/compliance")
        self.assertEqual(body["open_remediations"], [])

    def test_manual_replace_case_and_deduplication(self) -> None:
        payload = {
            "article_ref": "ART-Z1",
            "channel": "web",
            "action_type": "replace",
            "reason": "图表样式与新版模板不一致，替换为重新制图",
            "subject_type": "derivative",
            "subject_ref": "DRV-Z1",
        }
        status, case = self.client.post("/remediations", payload)
        self.assertEqual(status, 201)
        self.assertEqual(case["action_type"], "replace")

        status, body = self.client.post("/remediations", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "case_exists")

        _, listing = self.client.get("/remediations?article_ref=ART-Z1&status=open")
        self.assertEqual(len(listing["items"]), 1)

        status, _ = self.client.post(
            f"/remediations/{case['case_ref']}/close", {"resolution_note": "已替换为 DRV-Z2"}
        )
        self.assertEqual(status, 200)

    def test_draft_article_compliance_without_voucher(self) -> None:
        self.client.post("/articles", {"article_ref": "ART-Z2"})
        self.client.post(
            "/articles/ART-Z2/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-Z1"}],
            },
        )
        status, body = self.client.get("/articles/ART-Z2/compliance")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "draft")
        item = body["items"][0]
        self.assertIsNone(item["voucher_ref"])
        # 草稿按当前最新合同版本给出许可依据与应展示署名
        _, contract = self.client.get("/contracts/CTR-Z")
        self.assertEqual(
            item["license_basis"][0]["contract_version"], contract["latest"]["version"]
        )
        self.assertEqual(
            item["attributions"], ["数据来源：官方购金数据方（2026-09-15T08:00:00+01:00）"]
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
