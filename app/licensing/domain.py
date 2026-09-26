"""数据授权与禁发控制的领域逻辑。

边界约定：
- 只管理素材的使用权与发布边界，不产出行情结论或交易建议；
- 审查结果只增不改：合同收窄、来源撤回、发布时间变动都只追加新的审查记录；
- 授权额度只按已发布文章版本实际引用的素材计算，编辑行为本身不占用额度；
- 法务放行凭证固定签发当时的规则快照，供事后审计与失效比对。
"""

from __future__ import annotations

import uuid

from ..timeutil import now_iso, parse_iso8601
from ..web import conflict, not_found

MATERIAL_KINDS = ("spot", "futures", "rates", "oil", "official_purchase")
SOURCE_KINDS = ("terminal", "public_statement", "research_institution", "official", "other")
SUBJECT_TYPES = ("material", "derivative")
ACTION_TYPES = ("add_attribution", "replace", "takedown")

# 审查触发情形
TRIGGER_EDITOR_REQUEST = "editor_request"
TRIGGER_CONTRACT_NARROWED = "contract_narrowed"
TRIGGER_CONTRACT_UPDATED = "contract_updated"
TRIGGER_SOURCE_WITHDRAWN = "source_withdrawn"
TRIGGER_MATERIAL_WITHDRAWN = "material_withdrawn"
TRIGGER_EMBARGO_CREATED = "embargo_created"
TRIGGER_PUBLISH_TIME_CHANGED = "publish_time_changed"
TRIGGER_VOUCHER_CHECK = "voucher_check"

# 阻断原因代码
REASON_SOURCE_WITHDRAWN = "source_withdrawn"
REASON_MATERIAL_WITHDRAWN = "material_withdrawn"
REASON_CONTRACT_NOT_EFFECTIVE = "contract_not_effective"
REASON_CONTRACT_EXPIRED = "contract_expired"
REASON_CHANNEL_NOT_LICENSED = "channel_not_licensed"
REASON_SECTION_NOT_LICENSED = "section_not_licensed"
REASON_REGION_NOT_LICENSED = "region_not_licensed"
REASON_EMBARGO_ACTIVE = "embargo_active"
REASON_ATTRIBUTION_MISSING = "attribution_missing"
REASON_QUOTA_EXCEEDED = "quota_exceeded"

# 触发下架处置的原因；仅缺署名时走补署名
_TAKEDOWN_REASONS = {
    REASON_SOURCE_WITHDRAWN,
    REASON_MATERIAL_WITHDRAWN,
    REASON_CONTRACT_NOT_EFFECTIVE,
    REASON_CONTRACT_EXPIRED,
    REASON_CHANNEL_NOT_LICENSED,
    REASON_SECTION_NOT_LICENSED,
    REASON_REGION_NOT_LICENSED,
    REASON_EMBARGO_ACTIVE,
    REASON_QUOTA_EXCEEDED,
}


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _add(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)


# ---------------------------------------------------------------- 主体解析


def resolve_subject(store, subject_type: str, subject_ref: str):
    """返回 (subject, materials)：素材自身，或衍生图表及其全部上游素材。"""
    if subject_type == "material":
        material = store.get_material(subject_ref)
        if material is None:
            raise not_found("material_not_found", f"素材不存在：{subject_ref}")
        return {"type": "material", "material": material}, [material]
    if subject_type == "derivative":
        derivative = store.get_derivative(subject_ref)
        if derivative is None:
            raise not_found("derivative_not_found", f"衍生图表不存在：{subject_ref}")
        parents = store.get_derivative_parents(subject_ref)
        return {"type": "derivative", "derivative": derivative}, parents
    raise not_found("subject_not_found", f"未知的素材类型：{subject_type}")


def expand_material_refs(store, references: list[dict]) -> set[str]:
    """把文章版本的引用展开为实际占额度的原始素材集合。"""
    material_refs: set[str] = set()
    for ref in references:
        if ref["subject_type"] == "material":
            material_refs.add(ref["subject_ref"])
        else:
            for parent in store.get_derivative_parents(ref["subject_ref"]):
                material_refs.add(parent["material_ref"])
    return material_refs


# ---------------------------------------------------------------- 署名


def render_template(template: str, source: dict, material: dict) -> str:
    return (
        template.replace("{source}", source["display_name"])
        .replace("{acquired_at}", material["acquired_at"])
        .replace("{unit}", material["unit"])
        .replace("{timezone}", material.get("timezone") or "")
    )


def render_attributions(subject: dict, materials: list[dict], contracts: dict, sources: dict) -> list[str]:
    """计算应展示的署名：衍生图表自带署名优先，否则按上游合同模板渲染。"""
    if subject["type"] == "derivative":
        own = subject["derivative"].get("attribution_text")
        if own:
            return [own]
    out: list[str] = []
    for material in materials:
        template = contracts[material["contract_ref"]]["attribution_template"]
        if not template:
            continue
        text = render_template(template, sources[material["source_ref"]], material)
        if text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------- 禁发


def embargo_matches(embargo: dict, material: dict, channel: str, region: str) -> bool:
    return (
        embargo["source_ref"] in (None, material["source_ref"])
        and embargo["kind"] in (None, material["kind"])
        and embargo["channel"] in (None, channel)
        and embargo["region"] in (None, region)
    )


def embargo_scope_matches_material(embargo: dict, material: dict) -> bool:
    return embargo["source_ref"] in (None, material["source_ref"]) and embargo["kind"] in (
        None,
        material["kind"],
    )


def find_covering_approval(store, embargo_ref: str, channel: str, section: str, region: str, planned) -> dict | None:
    for approval in store.list_approvals(embargo_ref):
        if approval["channel"] not in (None, channel):
            continue
        if approval["section"] not in (None, section):
            continue
        if approval["region"] not in (None, region):
            continue
        if approval["expires_at"] is not None and planned > parse_iso8601(approval["expires_at"]):
            continue
        return approval
    return None


# ---------------------------------------------------------------- 审查


def create_review(
    store,
    *,
    subject_type: str,
    subject_ref: str,
    planned_publish_at: str,
    channel: str,
    section: str,
    region: str,
    trigger: str,
) -> dict:
    """评估并追加一条审查结果。审查记录只增不改。"""
    planned = parse_iso8601(planned_publish_at, "planned_publish_at")
    subject, materials = resolve_subject(store, subject_type, subject_ref)

    reasons: list[str] = []
    sources: dict[str, dict] = {}
    contracts: dict[str, dict] = {}
    for material in materials:
        source = store.get_source(material["source_ref"])
        sources[material["source_ref"]] = source
        if source["status"] == "withdrawn":
            _add(reasons, REASON_SOURCE_WITHDRAWN)
        if material["status"] == "withdrawn":
            _add(reasons, REASON_MATERIAL_WITHDRAWN)
        contract = store.get_latest_contract(material["contract_ref"])
        contracts[material["contract_ref"]] = contract

    for contract in contracts.values():
        if planned < parse_iso8601(contract["effective_from"]):
            _add(reasons, REASON_CONTRACT_NOT_EFFECTIVE)
        if contract["effective_to"] and planned > parse_iso8601(contract["effective_to"]):
            _add(reasons, REASON_CONTRACT_EXPIRED)
        if channel not in contract["allowed_channels"]:
            _add(reasons, REASON_CHANNEL_NOT_LICENSED)
        if section not in contract["allowed_sections"]:
            _add(reasons, REASON_SECTION_NOT_LICENSED)
        if region not in contract["allowed_regions"]:
            _add(reasons, REASON_REGION_NOT_LICENSED)

    matched_embargoes: list[dict] = []
    used_approvals: list[str] = []
    for material in materials:
        for embargo in store.list_embargoes():
            if not embargo_matches(embargo, material, channel, region):
                continue
            matched_embargoes.append(embargo)
            if planned >= parse_iso8601(embargo["embargo_until"]):
                continue
            approval = find_covering_approval(
                store, embargo["embargo_ref"], channel, section, region, planned
            )
            if approval is not None:
                if approval["approval_ref"] not in used_approvals:
                    used_approvals.append(approval["approval_ref"])
            else:
                _add(reasons, REASON_EMBARGO_ACTIVE)

    attributions = render_attributions(subject, materials, contracts, sources)
    if subject["type"] == "derivative":
        if not subject["derivative"].get("attribution_text") and any(
            contract["attribution_required"] for contract in contracts.values()
        ):
            _add(reasons, REASON_ATTRIBUTION_MISSING)

    for contract in contracts.values():
        quota = contract["quota"]
        if quota is None:
            continue
        usage = contract_usage(store, contract["contract_ref"])
        fresh = {
            material["material_ref"]
            for material in materials
            if material["contract_ref"] == contract["contract_ref"]
        } - set(usage)
        if len(usage) + len(fresh) > quota:
            _add(reasons, REASON_QUOTA_EXCEEDED)

    review = {
        "review_ref": new_ref("REV"),
        "subject_type": subject_type,
        "subject_ref": subject_ref,
        "planned_publish_at": planned_publish_at,
        "channel": channel,
        "section": section,
        "region": region,
        "verdict": "allowed" if not reasons else "blocked",
        "reasons": reasons,
        "attributions": attributions,
        "rule_snapshot": {
            "evaluated_at": now_iso(),
            "contracts": {
                ref: {"version": c["version"], "attribution_required": c["attribution_required"]}
                for ref, c in contracts.items()
            },
            "sources": {ref: s["status"] for ref, s in sources.items()},
            "materials": {m["material_ref"]: m["status"] for m in materials},
            "embargoes": [
                {"embargo_ref": e["embargo_ref"], "embargo_until": e["embargo_until"]}
                for e in matched_embargoes
            ],
            "approvals": used_approvals,
        },
        "trigger": trigger,
        "created_at": now_iso(),
    }
    store.insert_review(review)
    return review


# ---------------------------------------------------------------- 授权额度


def contract_usage(store, contract_ref: str, exclude_article: str | None = None) -> dict:
    """已发布文章版本实际引用的素材才占用额度。

    返回 {material_ref: sorted(article_refs)}；编辑中的图表与草稿版本不计入。
    """
    usage: dict[str, set[str]] = {}
    for version in store.list_published_versions():
        if exclude_article and version["article_ref"] == exclude_article:
            continue
        for material_ref in expand_material_refs(store, version["references"]):
            material = store.get_material(material_ref)
            if material and material["contract_ref"] == contract_ref:
                usage.setdefault(material_ref, set()).add(version["article_ref"])
    return {ref: sorted(articles) for ref, articles in usage.items()}


# ---------------------------------------------------------------- 级联复审与处置


def _action_for(reasons: list[str]) -> str:
    if any(reason in _TAKEDOWN_REASONS for reason in reasons):
        return "takedown"
    return "add_attribution"


def open_remediation(
    store,
    *,
    article_ref: str,
    version_no: int | None,
    channel: str,
    subject_type: str | None,
    subject_ref: str | None,
    action_type: str,
    reason: str,
    review_ref: str | None = None,
) -> dict | None:
    """开立处置单；同一文章+渠道+素材+动作已有未闭环单子时去重。"""
    existing = store.find_open_remediation(article_ref, channel, subject_ref, action_type)
    if existing is not None:
        return None
    case = {
        "case_ref": new_ref("REM"),
        "article_ref": article_ref,
        "version_no": version_no,
        "channel": channel,
        "subject_type": subject_type,
        "subject_ref": subject_ref,
        "action_type": action_type,
        "reason": reason,
        "review_ref": review_ref,
        "status": "open",
        "opened_at": now_iso(),
    }
    store.insert_remediation(case)
    return case


def cascade_published_reviews(
    store,
    *,
    trigger: str,
    material_refs: list[str],
    evaluate_at: str | None = None,
    attribution_refresh: bool = False,
) -> dict:
    """对已发布页面按新事实重新审查：只追加新审查结果，受阻渠道开立处置单。"""
    affected = set(material_refs)
    derivative_refs = set(store.find_derivatives_with_parents(list(affected)))
    created_reviews: list[str] = []
    opened_cases: list[str] = []
    for version in store.list_published_versions():
        hits = [
            ref
            for ref in version["references"]
            if (ref["subject_type"] == "material" and ref["subject_ref"] in affected)
            or (ref["subject_type"] == "derivative" and ref["subject_ref"] in derivative_refs)
        ]
        if not hits:
            continue
        planned = evaluate_at or version["planned_publish_at"]
        for ref in hits:
            for channel in version["channels"]:
                review = create_review(
                    store,
                    subject_type=ref["subject_type"],
                    subject_ref=ref["subject_ref"],
                    planned_publish_at=planned,
                    channel=channel,
                    section=version["section"],
                    region=version["region"],
                    trigger=trigger,
                )
                created_reviews.append(review["review_ref"])
                if review["verdict"] == "blocked":
                    case = open_remediation(
                        store,
                        article_ref=version["article_ref"],
                        version_no=version["version_no"],
                        channel=channel,
                        subject_type=ref["subject_type"],
                        subject_ref=ref["subject_ref"],
                        action_type=_action_for(review["reasons"]),
                        reason=",".join(review["reasons"]),
                        review_ref=review["review_ref"],
                    )
                    if case:
                        opened_cases.append(case["case_ref"])
                elif attribution_refresh:
                    case = open_remediation(
                        store,
                        article_ref=version["article_ref"],
                        version_no=version["version_no"],
                        channel=channel,
                        subject_type=ref["subject_type"],
                        subject_ref=ref["subject_ref"],
                        action_type="add_attribution",
                        reason="attribution_template_changed",
                        review_ref=review["review_ref"],
                    )
                    if case:
                        opened_cases.append(case["case_ref"])
    return {"generated_review_refs": created_reviews, "opened_case_refs": opened_cases}


# ---------------------------------------------------------------- 合同变更


def cascade_new_embargo(store, embargo: dict) -> dict:
    """新禁发登记后，对涉及素材的最新文章版本（含草稿）生成新的审查结果。

    已发布版本评估时点取 max(计划发布时间, 当前时间)：禁发对尚未撤下的
    已发布页面同样构成约束；受阻渠道开立处置单。
    """
    materials = [
        m for m in store.list_materials() if embargo_scope_matches_material(embargo, m)
    ]
    if not materials:
        return {"generated_review_refs": [], "opened_case_refs": []}
    affected = {m["material_ref"] for m in materials}
    derivative_refs = set(store.find_derivatives_with_parents(list(affected)))
    now = now_iso()
    created_reviews: list[str] = []
    opened_cases: list[str] = []
    for version in store.list_latest_versions():
        hits = [
            ref
            for ref in version["references"]
            if (ref["subject_type"] == "material" and ref["subject_ref"] in affected)
            or (ref["subject_type"] == "derivative" and ref["subject_ref"] in derivative_refs)
        ]
        if not hits:
            continue
        planned = version["planned_publish_at"]
        if version["status"] == "published" and parse_iso8601(now) > parse_iso8601(planned):
            planned = now
        for ref in hits:
            for channel in version["channels"]:
                review = create_review(
                    store,
                    subject_type=ref["subject_type"],
                    subject_ref=ref["subject_ref"],
                    planned_publish_at=planned,
                    channel=channel,
                    section=version["section"],
                    region=version["region"],
                    trigger=TRIGGER_EMBARGO_CREATED,
                )
                created_reviews.append(review["review_ref"])
                if version["status"] == "published" and review["verdict"] == "blocked":
                    case = open_remediation(
                        store,
                        article_ref=version["article_ref"],
                        version_no=version["version_no"],
                        channel=channel,
                        subject_type=ref["subject_type"],
                        subject_ref=ref["subject_ref"],
                        action_type=_action_for(review["reasons"]),
                        reason=",".join(review["reasons"]),
                        review_ref=review["review_ref"],
                    )
                    if case:
                        opened_cases.append(case["case_ref"])
    return {"generated_review_refs": created_reviews, "opened_case_refs": opened_cases}


def register_contract_version(store, row: dict) -> dict:
    """登记合同新版本；收窄或对已发布页面有影响的变更会触发级联复审。"""
    previous = store.get_latest_contract(row["contract_ref"])
    store.insert_contract(row)
    result = {"contract": row, "narrowed": False, "generated_review_refs": [], "opened_case_refs": []}
    if previous is None:
        return result

    narrowed = _is_narrowed(previous, row)
    template_changed = previous["attribution_template"] != row["attribution_template"]
    result["narrowed"] = narrowed
    materials = store.list_materials_by_contract(row["contract_ref"])
    if materials and (narrowed or template_changed):
        cascade = cascade_published_reviews(
            store,
            trigger=TRIGGER_CONTRACT_NARROWED if narrowed else TRIGGER_CONTRACT_UPDATED,
            material_refs=[m["material_ref"] for m in materials],
            attribution_refresh=template_changed,
        )
        result["generated_review_refs"] = cascade["generated_review_refs"]
        result["opened_case_refs"] = cascade["opened_case_refs"]
    return result


def _is_narrowed(previous: dict, current: dict) -> bool:
    for field in ("allowed_channels", "allowed_sections", "allowed_regions"):
        if not set(current[field]) >= set(previous[field]):
            return True
    old_quota, new_quota = previous["quota"], current["quota"]
    if old_quota is None and new_quota is not None:
        return True
    if old_quota is not None and new_quota is not None and new_quota < old_quota:
        return True
    old_to = previous["effective_to"]
    new_to = current["effective_to"]
    if new_to and (old_to is None or parse_iso8601(new_to) < parse_iso8601(old_to)):
        return True
    return False


# ---------------------------------------------------------------- 放行凭证与发布


def _version_or_404(store, article_ref: str, version_no: int) -> dict:
    version = store.get_article_version(article_ref, version_no)
    if version is None:
        raise not_found("version_not_found", f"文章版本不存在：{article_ref} v{version_no}")
    return version


def _involved_contracts(store, references: list[dict]) -> dict[str, dict]:
    contracts: dict[str, dict] = {}
    for material_ref in expand_material_refs(store, references):
        material = store.get_material(material_ref)
        if material:
            contracts[material["contract_ref"]] = store.get_latest_contract(material["contract_ref"])
    return contracts


def _relevant_embargoes(store, references: list[dict]) -> list[dict]:
    materials = [
        store.get_material(ref) for ref in sorted(expand_material_refs(store, references))
    ]
    seen: dict[str, dict] = {}
    for material in materials:
        if material is None:
            continue
        for embargo in store.list_embargoes():
            if embargo_scope_matches_material(embargo, material):
                seen[embargo["embargo_ref"]] = embargo
    return [seen[key] for key in sorted(seen)]


def issue_voucher(store, *, article_ref: str, version_no: int, issued_by: str) -> dict:
    """法务签发放行凭证：对所有引用×渠道做放行审查，全部通过才签发，并固定当时规则。"""
    version = _version_or_404(store, article_ref, version_no)
    if version["status"] != "draft":
        raise conflict("version_not_draft", f"只能为草稿版本签发凭证，当前状态：{version['status']}")

    reviews: list[dict] = []
    for ref in version["references"]:
        for channel in version["channels"]:
            reviews.append(
                create_review(
                    store,
                    subject_type=ref["subject_type"],
                    subject_ref=ref["subject_ref"],
                    planned_publish_at=version["planned_publish_at"],
                    channel=channel,
                    section=version["section"],
                    region=version["region"],
                    trigger=TRIGGER_VOUCHER_CHECK,
                )
            )
    blocked = [r for r in reviews if r["verdict"] != "allowed"]
    if blocked:
        raise conflict(
            "voucher_blocked",
            "存在未通过的放行审查，不能签发凭证",
            extra={
                "blocked": [
                    {
                        "review_ref": r["review_ref"],
                        "subject_ref": r["subject_ref"],
                        "channel": r["channel"],
                        "reasons": r["reasons"],
                    }
                    for r in blocked
                ]
            },
        )

    contracts = _involved_contracts(store, version["references"])
    sources = {
        ref: store.get_source(ref)["status"]
        for ref in {c["source_ref"] for c in contracts.values()}
    }
    voucher = {
        "voucher_ref": new_ref("VCH"),
        "article_ref": article_ref,
        "version_no": version_no,
        "issued_by": issued_by,
        "status": "active",
        "issued_at": now_iso(),
        "rule_snapshot": {
            "issued_at": now_iso(),
            "planned_publish_at": version["planned_publish_at"],
            "channels": version["channels"],
            "review_refs": [r["review_ref"] for r in reviews],
            "contracts": {ref: c["version"] for ref, c in contracts.items()},
            "sources": sources,
            "embargoes": [
                {"embargo_ref": e["embargo_ref"], "embargo_until": e["embargo_until"]}
                for e in _relevant_embargoes(store, version["references"])
            ],
        },
    }
    store.insert_voucher(voucher)
    return voucher


def voucher_staleness(store, voucher: dict) -> list[str]:
    """比对凭证固定的规则与当前规则，返回失效项列表。"""
    snapshot = voucher["rule_snapshot"]
    stale: list[str] = []
    for contract_ref, pinned_version in snapshot["contracts"].items():
        latest = store.get_latest_contract(contract_ref)
        if latest is None or latest["version"] != pinned_version:
            stale.append(f"contract:{contract_ref}")
    for source_ref, pinned_status in snapshot["sources"].items():
        source = store.get_source(source_ref)
        if source is None or source["status"] != pinned_status:
            stale.append(f"source:{source_ref}")
    pinned_embargoes = {
        (e["embargo_ref"], e["embargo_until"]) for e in snapshot["embargoes"]
    }
    version = store.get_article_version(voucher["article_ref"], voucher["version_no"])
    references = version["references"] if version else []
    current_embargoes = {
        (e["embargo_ref"], e["embargo_until"])
        for e in _relevant_embargoes(store, references)
    }
    if pinned_embargoes != current_embargoes:
        stale.append("embargoes")
    return stale


def publish_version(store, *, article_ref: str, version_no: int, voucher_ref: str) -> dict:
    version = _version_or_404(store, article_ref, version_no)
    if version["status"] != "draft":
        raise conflict("version_not_publishable", f"版本当前状态不可发布：{version['status']}")
    voucher = store.get_voucher(voucher_ref)
    if voucher is None:
        raise not_found("voucher_not_found", f"放行凭证不存在：{voucher_ref}")
    if voucher["article_ref"] != article_ref or voucher["version_no"] != version_no:
        raise conflict("voucher_mismatch", "放行凭证与文章版本不匹配")
    if voucher["status"] != "active":
        raise conflict("voucher_not_active", f"放行凭证状态不可用：{voucher['status']}")

    stale = voucher_staleness(store, voucher)
    if stale:
        raise conflict(
            "voucher_stale",
            "凭证签发后规则已变化，需要法务重新签发",
            extra={"stale": stale},
        )

    for contract_ref, contract in _involved_contracts(store, version["references"]).items():
        quota = contract["quota"]
        if quota is None:
            continue
        usage = contract_usage(store, contract_ref, exclude_article=article_ref)
        fresh = {
            ref
            for ref in expand_material_refs(store, version["references"])
            if (store.get_material(ref) or {}).get("contract_ref") == contract_ref
        }
        projected = set(usage) | fresh
        if len(projected) > quota:
            raise conflict(
                "quota_exceeded",
                f"合同 {contract_ref} 授权额度 {quota}，发布后占用 {len(projected)}",
                extra={
                    "contract_ref": contract_ref,
                    "quota": quota,
                    "projected": sorted(projected),
                },
            )

    previous = store.get_published_version(article_ref)
    if previous is not None:
        store.set_version_status(article_ref, previous["version_no"], "superseded")
    published_at = now_iso()
    store.set_version_status(article_ref, version_no, "published", published_at)
    version = store.get_article_version(article_ref, version_no)
    return version


def reschedule_version(store, *, article_ref: str, version_no: int, planned_publish_at: str) -> dict:
    """发布时间变动：更新计划时间并对全部引用生成新的审查结果。"""
    version = _version_or_404(store, article_ref, version_no)
    parse_iso8601(planned_publish_at, "planned_publish_at")
    store.update_version_planned(article_ref, version_no, planned_publish_at)
    created_reviews: list[str] = []
    opened_cases: list[str] = []
    for ref in version["references"]:
        for channel in version["channels"]:
            review = create_review(
                store,
                subject_type=ref["subject_type"],
                subject_ref=ref["subject_ref"],
                planned_publish_at=planned_publish_at,
                channel=channel,
                section=version["section"],
                region=version["region"],
                trigger=TRIGGER_PUBLISH_TIME_CHANGED,
            )
            created_reviews.append(review["review_ref"])
            if version["status"] == "published" and review["verdict"] == "blocked":
                case = open_remediation(
                    store,
                    article_ref=article_ref,
                    version_no=version_no,
                    channel=channel,
                    subject_type=ref["subject_type"],
                    subject_ref=ref["subject_ref"],
                    action_type=_action_for(review["reasons"]),
                    reason=",".join(review["reasons"]),
                    review_ref=review["review_ref"],
                )
                if case:
                    opened_cases.append(case["case_ref"])
    return {
        "version": store.get_article_version(article_ref, version_no),
        "generated_review_refs": created_reviews,
        "opened_case_refs": opened_cases,
    }


# ---------------------------------------------------------------- 合规清单


def article_compliance(store, article_ref: str) -> dict:
    """任一文章的许可依据、应展示署名与尚未闭环的渠道处置。"""
    article = store.get_article(article_ref)
    if article is None:
        raise not_found("article_not_found", f"文章不存在：{article_ref}")
    version = store.get_published_version(article_ref) or store.get_latest_version(article_ref)
    if version is None:
        raise not_found("version_not_found", f"文章尚无版本：{article_ref}")
    voucher = store.get_active_voucher(article_ref, version["version_no"])

    pinned_reviews: dict[tuple[str, str], dict] = {}
    if voucher is not None:
        for review_ref in voucher["rule_snapshot"]["review_refs"]:
            review = store.get_review(review_ref)
            if review:
                pinned_reviews[(review["subject_type"], review["subject_ref"])] = review

    items = []
    for ref in version["references"]:
        subject, materials = resolve_subject(store, ref["subject_type"], ref["subject_ref"])
        contracts: dict[str, dict] = {}
        sources: dict[str, dict] = {}
        for material in materials:
            contracts[material["contract_ref"]] = store.get_latest_contract(material["contract_ref"])
            sources[material["source_ref"]] = store.get_source(material["source_ref"])
        pinned = pinned_reviews.get((ref["subject_type"], ref["subject_ref"]))
        if pinned is not None:
            attributions = pinned["attributions"]
        else:
            attributions = render_attributions(subject, materials, contracts, sources)
        basis = []
        for material in materials:
            contract_ref = material["contract_ref"]
            if voucher is not None:
                contract_version = voucher["rule_snapshot"]["contracts"].get(
                    contract_ref, contracts[contract_ref]["version"]
                )
            else:
                contract_version = contracts[contract_ref]["version"]
            basis.append(
                {
                    "material_ref": material["material_ref"],
                    "source_ref": material["source_ref"],
                    "contract_ref": contract_ref,
                    "contract_version": contract_version,
                }
            )
        items.append(
            {
                "subject_type": ref["subject_type"],
                "subject_ref": ref["subject_ref"],
                "license_basis": basis,
                "voucher_ref": voucher["voucher_ref"] if voucher else None,
                "attributions": attributions,
            }
        )

    open_cases = store.list_remediations(article_ref=article_ref, status="open")
    return {
        "article_ref": article_ref,
        "version_no": version["version_no"],
        "status": version["status"],
        "items": items,
        "open_remediations": open_cases,
    }
