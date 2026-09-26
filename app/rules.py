"""授权可用性规则引擎（纯函数，不访问数据库与时间源）。

输入均为普通字典，便于在审查时整体快照、在凭证中重放。
服务只管理使用权与发布边界：规则引擎不产生行情结论或交易建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

# 渠道：仅内部审稿，不构成对外发布
INTERNAL_CHANNEL = "internal_review"

# finding 代码
F_SOURCE_WITHDRAWN = "source_withdrawn"
F_MATERIAL_WITHDRAWN = "material_withdrawn"
F_CHART_WITHDRAWN = "chart_withdrawn"
F_CONTRACT_INACTIVE = "contract_inactive"
F_SECTION_DENIED = "section_denied"
F_REGION_DENIED = "region_denied"
F_CHANNEL_DENIED = "channel_denied"
F_DERIVED_DENIED = "derived_not_allowed"
F_EMBARGO = "embargo_active"
F_QUOTA = "quota_exceeded"
F_ATTRIBUTION = "attribution_required"
F_EXCEPTION = "embargo_exception"
F_CHAIN_DENIED = "derived_upstream_denied"
F_INTERNAL = "internal_review_only"


@dataclass(frozen=True)
class Target:
    """被评估的一个素材或图表。"""

    type: str  # 'material' | 'chart'
    id: str
    material: dict[str, Any]
    contract: dict[str, Any]
    source: dict[str, Any]
    chart: dict[str, Any] | None = None
    # 图表的上游素材链（含直接来源素材）
    upstream: tuple[dict[str, Any], ...] = ()


def _matches(allowed: Iterable[str], requested: str) -> bool:
    allowed = list(allowed)
    return "*" in allowed or requested in allowed


def _any_match(allowed: Iterable[str], requested: Iterable[str]) -> bool:
    """任一请求值命中即可（地区取交集发布：交集为空才整体阻断）。"""
    allowed = list(allowed)
    if "*" in allowed:
        return True
    return any(r in allowed for r in requested)


def parse_ts(value: str) -> float:
    """带偏移量 ISO 8601 -> POSIX 秒数。"""
    from datetime import datetime

    return datetime.fromisoformat(value).timestamp()


def embargo_active(embargo: dict[str, Any], now_ts: float) -> bool:
    return embargo["status"] == "active" and parse_ts(embargo["embargo_until"]) > now_ts


def exception_covers(
    exception: dict[str, Any],
    target_type: str,
    target_id: str,
    embargo: dict[str, Any] | None,
    now_ts: float,
    publish_ts: float | None = None,
) -> bool:
    # 撤销以审查当下为准；到期以计划发布时刻为准（发布时已失效的例外不覆盖）
    if exception.get("revoked_at") and parse_ts(exception["revoked_at"]) <= now_ts:
        return False
    expiry_ref = publish_ts if publish_ts is not None else now_ts
    if exception.get("expires_at") and parse_ts(exception["expires_at"]) <= expiry_ref:
        return False
    if embargo is not None and exception.get("embargo_id") not in (None, embargo["embargo_id"]):
        return False
    if exception["target_type"] == "any":
        return True
    return exception["target_type"] == target_type and exception["target_id"] == target_id


def _embargoes_for_target(
    target: Target, embargoes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    hit: list[dict[str, Any]] = []
    material_id = target.material["material_id"]
    source_id = target.material["source_id"]
    category = target.material["category"]
    chart_id = target.chart["chart_id"] if target.chart else None
    for emb in embargoes:
        if emb["scope_type"] == "global":
            hit.append(emb)
        elif emb["scope_type"] == "category" and emb["scope_ref"] == category:
            hit.append(emb)
        elif emb["scope_type"] == "source" and emb["scope_ref"] == source_id:
            hit.append(emb)
        elif emb["scope_type"] == "material" and emb["scope_ref"] == material_id:
            hit.append(emb)
        elif emb["scope_type"] == "chart" and chart_id and emb["scope_ref"] == chart_id:
            hit.append(emb)
    return hit


def _attribution_for(target: Target) -> str:
    """衍生图表在合同要求时同时展示上游与衍生署名。"""
    template = target.contract["attribution_template"]
    if target.type == "chart" and target.contract.get("derived_attribution_required", 1):
        # 上游署名（含主素材）去重保序，再与衍生图表署名拼接
        upstream_parts: list[str] = []
        for m in target.upstream:
            text = m["contract"]["attribution_template"]
            if text not in upstream_parts:
                upstream_parts.append(text)
        chart_label = f"图表：{template}"
        parts = upstream_parts + ([chart_label] if chart_label not in upstream_parts else [])
        return "；".join(parts)
    return template


def evaluate_target(
    target: Target,
    *,
    section: str,
    channel: str,
    region: str,
    publish_ts: float,
    now_ts: float,
    embargoes: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
    check_quota: bool = True,
    extra_holds: int = 0,
) -> dict[str, Any]:
    """单个 素材/图表 × 渠道 × 地区 的判断。

    返回 {verdict, findings:[{code,severity,message,embargo_id?}], attribution}
    """
    findings: list[dict[str, Any]] = []
    material = target.material
    contract = target.contract
    source = target.source

    # 内部审稿渠道：只给信息性结论，绝不等于放行
    if channel == INTERNAL_CHANNEL:
        findings.append(
            {"code": F_INTERNAL, "severity": "info",
             "message": "内部审稿预览，不构成对外发布授权"}
        )

    if source["status"] != "active":
        findings.append({"code": F_SOURCE_WITHDRAWN, "severity": "block",
                         "message": f"来源已撤回：{source['source_id']}"})
    if material["status"] != "active":
        findings.append({"code": F_MATERIAL_WITHDRAWN, "severity": "block",
                         "message": f"素材已撤回：{material['material_id']}"})
    if target.chart and target.chart["status"] != "active":
        findings.append({"code": F_CHART_WITHDRAWN, "severity": "block",
                         "message": f"图表已撤回：{target.chart['chart_id']}"})

    if contract["status"] != "current":
        findings.append({"code": F_CONTRACT_INACTIVE, "severity": "block",
                         "message": f"合同版本已失效：{contract['contract_version_id']}"})
    else:
        if not _matches(contract["allowed_sections_json"], section):
            findings.append({"code": F_SECTION_DENIED, "severity": "block",
                             "message": f"栏目 {section} 不在授权范围"})
        if not _matches(contract["allowed_regions_json"], region):
            findings.append({"code": F_REGION_DENIED, "severity": "block",
                             "message": f"地区 {region} 不在授权范围"})
        if not _matches(contract["allowed_channels_json"], channel) and channel != INTERNAL_CHANNEL:
            findings.append({"code": F_CHANNEL_DENIED, "severity": "block",
                             "message": f"渠道 {channel} 不在授权范围"})

    # 衍生授权
    if target.type == "chart":
        if not contract.get("derived_allowed", 0) and channel != INTERNAL_CHANNEL:
            findings.append({"code": F_DERIVED_DENIED, "severity": "block",
                             "message": "合同不允许制作衍生图表"})
        # 上游任一素材在该渠道/地区不可用，图表同样不可用
        for up in target.upstream:
            up_contract = up["contract"]
            if up_contract["status"] != "current":
                findings.append({"code": F_CHAIN_DENIED, "severity": "block",
                                 "message": f"上游素材 {up['material_id']} 合同失效"})
                continue
            if (not _matches(up_contract["allowed_channels_json"], channel)
                    and channel != INTERNAL_CHANNEL):
                findings.append({"code": F_CHAIN_DENIED, "severity": "block",
                                 "message": f"上游素材 {up['material_id']} 未授权渠道 {channel}"})
            if not _matches(up_contract["allowed_regions_json"], region):
                findings.append({"code": F_CHAIN_DENIED, "severity": "block",
                                 "message": f"上游素材 {up['material_id']} 未授权地区 {region}"})

    # 禁发时点：以发布时间判断；被有效例外覆盖则降级为信息
    for emb in _embargoes_for_target(target, embargoes):
        if parse_ts(emb["embargo_until"]) > publish_ts:
            covering = next(
                (e for e in exceptions
                 if exception_covers(e, target.type, target.id, emb, now_ts, publish_ts)),
                None,
            )
            if covering:
                findings.append({"code": F_EXCEPTION, "severity": "info",
                                 "message": f"禁发由例外 {covering['exception_id']} 覆盖：{emb['reason']}",
                                 "embargo_id": emb["embargo_id"],
                                 "exception_id": covering["exception_id"]})
            else:
                findings.append({"code": F_EMBARGO, "severity": "block",
                                 "message": f"禁发至 {emb['embargo_until']}：{emb['reason']}",
                                 "embargo_id": emb["embargo_id"]})

    # 额度（计划级预扫描时关闭，避免多渠道重复计数）
    quota = contract.get("quota_limit")
    if check_quota and quota is not None and channel != INTERNAL_CHANNEL:
        if extra_holds + 1 > quota:
            findings.append({"code": F_QUOTA, "severity": "block",
                             "message": f"授权额度不足（已占用 {extra_holds}/{quota}）"})

    # 署名义务（非阻断）
    attribution = _attribution_for(target)
    findings.append({"code": F_ATTRIBUTION, "severity": "obligation",
                     "message": f"须展示署名：{attribution}"})

    blocked = any(f["severity"] == "block" for f in findings)
    return {
        "verdict": "blocked" if blocked else "usable",
        "findings": findings,
        "attribution": attribution,
    }


def evaluate_plan(
    targets: list[Target],
    *,
    section: str,
    channels: list[str],
    regions: list[str],
    planned_publish_at: str,
    now: str,
    embargoes: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
    existing_holds: dict[str, int] | None = None,
) -> dict[str, Any]:
    """对一个发布计划（多渠道 × 多地区）评估全部被引用素材。

    existing_holds: contract_version_id -> 其他文章当前版本已占用额度数。
    额度按“被引用素材”计数：同一素材在多渠道多地区发布只占用一个额度。
    额度归属来源（同一来源同时只有一个 current 合同，换版本后计数连续）。
    """
    now_ts = parse_ts(now)
    publish_ts = parse_ts(planned_publish_at)
    existing_holds = dict(existing_holds or {})

    decisions: list[dict[str, Any]] = []
    all_findings: list[dict[str, Any]] = []
    attributions: dict[str, str] = {}

    eval_channels = list(channels)
    if INTERNAL_CHANNEL not in eval_channels:
        eval_channels = [INTERNAL_CHANNEL] + eval_channels

    # 目标级预扫描：额度（每个素材只计一次）与署名
    quota_blocked: set[str] = set()
    local_holds: dict[str, int] = {}
    for target in targets:
        key = f"{target.type}:{target.id}"
        attributions[key] = _attribution_for(target)
        sid = target.source["source_id"]
        quota = target.contract.get("quota_limit")
        if quota is not None:
            used = existing_holds.get(sid, 0) + local_holds.get(sid, 0)
            if used + 1 > quota:
                quota_blocked.add(key)
                all_findings.append({
                    "target_type": target.type, "target_id": target.id,
                    "channel": None, "region": None,
                    "code": F_QUOTA, "severity": "block",
                    "message": f"授权额度不足（已占用 {used}/{quota}）",
                })
            else:
                local_holds[sid] = used + 1

    for channel in eval_channels:
        for region in regions:
            channel_blocked = False
            for target in targets:
                key = f"{target.type}:{target.id}"
                result = evaluate_target(
                    target, section=section, channel=channel, region=region,
                    publish_ts=publish_ts, now_ts=now_ts,
                    embargoes=embargoes, exceptions=exceptions, check_quota=False,
                )
                for f in result["findings"]:
                    all_findings.append({
                        "target_type": target.type, "target_id": target.id,
                        "channel": channel, "region": region,
                        "code": f["code"], "severity": f["severity"],
                        "message": f["message"],
                        **({"embargo_id": f["embargo_id"]} if "embargo_id" in f else {}),
                        **({"exception_id": f["exception_id"]} if "exception_id" in f else {}),
                    })
                if result["verdict"] == "blocked":
                    channel_blocked = True
                if channel != INTERNAL_CHANNEL and key in quota_blocked:
                    channel_blocked = True
            decisions.append({
                "channel": channel, "region": region,
                "verdict": "blocked" if channel_blocked else "usable",
            })

    external = [d for d in decisions if d["channel"] != INTERNAL_CHANNEL]
    overall = "usable" if external and all(d["verdict"] == "usable" for d in external) else "blocked"
    return {
        "verdict": overall,
        "decisions": decisions,
        "findings": all_findings,
        "attributions": attributions,
    }
