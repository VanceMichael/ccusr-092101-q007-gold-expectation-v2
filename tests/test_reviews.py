"""编辑创建素材时即获得可用性判断：禁发、渠道、署名、例外批准。"""

from tests.helper import (
    ServerTestCase,
    make_contract,
    make_derivative,
    make_material,
    make_source,
)


class ReviewTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-R")
        make_contract(cls.client, "CTR-R", "SRC-R")
        make_material(cls.client, "MAT-R1", "SRC-R", "CTR-R")

    def review(self, subject_type, subject_ref, **overrides):
        payload = {
            "subject_type": subject_type,
            "subject_ref": subject_ref,
            "planned_publish_at": "2026-09-17T12:00:00+00:00",
            "channel": "web",
            "section": "gold",
            "region": "CN",
        }
        payload.update(overrides)
        return self.client.post("/reviews", payload)

    def test_allowed_review_renders_attribution(self) -> None:
        status, body = self.review("material", "MAT-R1")
        self.assertEqual(status, 201)
        self.assertEqual(body["verdict"], "allowed")
        self.assertEqual(body["reasons"], [])
        # 署名按合同模板渲染，取得时间保留原时区偏移
        self.assertEqual(
            body["attributions"], ["数据来源：来源SRC-R（2026-09-16T09:30:00-04:00）"]
        )
        self.assertEqual(body["trigger"], "editor_request")
        self.assertIn("CTR-R", body["rule_snapshot"]["contracts"])

    def test_channel_not_licensed(self) -> None:
        status, body = self.review("material", "MAT-R1", channel="print")
        self.assertEqual(status, 201)
        self.assertEqual(body["verdict"], "blocked")
        self.assertIn("channel_not_licensed", body["reasons"])

    def test_section_and_region_not_licensed(self) -> None:
        _, body = self.review("material", "MAT-R1", section="sport", region="EU")
        self.assertIn("section_not_licensed", body["reasons"])
        self.assertIn("region_not_licensed", body["reasons"])

    def test_embargo_blocks_until_lifted(self) -> None:
        self.client.post(
            "/embargoes",
            {
                "embargo_ref": "EMB-R1",
                "event_ref": "FOMC-2026-09",
                "kind": "spot",
                "embargo_until": "2026-09-17T18:00:00+00:00",
            },
        )
        _, before = self.review("material", "MAT-R1", planned_publish_at="2026-09-17T17:00:00+00:00")
        self.assertEqual(before["verdict"], "blocked")
        self.assertIn("embargo_active", before["reasons"])

        _, after = self.review("material", "MAT-R1", planned_publish_at="2026-09-17T19:00:00+00:00")
        self.assertEqual(after["verdict"], "allowed")

    def test_exception_approval_scoped_by_channel(self) -> None:
        # 用独立的利率素材与禁发，避免与现货禁发互相干扰
        make_material(self.client, "MAT-R3", "SRC-R", "CTR-R", kind="rates", unit="%")
        self.client.post(
            "/embargoes",
            {
                "embargo_ref": "EMB-R2",
                "event_ref": "FOMC-2026-09",
                "kind": "rates",
                "embargo_until": "2026-09-17T18:00:00+00:00",
            },
        )
        status, approval = self.client.post(
            "/embargoes/EMB-R2/approvals",
            {
                "approver_ref": "LEGAL-7",
                "channel": "web",
                "expires_at": "2026-09-17T17:45:00+00:00",
                "reason": "官网快讯例外",
            },
        )
        self.assertEqual(status, 201)

        _, web = self.review(
            "material", "MAT-R3", planned_publish_at="2026-09-17T17:30:00+00:00", channel="web"
        )
        self.assertEqual(web["verdict"], "allowed")
        self.assertIn(approval["approval_ref"], web["rule_snapshot"]["approvals"])

        # 例外不覆盖社交媒体渠道
        _, social = self.review(
            "material", "MAT-R3", planned_publish_at="2026-09-17T17:30:00+00:00", channel="social"
        )
        self.assertIn("embargo_active", social["reasons"])

        # 例外有效期之后不再覆盖
        _, expired = self.review(
            "material", "MAT-R3", planned_publish_at="2026-09-17T17:50:00+00:00", channel="web"
        )
        self.assertIn("embargo_active", expired["reasons"])

        # 禁发时点之后不再需要例外
        _, lifted = self.review(
            "material", "MAT-R3", planned_publish_at="2026-09-17T18:00:00+00:00", channel="web"
        )
        self.assertEqual(lifted["verdict"], "allowed")

    def test_derivative_without_attribution_blocked(self) -> None:
        make_derivative(self.client, "DRV-R1", ["MAT-R1"], created_by="ED-1")
        _, body = self.review("derivative", "DRV-R1")
        self.assertEqual(body["verdict"], "blocked")
        self.assertIn("attribution_missing", body["reasons"])

        make_derivative(
            self.client,
            "DRV-R2",
            ["MAT-R1"],
            attribution_text="制图：编辑部；数据来源：来源SRC-R",
        )
        _, body = self.review("derivative", "DRV-R2")
        self.assertEqual(body["verdict"], "allowed")
        self.assertEqual(body["attributions"], ["制图：编辑部；数据来源：来源SRC-R"])

    def test_material_creation_can_return_immediate_review(self) -> None:
        status, body = make_material(
            self.client,
            "MAT-R2",
            "SRC-R",
            "CTR-R",
            kind="official_purchase",
            unit="t",
            timezone="Europe/London",
            acquired_at="2026-09-15T08:00:00+01:00",
            review={
                "planned_publish_at": "2026-10-01T09:00:00+08:00",
                "channel": "web",
                "section": "gold",
                "region": "CN",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["review"]["verdict"], "allowed")
        self.assertEqual(body["review"]["trigger"], "editor_request")

    def test_material_keeps_original_timezone_unit_and_acquired_at(self) -> None:
        for kind, unit, tz, acquired in [
            ("futures", "USD/oz", "America/Chicago", "2026-09-16T08:20:00-05:00"),
            ("rates", "%", "America/New_York", "2026-09-16T14:00:00-04:00"),
            ("oil", "USD/bbl", "Asia/Dubai", "2026-09-16T17:00:00+04:00"),
            ("official_purchase", "t", "Europe/London", "2026-09-15T08:00:00+01:00"),
        ]:
            ref = f"MAT-K-{kind}"
            status, _ = make_material(
                self.client, ref, "SRC-R", "CTR-R", kind=kind, unit=unit, timezone=tz, acquired_at=acquired
            )
            self.assertEqual(status, 201)
            _, material = self.client.get(f"/materials/{ref}")
            self.assertEqual(material["acquired_at"], acquired)
            self.assertEqual(material["timezone"], tz)
            self.assertEqual(material["unit"], unit)
            self.assertEqual(material["kind"], kind)

    def test_naive_time_rejected(self) -> None:
        _, body = self.review("material", "MAT-R1", planned_publish_at="2026-09-17T12:00:00")
        self.assertEqual(body["error"]["code"], "invalid_time")

    def test_advisory_content_rejected(self) -> None:
        status, body = make_material(
            self.client, "MAT-ADV", "SRC-R", "CTR-R", recommendation="建议买入黄金"
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "advisory_content_not_accepted")

    def test_unknown_subject_404(self) -> None:
        status, body = self.review("material", "MAT-NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "material_not_found")

    def test_reviews_are_append_only(self) -> None:
        _, first = self.review("material", "MAT-R1", channel="print")
        _, second = self.review("material", "MAT-R1", channel="web")
        self.assertNotEqual(first["review_ref"], second["review_ref"])
        _, fetched = self.client.get(f"/reviews/{first['review_ref']}")
        # 新审查不会改写历史结果
        self.assertEqual(fetched["verdict"], "blocked")
        self.assertEqual(fetched["channel"], "print")


if __name__ == "__main__":
    import unittest

    unittest.main()
