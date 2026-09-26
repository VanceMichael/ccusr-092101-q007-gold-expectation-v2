"""合同收窄、来源/素材撤回、禁发新增、发布时间变动：只生成新审查结果，
已发布页面进入补署名、替换或下架流程。"""

from tests.helper import (
    ServerTestCase,
    make_contract,
    make_derivative,
    make_material,
    make_source,
    publish_flow,
)


def contract_payload(ref: str, source_ref: str, version: int, **overrides):
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
    return payload


class ContractChangeTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-C", display_name="终端供应商C")
        make_contract(cls.client, "CTR-C", "SRC-C")
        make_material(cls.client, "MAT-C1", "SRC-C", "CTR-C")
        make_derivative(
            cls.client, "DRV-C1", ["MAT-C1"], attribution_text="图表：数据来源终端供应商C"
        )
        _, _, (_status, published) = publish_flow(
            cls.client,
            "ART-C1",
            [
                {"subject_type": "material", "subject_ref": "MAT-C1"},
                {"subject_type": "derivative", "subject_ref": "DRV-C1"},
            ],
            channels=["web", "social"],
        )
        assert _status == 200, published

    def test_channel_narrowing_cascades_to_published_and_opens_takedown(self) -> None:
        status, body = self.client.post(
            "/contracts",
            contract_payload(
                "CTR-C",
                "SRC-C",
                2,
                allowed_channels=["web"],  # 社交媒体授权被收窄
                change_reason="合同续签后取消社媒授权",
            ),
        )
        self.assertEqual(status, 201)
        self.assertTrue(body["narrowed"])
        # 素材 + 衍生图表 × 2 个渠道 = 4 条新审查
        self.assertEqual(len(body["generated_review_refs"]), 4)

        # social 渠道受阻 → 下架处置；web 渠道仍可用
        _, items = self.client.get("/remediations?article_ref=ART-C1")
        open_cases = items["items"]
        self.assertEqual(len(open_cases), 2)
        self.assertTrue(all(case["channel"] == "social" for case in open_cases))
        self.assertTrue(all(case["action_type"] == "takedown" for case in open_cases))
        self.assertEqual({case["subject_ref"] for case in open_cases}, {"MAT-C1", "DRV-C1"})

    def test_attribution_template_change_opens_add_attribution(self) -> None:
        make_source(self.client, "SRC-C2", display_name="终端供应商C2")
        make_contract(self.client, "CTR-C2", "SRC-C2")
        make_material(self.client, "MAT-C2", "SRC-C2", "CTR-C2")
        _, _, (_status, published) = publish_flow(
            self.client, "ART-C2", [{"subject_type": "material", "subject_ref": "MAT-C2"}]
        )
        assert _status == 200, published

        status, body = self.client.post(
            "/contracts",
            contract_payload(
                "CTR-C2",
                "SRC-C2",
                2,
                attribution_template="数据来源：{source}（{acquired_at}，{unit}）版权所有",
                change_reason="署名格式调整",
            ),
        )
        self.assertEqual(status, 201)
        self.assertFalse(body["narrowed"])
        _, items = self.client.get("/remediations?article_ref=ART-C2")
        self.assertEqual(len(items["items"]), 1)
        case = items["items"][0]
        self.assertEqual(case["action_type"], "add_attribution")
        self.assertEqual(case["channel"], "web")

    def test_widening_does_not_trigger_cascade(self) -> None:
        make_source(self.client, "SRC-C3")
        make_contract(self.client, "CTR-C3", "SRC-C3", allowed_channels=["web"])
        make_material(self.client, "MAT-C3", "SRC-C3", "CTR-C3")
        _, _, (_status, published) = publish_flow(
            self.client, "ART-C3", [{"subject_type": "material", "subject_ref": "MAT-C3"}]
        )
        assert _status == 200, published

        status, body = self.client.post(
            "/contracts",
            contract_payload("CTR-C3", "SRC-C3", 2, allowed_channels=["web", "social"]),
        )
        self.assertEqual(status, 201)
        self.assertFalse(body["narrowed"])
        self.assertEqual(body["generated_review_refs"], [])

    def test_quota_reduction_is_narrowing(self) -> None:
        make_source(self.client, "SRC-C4")
        make_contract(self.client, "CTR-C4", "SRC-C4", quota=5)
        make_material(self.client, "MAT-C4", "SRC-C4", "CTR-C4")
        _, _, (_status, published) = publish_flow(
            self.client, "ART-C4", [{"subject_type": "material", "subject_ref": "MAT-C4"}]
        )
        assert _status == 200, published
        status, body = self.client.post(
            "/contracts", contract_payload("CTR-C4", "SRC-C4", 2, quota=5)
        )
        self.assertEqual(status, 201)
        self.assertFalse(body["narrowed"])
        status, body = self.client.post(
            "/contracts", contract_payload("CTR-C4", "SRC-C4", 3, quota=0)
        )
        # quota=0 会被校验拒绝，换成 1：已占用 1 个额度时仍不超限，但收窄成立
        self.assertEqual(status, 400)
        status, body = self.client.post(
            "/contracts", contract_payload("CTR-C4", "SRC-C4", 3, quota=1)
        )
        self.assertEqual(status, 201)
        self.assertTrue(body["narrowed"])

    def test_contract_version_must_be_sequential(self) -> None:
        make_source(self.client, "SRC-C9")
        make_contract(self.client, "CTR-C9", "SRC-C9")
        status, body = self.client.post(
            "/contracts", contract_payload("CTR-C9", "SRC-C9", 3)
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "contract_version_conflict")


class WithdrawalTest(ServerTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        make_source(cls.client, "SRC-W", display_name="研究机构W")
        make_contract(cls.client, "CTR-W", "SRC-W")
        make_material(cls.client, "MAT-W1", "SRC-W", "CTR-W", kind="oil", unit="USD/bbl")

    def test_source_withdrawal_takedowns_published_and_blocks_drafts(self) -> None:
        _, _, (_status, published) = publish_flow(
            self.client, "ART-W1", [{"subject_type": "material", "subject_ref": "MAT-W1"}]
        )
        assert _status == 200, published

        status, body = self.client.post("/sources/SRC-W/withdraw", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["generated_review_refs"]), 1)
        self.assertEqual(len(body["opened_case_refs"]), 1)

        _, case = self.client.get(f"/remediations/{body['opened_case_refs'][0]}")
        self.assertEqual(case["action_type"], "takedown")
        self.assertIn("source_withdrawn", case["reason"])

        # 撤回后再为草稿做审查必然受阻
        status, review = self.client.post(
            "/reviews",
            {
                "subject_type": "material",
                "subject_ref": "MAT-W1",
                "planned_publish_at": "2026-12-01T09:00:00+08:00",
                "channel": "web",
                "section": "gold",
                "region": "CN",
            },
        )
        self.assertEqual(review["verdict"], "blocked")
        self.assertIn("source_withdrawn", review["reasons"])

        # 重复撤回幂等报错
        status, body = self.client.post("/sources/SRC-W/withdraw", {})
        self.assertEqual(status, 409)

    def test_individual_material_withdrawal(self) -> None:
        make_source(self.client, "SRC-W2")
        make_contract(self.client, "CTR-W2", "SRC-W2")
        make_material(self.client, "MAT-W2", "SRC-W2", "CTR-W2")
        _, _, (_status, published) = publish_flow(
            self.client, "ART-W2", [{"subject_type": "material", "subject_ref": "MAT-W2"}]
        )
        assert _status == 200, published

        status, body = self.client.post("/materials/MAT-W2/withdraw", {})
        self.assertEqual(status, 200)
        case_ref = body["opened_case_refs"][0]
        _, case = self.client.get(f"/remediations/{case_ref}")
        self.assertIn("material_withdrawn", case["reason"])

        # 处置闭环后从合规清单的未闭环列表消失
        status, _ = self.client.post(
            f"/remediations/{case_ref}/close", {"resolution_note": "已用替代素材替换并下架原图表"}
        )
        self.assertEqual(status, 200)
        status, compliance = self.client.get("/articles/ART-W2/compliance")
        self.assertEqual(compliance["open_remediations"], [])


class RescheduleAndEmbargoTest(ServerTestCase):
    def _chain(self, suffix: str) -> None:
        make_source(self.client, f"SRC-E-{suffix}")
        make_contract(self.client, f"CTR-E-{suffix}", f"SRC-E-{suffix}")
        make_material(self.client, f"MAT-E-{suffix}", f"SRC-E-{suffix}", f"CTR-E-{suffix}")

    def test_publish_time_change_generates_new_review(self) -> None:
        self._chain("R1")
        self.client.post("/articles", {"article_ref": "ART-E-R1"})
        _, version = self.client.post(
            "/articles/ART-E-R1/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-02T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-E-R1"}],
            },
        )
        status, body = self.client.post(
            f"/articles/ART-E-R1/versions/{version['version_no']}/reschedule",
            {"planned_publish_at": "2026-10-03T09:00:00+08:00"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["generated_review_refs"]), 1)
        _, review = self.client.get(f"/reviews/{body['generated_review_refs'][0]}")
        self.assertEqual(review["trigger"], "publish_time_changed")
        self.assertEqual(review["planned_publish_at"], "2026-10-03T09:00:00+08:00")

    def test_new_embargo_after_publish_opens_takedown(self) -> None:
        self._chain("R2")
        _, _, (_status, published) = publish_flow(
            self.client, "ART-E-R2", [{"subject_type": "material", "subject_ref": "MAT-E-R2"}]
        )
        assert _status == 200, published

        # 已发布页面计划时间在过去；新禁发即刻生效
        status, body = self.client.post(
            "/embargoes",
            {
                "embargo_ref": "EMB-E-R2",
                "event_ref": "EMERGENCY-E",
                "source_ref": "SRC-E-R2",
                "embargo_until": "2030-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(body["opened_case_refs"]), 1)

    def test_draft_version_gets_fresh_review_but_no_remediation(self) -> None:
        self._chain("R3")
        self.client.post("/articles", {"article_ref": "ART-E-R3"})
        self.client.post(
            "/articles/ART-E-R3/versions",
            {
                "channels": ["web"],
                "section": "gold",
                "region": "CN",
                "planned_publish_at": "2026-10-02T09:00:00+08:00",
                "references": [{"subject_type": "material", "subject_ref": "MAT-E-R3"}],
            },
        )
        status, body = self.client.post(
            "/embargoes",
            {
                "embargo_ref": "EMB-E-R3",
                "event_ref": "FOMC-2026-11",
                "source_ref": "SRC-E-R3",
                "embargo_until": "2030-01-01T00:00:00+00:00",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(body["generated_review_refs"]), 1)
        self.assertEqual(body["opened_case_refs"], [])
        _, review = self.client.get(f"/reviews/{body['generated_review_refs'][0]}")
        self.assertEqual(review["trigger"], "embargo_created")
        self.assertEqual(review["verdict"], "blocked")
