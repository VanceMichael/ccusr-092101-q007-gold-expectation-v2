"""HTTP 端点：请求校验、调用领域逻辑、组装响应。"""

from __future__ import annotations

from ..timeutil import now_iso, parse_iso8601
from ..web import bad_request, conflict, not_found
from . import domain
from .domain import (
    ACTION_TYPES,
    MATERIAL_KINDS,
    SOURCE_KINDS,
    SUBJECT_TYPES,
    TRIGGER_EDITOR_REQUEST,
    TRIGGER_MATERIAL_WITHDRAWN,
    TRIGGER_SOURCE_WITHDRAWN,
)


# ---------------------------------------------------------------- 校验辅助


def _body(body: object) -> dict:
    if not isinstance(body, dict):
        raise bad_request("invalid_body", "请求体必须是 JSON 对象")
    return body


def _require(body: dict, *fields: str) -> None:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise bad_request("missing_field", "缺少必填字段：" + ", ".join(missing))


def _time(value: object, field: str) -> str:
    try:
        parse_iso8601(value, field)
    except ValueError as err:
        raise bad_request("invalid_time", str(err)) from err
    return value


def _optional_time(value: object, field: str) -> str | None:
    if value in (None, ""):
        return None
    return _time(value, field)


def _str_list(value: object, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise bad_request("invalid_field", f"{field} 必须是非空字符串数组")
    return [item.strip() for item in value]


def _optional_quota(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise bad_request("invalid_field", "quota 必须是正整数或 null")
    return value


def _version_no(raw: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise bad_request("invalid_field", f"version_no 必须是整数：{raw}") from None


def _review_request(body: dict) -> dict | None:
    spec = body.get("review")
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise bad_request("invalid_field", "review 必须是对象")
    _require(spec, "planned_publish_at", "channel", "section", "region")
    _time(spec["planned_publish_at"], "planned_publish_at")
    return spec


def _maybe_review(store, subject_type: str, subject_ref: str, spec: dict | None) -> dict | None:
    if spec is None:
        return None
    return domain.create_review(
        store,
        subject_type=subject_type,
        subject_ref=subject_ref,
        planned_publish_at=spec["planned_publish_at"],
        channel=spec["channel"],
        section=spec["section"],
        region=spec["region"],
        trigger=TRIGGER_EDITOR_REQUEST,
    )


# ---------------------------------------------------------------- 路由注册


def register_routes(router, store) -> None:
    def health(body, query):
        return 200, {"status": "ok"}

    router.add("GET", "/health", health)

    # ---- 来源 ----

    def create_source(body, query):
        body = _body(body)
        _require(body, "source_ref", "kind", "display_name")
        if body["kind"] not in SOURCE_KINDS:
            raise bad_request("invalid_field", f"kind 必须是：{'/'.join(SOURCE_KINDS)}")
        if store.get_source(body["source_ref"]):
            raise conflict("duplicate_ref", f"来源已存在：{body['source_ref']}")
        row = {
            "source_ref": body["source_ref"],
            "kind": body["kind"],
            "display_name": body["display_name"],
            "status": "active",
            "created_at": now_iso(),
        }
        store.insert_source(row)
        return 201, row

    def withdraw_source(body, query, source_ref):
        source = store.get_source(source_ref)
        if source is None:
            raise not_found("source_not_found", f"来源不存在：{source_ref}")
        if source["status"] == "withdrawn":
            raise conflict("source_already_withdrawn", f"来源已撤回：{source_ref}")
        withdrawn_at = now_iso()
        store.mark_source_withdrawn(source_ref, withdrawn_at)
        materials = store.list_materials_by_source(source_ref)
        cascade = domain.cascade_published_reviews(
            store,
            trigger=TRIGGER_SOURCE_WITHDRAWN,
            material_refs=[m["material_ref"] for m in materials],
        )
        return 200, {
            "source_ref": source_ref,
            "status": "withdrawn",
            "withdrawn_at": withdrawn_at,
            **cascade,
        }

    router.add("POST", "/sources", create_source)
    router.add("POST", "/sources/{source_ref}/withdraw", withdraw_source)

    # ---- 合同 ----

    def create_contract(body, query):
        body = _body(body)
        _require(
            body,
            "contract_ref",
            "version",
            "source_ref",
            "effective_from",
            "allowed_sections",
            "allowed_regions",
            "allowed_channels",
        )
        if isinstance(body["version"], bool) or not isinstance(body["version"], int) or body["version"] < 1:
            raise bad_request("invalid_field", "version 必须是正整数")
        source = store.get_source(body["source_ref"])
        if source is None:
            raise not_found("source_not_found", f"来源不存在：{body['source_ref']}")
        _time(body["effective_from"], "effective_from")
        effective_to = _optional_time(body.get("effective_to"), "effective_to")
        attribution_required = bool(body.get("attribution_required", True))
        attribution_template = body.get("attribution_template", "")
        if not isinstance(attribution_template, str):
            raise bad_request("invalid_field", "attribution_template 必须是字符串")
        if attribution_required and not attribution_template:
            raise bad_request("invalid_field", "要求署名时必须提供 attribution_template")
        row = {
            "contract_ref": body["contract_ref"],
            "version": body["version"],
            "source_ref": body["source_ref"],
            "effective_from": body["effective_from"],
            "effective_to": effective_to,
            "allowed_sections": _str_list(body["allowed_sections"], "allowed_sections"),
            "allowed_regions": _str_list(body["allowed_regions"], "allowed_regions"),
            "allowed_channels": _str_list(body["allowed_channels"], "allowed_channels"),
            "attribution_required": attribution_required,
            "attribution_template": attribution_template,
            "quota": _optional_quota(body.get("quota")),
            "change_reason": body.get("change_reason"),
            "created_at": now_iso(),
        }
        latest = store.get_latest_contract(row["contract_ref"])
        if latest is None and row["version"] != 1:
            raise bad_request("invalid_version", "新合同的首个版本号必须为 1")
        if latest is not None and row["version"] != latest["version"] + 1:
            raise conflict(
                "contract_version_conflict",
                f"合同 {row['contract_ref']} 当前最新版本为 {latest['version']}，下一版本应为 {latest['version'] + 1}",
            )
        result = domain.register_contract_version(store, row)
        return 201, result

    def get_contract(body, query, contract_ref):
        latest = store.get_latest_contract(contract_ref)
        if latest is None:
            raise not_found("contract_not_found", f"合同不存在：{contract_ref}")
        versions = store.list_contract_versions(contract_ref)
        return 200, {"latest": latest, "versions": [v["version"] for v in versions]}

    def contract_usage(body, query, contract_ref):
        latest = store.get_latest_contract(contract_ref)
        if latest is None:
            raise not_found("contract_not_found", f"合同不存在：{contract_ref}")
        usage = domain.contract_usage(store, contract_ref)
        return 200, {
            "contract_ref": contract_ref,
            "version": latest["version"],
            "quota": latest["quota"],
            "used": len(usage),
            "materials": [
                {"material_ref": ref, "articles": articles} for ref, articles in sorted(usage.items())
            ],
        }

    router.add("POST", "/contracts", create_contract)
    router.add("GET", "/contracts/{contract_ref}", get_contract)
    router.add("GET", "/contracts/{contract_ref}/usage", contract_usage)

    # ---- 素材 ----

    def create_material(body, query):
        body = _body(body)
        _require(body, "material_ref", "source_ref", "contract_ref", "kind", "acquired_at", "unit")
        if body["kind"] not in MATERIAL_KINDS:
            raise bad_request("invalid_field", f"kind 必须是：{'/'.join(MATERIAL_KINDS)}")
        if store.get_material(body["material_ref"]):
            raise conflict("duplicate_ref", f"素材已存在：{body['material_ref']}")
        source = store.get_source(body["source_ref"])
        if source is None:
            raise not_found("source_not_found", f"来源不存在：{body['source_ref']}")
        contract = store.get_latest_contract(body["contract_ref"])
        if contract is None:
            raise not_found("contract_not_found", f"合同不存在：{body['contract_ref']}")
        if contract["source_ref"] != body["source_ref"]:
            raise bad_request("contract_source_mismatch", "合同与素材来源不一致")
        _time(body["acquired_at"], "acquired_at")
        if not body.get("payload_ref") and not body.get("payload_sha256"):
            raise bad_request("missing_payload_evidence", "素材必须携带 payload_ref 或 payload_sha256")
        row = {
            "material_ref": body["material_ref"],
            "source_ref": body["source_ref"],
            "contract_ref": body["contract_ref"],
            "contract_version": contract["version"],
            "kind": body["kind"],
            "acquired_at": body["acquired_at"],
            "timezone": body.get("timezone"),
            "unit": body["unit"],
            "payload_ref": body.get("payload_ref"),
            "payload_sha256": body.get("payload_sha256"),
            "status": "available",
            "created_at": now_iso(),
        }
        store.insert_material(row)
        review = _maybe_review(store, "material", row["material_ref"], _review_request(body))
        response = dict(row)
        if review:
            response["review"] = review
        return 201, response

    def get_material(body, query, material_ref):
        material = store.get_material(material_ref)
        if material is None:
            raise not_found("material_not_found", f"素材不存在：{material_ref}")
        return 200, material

    def withdraw_material(body, query, material_ref):
        material = store.get_material(material_ref)
        if material is None:
            raise not_found("material_not_found", f"素材不存在：{material_ref}")
        if material["status"] == "withdrawn":
            raise conflict("material_already_withdrawn", f"素材已撤回：{material_ref}")
        withdrawn_at = now_iso()
        store.mark_material_withdrawn(material_ref, withdrawn_at)
        cascade = domain.cascade_published_reviews(
            store, trigger=TRIGGER_MATERIAL_WITHDRAWN, material_refs=[material_ref]
        )
        return 200, {
            "material_ref": material_ref,
            "status": "withdrawn",
            "withdrawn_at": withdrawn_at,
            **cascade,
        }

    router.add("POST", "/materials", create_material)
    router.add("GET", "/materials/{material_ref}", get_material)
    router.add("POST", "/materials/{material_ref}/withdraw", withdraw_material)

    # ---- 衍生图表 ----

    def create_derivative(body, query):
        body = _body(body)
        _require(body, "derivative_ref", "parents")
        parents = _str_list(body["parents"], "parents")
        if store.get_derivative(body["derivative_ref"]):
            raise conflict("duplicate_ref", f"衍生图表已存在：{body['derivative_ref']}")
        for material_ref in parents:
            if store.get_material(material_ref) is None:
                raise not_found("material_not_found", f"上游素材不存在：{material_ref}")
        row = {
            "derivative_ref": body["derivative_ref"],
            "attribution_text": body.get("attribution_text"),
            "note": body.get("note"),
            "created_by": body.get("created_by"),
            "created_at": now_iso(),
        }
        store.insert_derivative(row, parents)
        if row["created_by"]:
            store.add_derivative_editor(row["derivative_ref"], row["created_by"], row["created_at"])
        review = _maybe_review(store, "derivative", row["derivative_ref"], _review_request(body))
        response = {**row, "parents": parents}
        if review:
            response["review"] = review
        return 201, response

    def get_derivative(body, query, derivative_ref):
        derivative = store.get_derivative(derivative_ref)
        if derivative is None:
            raise not_found("derivative_not_found", f"衍生图表不存在：{derivative_ref}")
        parents = [m["material_ref"] for m in store.get_derivative_parents(derivative_ref)]
        return 200, {
            **derivative,
            "parents": parents,
            "editors": store.list_derivative_editors(derivative_ref),
        }

    def add_derivative_editor(body, query, derivative_ref):
        body = _body(body)
        _require(body, "editor_ref")
        if store.get_derivative(derivative_ref) is None:
            raise not_found("derivative_not_found", f"衍生图表不存在：{derivative_ref}")
        store.add_derivative_editor(derivative_ref, body["editor_ref"], now_iso())
        return 200, {
            "derivative_ref": derivative_ref,
            "editors": store.list_derivative_editors(derivative_ref),
        }

    router.add("POST", "/derivatives", create_derivative)
    router.add("GET", "/derivatives/{derivative_ref}", get_derivative)
    router.add("POST", "/derivatives/{derivative_ref}/editors", add_derivative_editor)

    # ---- 禁发与例外批准 ----

    def create_embargo(body, query):
        body = _body(body)
        _require(body, "embargo_ref", "event_ref", "embargo_until")
        if store.get_embargo(body["embargo_ref"]):
            raise conflict("duplicate_ref", f"禁发已存在：{body['embargo_ref']}")
        _time(body["embargo_until"], "embargo_until")
        if body.get("source_ref") and store.get_source(body["source_ref"]) is None:
            raise not_found("source_not_found", f"来源不存在：{body['source_ref']}")
        if body.get("kind") and body["kind"] not in MATERIAL_KINDS:
            raise bad_request("invalid_field", f"kind 必须是：{'/'.join(MATERIAL_KINDS)}")
        row = {
            "embargo_ref": body["embargo_ref"],
            "event_ref": body["event_ref"],
            "source_ref": body.get("source_ref"),
            "kind": body.get("kind"),
            "channel": body.get("channel"),
            "region": body.get("region"),
            "embargo_until": body["embargo_until"],
            "created_at": now_iso(),
        }
        store.insert_embargo(row)
        cascade = domain.cascade_new_embargo(store, row)
        return 201, {**row, **cascade}

    def create_approval(body, query, embargo_ref):
        body = _body(body)
        _require(body, "approver_ref", "reason")
        if store.get_embargo(embargo_ref) is None:
            raise not_found("embargo_not_found", f"禁发不存在：{embargo_ref}")
        row = {
            "approval_ref": domain.new_ref("APR"),
            "embargo_ref": embargo_ref,
            "approver_ref": body["approver_ref"],
            "channel": body.get("channel"),
            "section": body.get("section"),
            "region": body.get("region"),
            "expires_at": _optional_time(body.get("expires_at"), "expires_at"),
            "reason": body["reason"],
            "created_at": now_iso(),
        }
        store.insert_approval(row)
        return 201, row

    router.add("POST", "/embargoes", create_embargo)
    router.add("POST", "/embargoes/{embargo_ref}/approvals", create_approval)

    # ---- 审查 ----

    def create_review(body, query):
        body = _body(body)
        _require(body, "subject_type", "subject_ref", "planned_publish_at", "channel", "section", "region")
        if body["subject_type"] not in SUBJECT_TYPES:
            raise bad_request("invalid_field", f"subject_type 必须是：{'/'.join(SUBJECT_TYPES)}")
        _time(body["planned_publish_at"], "planned_publish_at")
        review = domain.create_review(
            store,
            subject_type=body["subject_type"],
            subject_ref=body["subject_ref"],
            planned_publish_at=body["planned_publish_at"],
            channel=body["channel"],
            section=body["section"],
            region=body["region"],
            trigger=TRIGGER_EDITOR_REQUEST,
        )
        return 201, review

    def get_review(body, query, review_ref):
        review = store.get_review(review_ref)
        if review is None:
            raise not_found("review_not_found", f"审查结果不存在：{review_ref}")
        return 200, review

    router.add("POST", "/reviews", create_review)
    router.add("GET", "/reviews/{review_ref}", get_review)

    # ---- 文章与版本 ----

    def create_article(body, query):
        body = _body(body)
        _require(body, "article_ref")
        if store.get_article(body["article_ref"]):
            raise conflict("duplicate_ref", f"文章已存在：{body['article_ref']}")
        row = {
            "article_ref": body["article_ref"],
            "desk": body.get("desk"),
            "created_at": now_iso(),
        }
        store.insert_article(row)
        return 201, row

    def create_version(body, query, article_ref):
        body = _body(body)
        _require(body, "channels", "section", "region", "planned_publish_at", "references")
        if store.get_article(article_ref) is None:
            raise not_found("article_not_found", f"文章不存在：{article_ref}")
        channels = _str_list(body["channels"], "channels")
        _time(body["planned_publish_at"], "planned_publish_at")
        raw_refs = body["references"]
        if not isinstance(raw_refs, list) or not raw_refs:
            raise bad_request("invalid_field", "references 必须是非空数组")
        references: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in raw_refs:
            if not isinstance(item, dict):
                raise bad_request("invalid_field", "references 元素必须是对象")
            _require(item, "subject_type", "subject_ref")
            if item["subject_type"] not in SUBJECT_TYPES:
                raise bad_request("invalid_field", f"subject_type 必须是：{'/'.join(SUBJECT_TYPES)}")
            domain.resolve_subject(store, item["subject_type"], item["subject_ref"])
            key = (item["subject_type"], item["subject_ref"])
            if key not in seen:
                seen.add(key)
                references.append({"subject_type": item["subject_type"], "subject_ref": item["subject_ref"]})
        latest = store.get_latest_version(article_ref)
        version_no = 1 if latest is None else latest["version_no"] + 1
        row = {
            "article_ref": article_ref,
            "version_no": version_no,
            "channels": channels,
            "section": body["section"],
            "region": body["region"],
            "planned_publish_at": body["planned_publish_at"],
            "references": references,
            "status": "draft",
            "created_at": now_iso(),
        }
        store.insert_article_version(row)
        return 201, row

    def reschedule(body, query, article_ref, version_no):
        body = _body(body)
        _require(body, "planned_publish_at")
        _time(body["planned_publish_at"], "planned_publish_at")
        result = domain.reschedule_version(
            store,
            article_ref=article_ref,
            version_no=_version_no(version_no),
            planned_publish_at=body["planned_publish_at"],
        )
        return 200, result

    def publish(body, query, article_ref, version_no):
        body = _body(body)
        _require(body, "voucher_ref")
        version = domain.publish_version(
            store,
            article_ref=article_ref,
            version_no=_version_no(version_no),
            voucher_ref=body["voucher_ref"],
        )
        return 200, version

    def compliance(body, query, article_ref):
        return 200, domain.article_compliance(store, article_ref)

    router.add("POST", "/articles", create_article)
    router.add("POST", "/articles/{article_ref}/versions", create_version)
    router.add("POST", "/articles/{article_ref}/versions/{version_no}/reschedule", reschedule)
    router.add("POST", "/articles/{article_ref}/versions/{version_no}/publish", publish)
    router.add("GET", "/articles/{article_ref}/compliance", compliance)

    # ---- 放行凭证 ----

    def issue_voucher(body, query):
        body = _body(body)
        _require(body, "article_ref", "version_no", "issued_by")
        if isinstance(body["version_no"], bool) or not isinstance(body["version_no"], int):
            raise bad_request("invalid_field", "version_no 必须是整数")
        voucher = domain.issue_voucher(
            store,
            article_ref=body["article_ref"],
            version_no=body["version_no"],
            issued_by=body["issued_by"],
        )
        return 201, voucher

    def get_voucher(body, query, voucher_ref):
        voucher = store.get_voucher(voucher_ref)
        if voucher is None:
            raise not_found("voucher_not_found", f"放行凭证不存在：{voucher_ref}")
        return 200, voucher

    router.add("POST", "/vouchers", issue_voucher)
    router.add("GET", "/vouchers/{voucher_ref}", get_voucher)

    # ---- 处置 ----

    def create_remediation(body, query):
        body = _body(body)
        _require(body, "article_ref", "channel", "action_type", "reason")
        if body["action_type"] not in ACTION_TYPES:
            raise bad_request("invalid_field", f"action_type 必须是：{'/'.join(ACTION_TYPES)}")
        if store.get_article(body["article_ref"]) is None:
            raise not_found("article_not_found", f"文章不存在：{body['article_ref']}")
        subject_type = body.get("subject_type")
        subject_ref = body.get("subject_ref")
        if (subject_type is None) != (subject_ref is None):
            raise bad_request("invalid_field", "subject_type 与 subject_ref 必须同时提供")
        if subject_type is not None:
            if subject_type not in SUBJECT_TYPES:
                raise bad_request("invalid_field", f"subject_type 必须是：{'/'.join(SUBJECT_TYPES)}")
            domain.resolve_subject(store, subject_type, subject_ref)
        published = store.get_published_version(body["article_ref"])
        case = domain.open_remediation(
            store,
            article_ref=body["article_ref"],
            version_no=published["version_no"] if published else None,
            channel=body["channel"],
            subject_type=subject_type,
            subject_ref=subject_ref,
            action_type=body["action_type"],
            reason=body["reason"],
        )
        if case is None:
            raise conflict("case_exists", "相同文章、渠道、素材与动作的处置单尚未闭环")
        return 201, case

    def list_remediations(body, query):
        article_ref = query.get("article_ref")
        status = query.get("status")
        if status is not None and status not in ("open", "closed"):
            raise bad_request("invalid_field", "status 必须是 open 或 closed")
        return 200, {"items": store.list_remediations(article_ref=article_ref, status=status)}

    def get_remediation(body, query, case_ref):
        case = store.get_remediation(case_ref)
        if case is None:
            raise not_found("case_not_found", f"处置单不存在：{case_ref}")
        return 200, case

    def close_remediation(body, query, case_ref):
        case = store.get_remediation(case_ref)
        if case is None:
            raise not_found("case_not_found", f"处置单不存在：{case_ref}")
        if case["status"] == "closed":
            raise conflict("case_already_closed", f"处置单已闭环：{case_ref}")
        note = body.get("resolution_note") if isinstance(body, dict) else None
        store.close_remediation(case_ref, now_iso(), note)
        return 200, store.get_remediation(case_ref)

    router.add("POST", "/remediations", create_remediation)
    router.add("GET", "/remediations", list_remediations)
    router.add("GET", "/remediations/{case_ref}", get_remediation)
    router.add("POST", "/remediations/{case_ref}/close", close_remediation)
