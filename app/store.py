"""存储层：SQLite 持久化 + 业务事务。

追加式事实：合同收窄、来源撤回、禁发改期都不覆盖旧记录，
而是产生新版本/新状态行与新的审查结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import rules


class StoreError(Exception):
    """请求语义错误（4xx）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for path in sorted(migrations_dir.glob("*.sql")):
                if path.stem not in applied:
                    conn.executescript(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
                        (path.stem,),
                    )

    # ------------------------------------------------------------------ 事件

    def _event(self, conn: sqlite3.Connection, event_type: str, actor: str,
               source_ref: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO event_journal(event_type, occurred_at, actor, source_ref, payload)"
            " VALUES (?,?,?,?,?)",
            (event_type, now_iso(), actor, source_ref, canonical_json(payload)),
        )

    # ------------------------------------------------------------------ 来源

    def create_source(self, *, source_id: str | None = None, name: str, kind: str,
                      actor: str = "editor") -> dict[str, Any]:
        sid = source_id or new_id("src")
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO sources(source_id, name, kind, status, created_at)"
                    " VALUES (?,?,?, 'active', ?)",
                    (sid, name, kind, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"来源已存在：{sid}") from exc
            self._event(conn, "source.created", actor, sid, {"name": name, "kind": kind})
        return self.get_source(sid)

    def withdraw_source(self, source_id: str, *, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row:
                raise StoreError(f"来源不存在：{source_id}")
            if row["status"] == "withdrawn":
                raise StoreError("来源已处于撤回状态")
            conn.execute(
                "UPDATE sources SET status='withdrawn', withdrawn_at=? WHERE source_id=?",
                (now_iso(), source_id),
            )
            self._event(conn, "source.withdrawn", actor, source_id, {})
        return self.get_source(source_id)

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not row:
                raise StoreError(f"来源不存在：{source_id}")
            return dict(row)

    # ------------------------------------------------------------------ 合同

    def create_contract_version(
        self, *, source_id: str, version_label: str, valid_from: str,
        valid_to: str | None = None, allowed_sections: list[str] | None = None,
        allowed_regions: list[str] | None = None, allowed_channels: list[str] | None = None,
        attribution_template: str, derived_allowed: bool = False,
        derived_attribution_required: bool = True, quota_limit: int | None = None,
        terms_digest: str | None = None, actor: str = "legal",
        contract_version_id: str | None = None,
    ) -> dict[str, Any]:
        cid = contract_version_id or new_id("ctr")
        digest = terms_digest or hashlib.sha256(
            canonical_json({
                "version_label": version_label, "valid_from": valid_from,
                "valid_to": valid_to, "allowed_sections": allowed_sections or ["*"],
                "allowed_regions": allowed_regions or ["*"],
                "allowed_channels": allowed_channels or ["*"],
                "attribution_template": attribution_template,
                "derived_allowed": derived_allowed,
                "derived_attribution_required": derived_attribution_required,
                "quota_limit": quota_limit,
            }).encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
                raise StoreError(f"来源不存在：{source_id}")
            old = conn.execute(
                "SELECT contract_version_id FROM contract_versions"
                " WHERE source_id=? AND status='current'", (source_id,)
            ).fetchone()
            if old:
                conn.execute(
                    "UPDATE contract_versions SET status='superseded' WHERE contract_version_id=?",
                    (old["contract_version_id"],),
                )
            try:
                conn.execute(
                    "INSERT INTO contract_versions(contract_version_id, source_id, version_label,"
                    " status, valid_from, valid_to, allowed_sections, allowed_regions,"
                    " allowed_channels, attribution_template, derived_allowed,"
                    " derived_attribution_required, quota_limit, terms_digest, created_at)"
                    " VALUES (?,?,?, 'current', ?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, source_id, version_label, valid_from, valid_to,
                     canonical_json(allowed_sections or ["*"]),
                     canonical_json(allowed_regions or ["*"]),
                     canonical_json(allowed_channels or ["*"]),
                     attribution_template, int(derived_allowed),
                     int(derived_attribution_required), quota_limit, digest, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"合同版本已存在：{source_id}/{version_label}") from exc
            self._event(conn, "contract.version_created", actor, cid, {
                "source_id": source_id, "version_label": version_label,
                "supersedes": old["contract_version_id"] if old else None,
                "terms_digest": digest,
            })
        return self.get_contract_version(cid)

    def get_contract_version(self, contract_version_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM contract_versions WHERE contract_version_id=?",
                (contract_version_id,),
            ).fetchone()
            if not row:
                raise StoreError(f"合同版本不存在：{contract_version_id}")
            return self._contract_row(row)

    @staticmethod
    def _contract_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("allowed_sections", "allowed_regions", "allowed_channels"):
            data[f"{key}_json"] = json.loads(data.pop(key))
        return data

    def _current_contract(self, conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM contract_versions WHERE source_id=? AND status='current'",
            (source_id,),
        ).fetchone()
        return self._contract_row(row) if row else None

    # ------------------------------------------------------------------ 素材

    def create_material(
        self, *, source_id: str, series_ref: str, category: str, event_time: str,
        tz_name: str, unit: str, controlled_ref: str | None = None,
        payload_digest: str | None = None,
        contract_version_id: str | None = None, actor: str = "editor",
        material_id: str | None = None,
    ) -> dict[str, Any]:
        mid = material_id or new_id("mat")
        with self._connect() as conn:
            source = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not source:
                raise StoreError(f"来源不存在：{source_id}")
            if contract_version_id:
                crow = conn.execute(
                    "SELECT * FROM contract_versions WHERE contract_version_id=?",
                    (contract_version_id,),
                ).fetchone()
                if not crow:
                    raise StoreError(f"合同版本不存在：{contract_version_id}")
                cid = contract_version_id
            else:
                cur = self._current_contract(conn, source_id)
                if not cur:
                    raise StoreError(f"来源尚无有效合同版本：{source_id}")
                cid = cur["contract_version_id"]
            conn.execute(
                "INSERT INTO materials(material_id, source_id, acquired_contract_version_id,"
                " series_ref, category, event_time, tz_name, unit, controlled_ref,"
                " payload_digest, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'active',?)",
                (mid, source_id, cid, series_ref, category, event_time, tz_name, unit,
                 controlled_ref, payload_digest, now_iso()),
            )
            self._event(conn, "material.created", actor, mid, {
                "source_id": source_id, "contract_version_id": cid,
                "series_ref": series_ref, "category": category,
            })
        return self.get_material(mid)

    def withdraw_material(self, material_id: str, *, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM materials WHERE material_id=?", (material_id,)).fetchone()
            if not row:
                raise StoreError(f"素材不存在：{material_id}")
            if row["status"] == "withdrawn":
                raise StoreError("素材已处于撤回状态")
            conn.execute("UPDATE materials SET status='withdrawn' WHERE material_id=?", (material_id,))
            self._event(conn, "material.withdrawn", actor, material_id, {})
        return self.get_material(material_id)

    def get_material(self, material_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
            if not row:
                raise StoreError(f"素材不存在：{material_id}")
            return dict(row)

    # ------------------------------------------------------------------ 图表

    def create_chart(self, *, material_id: str, title: str,
                     upstream_material_ids: list[str] | None = None,
                     spec_digest: str | None = None, actor: str = "editor",
                     chart_id: str | None = None) -> dict[str, Any]:
        chid = chart_id or new_id("cht")
        upstream = list(dict.fromkeys(upstream_material_ids or []))
        with self._connect() as conn:
            mrow = conn.execute("SELECT * FROM materials WHERE material_id=?", (material_id,)).fetchone()
            if not mrow:
                raise StoreError(f"素材不存在：{material_id}")
            for uid in upstream + [material_id]:
                if not conn.execute("SELECT 1 FROM materials WHERE material_id=?", (uid,)).fetchone():
                    raise StoreError(f"上游素材不存在：{uid}")
            conn.execute(
                "INSERT INTO charts(chart_id, source_id, material_id, title, spec_digest,"
                " status, created_at) VALUES (?,?,?,?,?, 'active', ?)",
                (chid, mrow["source_id"], material_id, title, spec_digest, now_iso()),
            )
            for uid in upstream:
                conn.execute(
                    "INSERT OR IGNORE INTO chart_materials(chart_id, material_id) VALUES (?,?)",
                    (chid, uid),
                )
            self._event(conn, "chart.created", actor, chid, {
                "material_id": material_id, "upstream": upstream,
            })
        return self.get_chart(chid)

    def withdraw_chart(self, chart_id: str, *, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM charts WHERE chart_id=?", (chart_id,)).fetchone()
            if not row:
                raise StoreError(f"图表不存在：{chart_id}")
            if row["status"] == "withdrawn":
                raise StoreError("图表已处于撤回状态")
            conn.execute("UPDATE charts SET status='withdrawn' WHERE chart_id=?", (chart_id,))
            self._event(conn, "chart.withdrawn", actor, chart_id, {})
        return self.get_chart(chart_id)

    def get_chart(self, chart_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM charts WHERE chart_id=?", (chart_id,)).fetchone()
            if not row:
                raise StoreError(f"图表不存在：{chart_id}")
            data = dict(row)
            data["upstream_material_ids"] = [
                r[0] for r in conn.execute(
                    "SELECT material_id FROM chart_materials WHERE chart_id=?", (chart_id,)
                )
            ]
            return data

    # ------------------------------------------------------------------ 禁运

    def create_embargo(self, *, scope_type: str, scope_ref: str, embargo_until: str,
                       reason: str, created_by: str = "legal",
                       embargo_id: str | None = None) -> dict[str, Any]:
        eid = embargo_id or new_id("emb")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO embargoes(embargo_id, scope_type, scope_ref, embargo_until,"
                " reason, status, created_by, created_at)"
                " VALUES (?,?,?,?,?, 'active', ?,?)",
                (eid, scope_type, scope_ref, embargo_until, reason, created_by, now_iso()),
            )
            self._event(conn, "embargo.created", created_by, eid, {
                "scope_type": scope_type, "scope_ref": scope_ref,
                "embargo_until": embargo_until, "reason": reason,
            })
        return self.get_embargo(eid)

    def reschedule_embargo(self, embargo_id: str, *, new_until: str, actor: str = "legal") -> dict[str, Any]:
        """发布时间变动：旧禁运行标记 superseded，生成新行，旧审查结果不受影响。"""
        with self._connect() as conn:
            old = conn.execute("SELECT * FROM embargoes WHERE embargo_id=?", (embargo_id,)).fetchone()
            if not old:
                raise StoreError(f"禁运不存在：{embargo_id}")
            if old["status"] != "active":
                raise StoreError("仅可改期处于 active 状态的禁运")
            new_id_ = new_id("emb")
            conn.execute("UPDATE embargoes SET status='superseded' WHERE embargo_id=?", (embargo_id,))
            conn.execute(
                "INSERT INTO embargoes(embargo_id, scope_type, scope_ref, embargo_until,"
                " reason, status, supersedes_id, created_by, created_at)"
                " VALUES (?,?,?,?,?, 'active', ?,?,?)",
                (new_id_, old["scope_type"], old["scope_ref"], new_until, old["reason"],
                 embargo_id, actor, now_iso()),
            )
            self._event(conn, "embargo.rescheduled", actor, new_id_, {
                "supersedes": embargo_id, "embargo_until": old["embargo_until"],
                "new_until": new_until,
            })
        return self.get_embargo(new_id_)

    def lift_embargo(self, embargo_id: str, *, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM embargoes WHERE embargo_id=?", (embargo_id,)).fetchone()
            if not row:
                raise StoreError(f"禁运不存在：{embargo_id}")
            if row["status"] != "active":
                raise StoreError("仅可解除处于 active 状态的禁运")
            conn.execute("UPDATE embargoes SET status='lifted' WHERE embargo_id=?", (embargo_id,))
            self._event(conn, "embargo.lifted", actor, embargo_id, {})
        return self.get_embargo(embargo_id)

    def get_embargo(self, embargo_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM embargoes WHERE embargo_id=?", (embargo_id,)).fetchone()
            if not row:
                raise StoreError(f"禁运不存在：{embargo_id}")
            return dict(row)

    # ------------------------------------------------------------------ 例外

    def grant_exception(self, *, target_type: str, target_id: str = "*",
                        embargo_id: str | None = None, granted_by: str = "legal",
                        reason: str, expires_at: str | None = None,
                        exception_id: str | None = None) -> dict[str, Any]:
        eid = exception_id or new_id("exc")
        with self._connect() as conn:
            if embargo_id and not conn.execute(
                "SELECT 1 FROM embargoes WHERE embargo_id=?", (embargo_id,)
            ).fetchone():
                raise StoreError(f"禁运不存在：{embargo_id}")
            conn.execute(
                "INSERT INTO embargo_exceptions(exception_id, embargo_id, target_type,"
                " target_id, granted_by, granted_at, expires_at, reason)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (eid, embargo_id, target_type, target_id, granted_by, now_iso(),
                 expires_at, reason),
            )
            self._event(conn, "exception.granted", granted_by, eid, {
                "embargo_id": embargo_id, "target_type": target_type,
                "target_id": target_id, "expires_at": expires_at, "reason": reason,
            })
        return self.get_exception(eid)

    def revoke_exception(self, exception_id: str, *, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revoked_at FROM embargo_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"例外不存在：{exception_id}")
            if row["revoked_at"]:
                raise StoreError("例外已撤销")
            conn.execute(
                "UPDATE embargo_exceptions SET revoked_at=? WHERE exception_id=?",
                (now_iso(), exception_id),
            )
            self._event(conn, "exception.revoked", actor, exception_id, {})
        return self.get_exception(exception_id)

    def get_exception(self, exception_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM embargo_exceptions WHERE exception_id=?", (exception_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"例外不存在：{exception_id}")
            return dict(row)

    # --------------------------------------------------------- 评估上下文加载

    def _load_targets(self, conn: sqlite3.Connection,
                      refs: list[dict[str, str]]) -> list[rules.Target]:
        material_ids = {r["material_id"] for r in refs if r.get("material_id")}
        chart_specs: dict[str, list[str]] = {}
        for r in refs:
            if r.get("chart_id"):
                crow = conn.execute("SELECT * FROM charts WHERE chart_id=?", (r["chart_id"],)).fetchone()
                if not crow:
                    raise StoreError(f"图表不存在：{r['chart_id']}")
                ups = [x[0] for x in conn.execute(
                    "SELECT material_id FROM chart_materials WHERE chart_id=?", (r["chart_id"],)
                )]
                chart_specs[r["chart_id"]] = ups
                material_ids.add(crow["material_id"])
                material_ids.update(ups)

        materials: dict[str, dict[str, Any]] = {}
        for mid in material_ids:
            mrow = conn.execute("SELECT * FROM materials WHERE material_id=?", (mid,)).fetchone()
            if not mrow:
                raise StoreError(f"素材不存在：{mid}")
            acquired = conn.execute(
                "SELECT * FROM contract_versions WHERE contract_version_id=?",
                (mrow["acquired_contract_version_id"],),
            ).fetchone()
            # 评估依据为来源当前合同（收窄/扩宽立即生效，产生新的审查结果）；
            # 取得时锁定的合同版本仍保留在素材与快照中，供凭证与报告引用。
            current = conn.execute(
                "SELECT * FROM contract_versions WHERE source_id=? AND status='current'",
                (mrow["source_id"],),
            ).fetchone()
            srow = conn.execute("SELECT * FROM sources WHERE source_id=?", (mrow["source_id"],)).fetchone()
            m = dict(mrow)
            m["contract"] = self._contract_row(current or acquired)
            m["acquired_contract"] = self._contract_row(acquired)
            m["source"] = dict(srow)
            materials[mid] = m

        targets: list[rules.Target] = []
        for r in refs:
            if r.get("material_id"):
                m = materials[r["material_id"]]
                targets.append(rules.Target(
                    type="material", id=r["material_id"], material=m,
                    contract=m["contract"], source=m["source"]))
            else:
                chid = r["chart_id"]
                chrow = conn.execute("SELECT * FROM charts WHERE chart_id=?", (chid,)).fetchone()
                primary = materials[chrow["material_id"]]
                upstream = [primary] + [materials[u] for u in chart_specs[chid]]
                targets.append(rules.Target(
                    type="chart", id=chid, material=primary,
                    contract=primary["contract"], source=primary["source"],
                    chart=dict(chrow),
                    upstream=tuple(upstream),
                ))
        return targets

    def _load_embargoes(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute("SELECT * FROM embargoes WHERE status='active'")]

    def _load_exceptions(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM embargo_exceptions WHERE revoked_at IS NULL")]

    def _existing_holds(self, conn: sqlite3.Connection, article_id: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT h.source_id, COUNT(*) AS n FROM quota_holds h"
            " JOIN article_versions v ON v.version_id = h.version_id"
            " WHERE v.article_id != ? GROUP BY h.source_id", (article_id,),
        ).fetchall()
        return {r["source_id"]: r["n"] for r in rows}

    def _snapshot(self, *, targets: list[rules.Target], section: str, channels: list[str],
                  regions: list[str], planned_publish_at: str,
                  embargoes: list[dict[str, Any]], exceptions: list[dict[str, Any]],
                  result: dict[str, Any]) -> dict[str, Any]:
        return {
            "evaluated_at": now_iso(),
            "section": section,
            "channels": channels,
            "regions": regions,
            "planned_publish_at": planned_publish_at,
            "targets": [
                {
                    "type": t.type, "id": t.id,
                    "source_id": t.source["source_id"],
                    "source_status": t.source["status"],
                    "material_id": t.material["material_id"],
                    "material_status": t.material["status"],
                    "series_ref": t.material["series_ref"],
                    "category": t.material["category"],
                    "event_time": t.material["event_time"],
                    "tz_name": t.material["tz_name"],
                    "unit": t.material["unit"],
                    "contract_version_id": t.contract["contract_version_id"],
                    "contract_status": t.contract["status"],
                    "version_label": t.contract["version_label"],
                    "terms_digest": t.contract["terms_digest"],
                    "acquired_contract_version_id": t.material["acquired_contract"]["contract_version_id"],
                    "acquired_version_label": t.material["acquired_contract"]["version_label"],
                    "chart_id": t.chart["chart_id"] if t.chart else None,
                    "chart_status": t.chart["status"] if t.chart else None,
                    "upstream_material_ids": [u["material_id"] for u in t.upstream],
                }
                for t in targets
            ],
            "contracts": [
                {
                    "contract_version_id": t.contract["contract_version_id"],
                    "source_id": t.source["source_id"],
                    "version_label": t.contract["version_label"],
                    "status": t.contract["status"],
                    "allowed_sections": t.contract["allowed_sections_json"],
                    "allowed_regions": t.contract["allowed_regions_json"],
                    "allowed_channels": t.contract["allowed_channels_json"],
                    "attribution_template": t.contract["attribution_template"],
                    "derived_allowed": bool(t.contract["derived_allowed"]),
                    "derived_attribution_required": bool(t.contract["derived_attribution_required"]),
                    "quota_limit": t.contract["quota_limit"],
                    "terms_digest": t.contract["terms_digest"],
                }
                for t in {x.contract["contract_version_id"]: x for x in targets}.values()
            ],
            "embargoes": embargoes,
            "exceptions": exceptions,
            "result": result,
        }

    # ------------------------------------------------------------------ 文章

    def create_article(self, *, title: str, section: str, actor: str = "editor",
                       article_id: str | None = None) -> dict[str, Any]:
        aid = article_id or new_id("art")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO articles(article_id, title, section, created_at)"
                " VALUES (?,?,?,?)",
                (aid, title, section, now_iso()),
            )
            self._event(conn, "article.created", actor, aid, {"title": title, "section": section})
        return self.get_article(aid)

    def get_article(self, article_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
            if not row:
                raise StoreError(f"文章不存在：{article_id}")
            return dict(row)

    def create_version(
        self, *, article_id: str, planned_publish_at: str, planned_channels: list[str],
        planned_regions: list[str], refs: list[dict[str, str]],
        created_by: str = "editor", commit: bool = False,
    ) -> dict[str, Any]:
        """创建文章版本并立即给出针对计划时间/渠道/地区的可用性判断。

        refs: [{"material_id": ...} 或 {"chart_id": ...}]，即文章实际引用的素材。
        仅这些引用参与授权与额度；多人编辑中未被引用的草稿不占额度。
        commit=True 时通过才占用额度并固化为 committed 审查。
        """
        if not refs:
            raise StoreError("文章版本至少需要引用一个素材或图表")
        if not planned_channels:
            raise StoreError("至少需要一个对外发布渠道")
        if rules.INTERNAL_CHANNEL in planned_channels:
            raise StoreError(f"{rules.INTERNAL_CHANNEL} 为保留渠道")
        version_id = new_id("ver")
        review_id = new_id("rev")
        with self._connect() as conn:
            article = conn.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
            if not article:
                raise StoreError(f"文章不存在：{article_id}")
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(version_seq), 0) + 1 AS s FROM article_versions"
                " WHERE article_id=?", (article_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO article_versions(version_id, article_id, version_seq,"
                " planned_publish_at, planned_channels, planned_regions, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (version_id, article_id, seq_row["s"], planned_publish_at,
                 canonical_json(planned_channels), canonical_json(planned_regions),
                 created_by, now_iso()),
            )
            for ref in refs:
                conn.execute(
                    "INSERT INTO version_refs(ref_id, version_id, material_id, chart_id, quoted_at)"
                    " VALUES (?,?,?,?,?)",
                    (new_id("ref"), version_id, ref.get("material_id"), ref.get("chart_id"), now_iso()),
                )

            targets = self._load_targets(conn, refs)
            embargoes = self._load_embargoes(conn)
            exceptions = self._load_exceptions(conn)
            holds = self._existing_holds(conn, article_id)
            result = rules.evaluate_plan(
                targets, section=article["section"],
                channels=planned_channels, regions=planned_regions,
                planned_publish_at=planned_publish_at, now=now_iso(),
                embargoes=embargoes, exceptions=exceptions, existing_holds=holds,
            )
            snapshot = self._snapshot(
                targets=targets, section=article["section"],
                channels=planned_channels, regions=planned_regions,
                planned_publish_at=planned_publish_at,
                embargoes=embargoes, exceptions=exceptions, result=result,
            )
            kind = "committed" if commit else "preview"
            conn.execute(
                "INSERT INTO reviews(review_id, version_id, kind, planned_publish_at,"
                " planned_channels, planned_regions, section, verdict, rule_snapshot,"
                " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (review_id, version_id, kind, planned_publish_at,
                 canonical_json(planned_channels), canonical_json(planned_regions),
                 article["section"], result["verdict"], canonical_json(snapshot),
                 created_by, now_iso()),
            )
            for d in result["decisions"]:
                conn.execute(
                    "INSERT INTO review_decisions(review_id, channel, region, verdict)"
                    " VALUES (?,?,?,?)",
                    (review_id, d["channel"], d["region"], d["verdict"]),
                )
            for i, f in enumerate(result["findings"]):
                conn.execute(
                    "INSERT INTO review_findings(finding_id, review_id, target_type, target_id,"
                    " channel, region, code, severity, message) VALUES (?,?,?,?,?,?,?,?,?)",
                    (f"{review_id}_f{i}", review_id, f.get("target_type"), f.get("target_id"),
                     f.get("channel"), f.get("region"), f["code"], f["severity"], f["message"]),
                )

            effective_kind = "preview"
            if commit and result["verdict"] != "usable":
                # 提交未通过：审查记录降级为 preview 保留，不占用额度、不切换当前版本
                conn.execute(
                    "UPDATE reviews SET kind='preview' WHERE review_id=?", (review_id,)
                )
                self._event(conn, "version.commit_rejected", created_by,
                            version_id, {"article_id": article_id, "review_id": review_id,
                                         "verdict": result["verdict"]})
            else:
                effective_kind = "committed" if commit else "preview"
                if commit:
                    # 释放同一文章旧版本占用（只有当前版本占额）
                    old_version = conn.execute(
                        "SELECT version_id FROM article_versions WHERE article_id=? AND version_id !=?"
                        " ORDER BY version_seq DESC LIMIT 1", (article_id, version_id),
                    ).fetchone()
                    if old_version:
                        conn.execute("DELETE FROM quota_holds WHERE version_id=?",
                                     (old_version["version_id"],))
                    for t in targets:
                        conn.execute(
                            "INSERT INTO quota_holds(hold_id, source_id, version_id, target_type,"
                            " target_id, occupied_at) VALUES (?,?,?,?,?,?)",
                            (new_id("hold"), t.source["source_id"], version_id, t.type, t.id, now_iso()),
                        )
                    conn.execute(
                        "UPDATE articles SET current_version_id=? WHERE article_id=?",
                        (version_id, article_id),
                    )
                self._event(conn, f"version.{effective_kind}", created_by,
                            version_id, {"article_id": article_id, "review_id": review_id,
                                         "verdict": result["verdict"]})

        return {"version_id": version_id, "review_id": review_id,
                "kind": effective_kind, **self._review_payload(review_id)}

    def _review_payload(self, review_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if not row:
                raise StoreError(f"审查不存在：{review_id}")
            decisions = [dict(r) for r in conn.execute(
                "SELECT channel, region, verdict FROM review_decisions WHERE review_id=? ORDER BY rowid",
                (review_id,))]
            findings = [dict(r) for r in conn.execute(
                "SELECT target_type, target_id, channel, region, code, severity, message"
                " FROM review_findings WHERE review_id=? ORDER BY rowid", (review_id,))]
            snapshot = json.loads(row["rule_snapshot"])
            return {
                "verdict": row["verdict"], "kind": row["kind"],
                "version_id": row["version_id"],
                "planned_publish_at": row["planned_publish_at"],
                "planned_channels": json.loads(row["planned_channels"]),
                "planned_regions": json.loads(row["planned_regions"]),
                "section": row["section"],
                "decisions": decisions, "findings": findings,
                "attributions": snapshot["result"]["attributions"],
                "rule_snapshot": snapshot,
            }

    def get_review(self, review_id: str) -> dict[str, Any]:
        return self._review_payload(review_id)

    # ------------------------------------------------------------------ 凭证

    def issue_credential(self, *, review_id: str, issued_by: str = "legal") -> dict[str, Any]:
        """法务签发放行凭证：固定签发当时的规则快照。"""
        credential_id = new_id("crd")
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            if not row:
                raise StoreError(f"审查不存在：{review_id}")
            if row["kind"] != "committed":
                raise StoreError("仅可对已提交版本的 committed 审查签发凭证")
            if row["verdict"] != "usable":
                raise StoreError("审查未通过，不能签发放行凭证")
            snapshot = json.loads(row["rule_snapshot"])
            digest = hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()
            try:
                conn.execute(
                    "INSERT INTO clearance_credentials(credential_id, review_id, version_id,"
                    " channels, regions, required_attributions, rule_snapshot, snapshot_digest,"
                    " issued_by, issued_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (credential_id, review_id, row["version_id"],
                     row["planned_channels"], row["planned_regions"],
                     canonical_json(snapshot["result"]["attributions"]),
                     canonical_json(snapshot), digest, issued_by, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError("该审查已签发凭证") from exc
            self._event(conn, "credential.issued", issued_by, credential_id, {
                "review_id": review_id, "version_id": row["version_id"],
                "snapshot_digest": digest,
            })
        return self.get_credential(credential_id)

    def get_credential(self, credential_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clearance_credentials WHERE credential_id=?", (credential_id,)
            ).fetchone()
            if not row:
                raise StoreError(f"凭证不存在：{credential_id}")
            return {
                "credential_id": credential_id,
                "review_id": row["review_id"], "version_id": row["version_id"],
                "channels": json.loads(row["channels"]),
                "regions": json.loads(row["regions"]),
                "required_attributions": json.loads(row["required_attributions"]),
                "snapshot_digest": row["snapshot_digest"],
                "issued_by": row["issued_by"], "issued_at": row["issued_at"],
            }

    # ------------------------------------------------------------------ 发布

    def publish(self, *, credential_id: str, actor: str = "editor",
                channels: list[str] | None = None,
                regions: list[str] | None = None) -> list[dict[str, Any]]:
        """凭凭证发布。只能发布凭证覆盖的渠道×地区；已发布则幂等返回。"""
        with self._connect() as conn:
            crow = conn.execute(
                "SELECT * FROM clearance_credentials WHERE credential_id=?", (credential_id,)
            ).fetchone()
            if not crow:
                raise StoreError(f"凭证不存在：{credential_id}")
            allowed_channels = json.loads(crow["channels"])
            allowed_regions = json.loads(crow["regions"])
            use_channels = channels or allowed_channels
            use_regions = regions or allowed_regions
            bad = set(use_channels) - set(allowed_channels)
            if bad:
                raise StoreError(f"渠道不在凭证范围内：{sorted(bad)}")
            bad = set(use_regions) - set(allowed_regions)
            if bad:
                raise StoreError(f"地区不在凭证范围内：{sorted(bad)}")
            out: list[dict[str, Any]] = []
            for channel in use_channels:
                for region in use_regions:
                    existing = conn.execute(
                        "SELECT publication_id FROM publications"
                        " WHERE version_id=? AND channel=? AND region=?",
                        (crow["version_id"], channel, region),
                    ).fetchone()
                    if existing:
                        out.append(self._publication_payload(conn, existing["publication_id"]))
                        continue
                    pid = new_id("pub")
                    ts = now_iso()
                    conn.execute(
                        "INSERT INTO publications(publication_id, article_id, version_id,"
                        " credential_id, channel, region, status, published_at, updated_at)"
                        " SELECT ?, article_id, ?, ?, ?, ?, 'live', ?, ?"
                        " FROM article_versions WHERE version_id=?",
                        (pid, crow["version_id"], credential_id, channel, region, ts, ts,
                         crow["version_id"]),
                    )
                    self._event(conn, "publication.live", actor, pid, {
                        "credential_id": credential_id, "channel": channel, "region": region,
                    })
                    out.append(self._publication_payload(conn, pid))
            return out

    def _publication_payload(self, conn: sqlite3.Connection, publication_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM publications WHERE publication_id=?", (publication_id,)).fetchone()
        return dict(row)

    # ------------------------------------------------------------------ 处置

    def open_disposition(self, *, publication_id: str, action_type: str, reason: str,
                         assignee: str | None = None, actor: str = "legal") -> dict[str, Any]:
        """已发布页面的补署名 / 替换 / 下架工单。"""
        did = new_id("dsp")
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM publications WHERE publication_id=?",
                                (publication_id,)).fetchone():
                raise StoreError(f"发布记录不存在：{publication_id}")
            conn.execute(
                "INSERT INTO dispositions(disposition_id, publication_id, action_type, reason,"
                " status, assignee, opened_at) VALUES (?,?,?,?,'open',?,?)",
                (did, publication_id, action_type, reason, assignee, now_iso()),
            )
            self._event(conn, "disposition.opened", actor, did, {
                "publication_id": publication_id, "action_type": action_type,
                "reason": reason, "assignee": assignee,
            })
        return self.get_disposition(did)

    def update_disposition(self, disposition_id: str, *, status: str,
                           note: str | None = None, actor: str = "legal") -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dispositions WHERE disposition_id=?",
                               (disposition_id,)).fetchone()
            if not row:
                raise StoreError(f"处置工单不存在：{disposition_id}")
            closed_at = now_iso() if status == "closed" and row["status"] != "closed" else row["closed_at"]
            conn.execute(
                "UPDATE dispositions SET status=?, note=COALESCE(?, note), closed_at=?"
                " WHERE disposition_id=?",
                (status, note, closed_at, disposition_id),
            )
            self._event(conn, "disposition.updated", actor, disposition_id, {"status": status})
        return self.get_disposition(disposition_id)

    def get_disposition(self, disposition_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dispositions WHERE disposition_id=?",
                               (disposition_id,)).fetchone()
            if not row:
                raise StoreError(f"处置工单不存在：{disposition_id}")
            return dict(row)

    # ------------------------------------------------------------------ 文章报告

    def article_report(self, article_id: str) -> dict[str, Any]:
        """任一文章列出：许可依据、应展示署名、已发布渠道与未闭环处置。"""
        with self._connect() as conn:
            article = conn.execute("SELECT * FROM articles WHERE article_id=?", (article_id,)).fetchone()
            if not article:
                raise StoreError(f"文章不存在：{article_id}")
            versions = [dict(r) for r in conn.execute(
                "SELECT version_id, version_seq, planned_publish_at, planned_channels,"
                " planned_regions, created_at FROM article_versions"
                " WHERE article_id=? ORDER BY version_seq", (article_id,))]
            current_id = article["current_version_id"]
            credential = None
            snapshot = None
            if current_id:
                crd = conn.execute(
                    "SELECT * FROM clearance_credentials WHERE version_id=?", (current_id,)
                ).fetchone()
                if crd:
                    snapshot = json.loads(crd["rule_snapshot"])
                    credential = {
                        "credential_id": crd["credential_id"],
                        "issued_by": crd["issued_by"], "issued_at": crd["issued_at"],
                        "snapshot_digest": crd["snapshot_digest"],
                        "channels": json.loads(crd["channels"]),
                        "regions": json.loads(crd["regions"]),
                        "attributions": json.loads(crd["required_attributions"]),
                    }
            refs: list[dict[str, Any]] = []
            if snapshot:
                for t in snapshot["targets"]:
                    refs.append({
                        "target": f"{t['type']}:{t['id']}",
                        "license_basis": {
                            "source_id": t["source_id"],
                            "source_status_at_review": t["source_status"],
                            "contract_version_id": t["contract_version_id"],
                            "contract_version_label": t["version_label"],
                            "contract_status_at_review": t["contract_status"],
                            "terms_digest": t["terms_digest"],
                            "series_ref": t["series_ref"],
                            "category": t["category"],
                            "event_time_original_tz": t["event_time"],
                            "tz_name": t["tz_name"],
                            "unit": t["unit"],
                        },
                        "attribution": snapshot["result"]["attributions"][f"{t['type']}:{t['id']}"],
                    })
            publications = []
            open_dispositions = []
            for pub in conn.execute(
                "SELECT * FROM publications WHERE article_id=? ORDER BY published_at", (article_id,)
            ):
                dsps = [dict(r) for r in conn.execute(
                    "SELECT disposition_id, action_type, reason, status, assignee, note,"
                    " opened_at, closed_at FROM dispositions WHERE publication_id=?"
                    " ORDER BY opened_at", (pub["publication_id"],))]
                publications.append({**dict(pub), "dispositions": dsps})
                for d in dsps:
                    if d["status"] != "closed":
                        open_dispositions.append({
                            "publication_id": pub["publication_id"],
                            "channel": pub["channel"], "region": pub["region"],
                            **d,
                        })
            return {
                "article_id": article_id,
                "title": article["title"],
                "section": article["section"],
                "current_version_id": current_id,
                "versions": versions,
                "credential": credential,
                "license_basis": refs,
                "publications": publications,
                "open_dispositions": open_dispositions,
            }
