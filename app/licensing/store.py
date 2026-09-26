"""SQLite 持久层：行与 Python 对象之间的转换集中在这里。"""

from __future__ import annotations

import json
import sqlite3
import threading


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(text: str) -> object:
    return json.loads(text)


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _run(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # ---- 来源 ----

    def insert_source(self, row: dict) -> None:
        self._run(
            "INSERT INTO sources(source_ref, kind, display_name, status, withdrawn_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["source_ref"],
                row["kind"],
                row["display_name"],
                row.get("status", "active"),
                row.get("withdrawn_at"),
                row["created_at"],
            ),
        )

    def get_source(self, source_ref: str) -> dict | None:
        return self._one("SELECT * FROM sources WHERE source_ref = ?", (source_ref,))

    def mark_source_withdrawn(self, source_ref: str, withdrawn_at: str) -> None:
        self._run(
            "UPDATE sources SET status = 'withdrawn', withdrawn_at = ? WHERE source_ref = ?",
            (withdrawn_at, source_ref),
        )

    # ---- 合同 ----

    @staticmethod
    def _decode_contract(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        for field in ("allowed_sections", "allowed_regions", "allowed_channels"):
            row[field] = _load(row[field])
        row["attribution_required"] = bool(row["attribution_required"])
        return row

    def insert_contract(self, row: dict) -> None:
        self._run(
            "INSERT INTO contracts(contract_ref, version, source_ref, effective_from, effective_to,"
            " allowed_sections, allowed_regions, allowed_channels, attribution_required,"
            " attribution_template, quota, change_reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["contract_ref"],
                row["version"],
                row["source_ref"],
                row["effective_from"],
                row.get("effective_to"),
                _dump(row["allowed_sections"]),
                _dump(row["allowed_regions"]),
                _dump(row["allowed_channels"]),
                1 if row["attribution_required"] else 0,
                row["attribution_template"],
                row.get("quota"),
                row.get("change_reason"),
                row["created_at"],
            ),
        )

    def get_contract(self, contract_ref: str, version: int) -> dict | None:
        row = self._one(
            "SELECT * FROM contracts WHERE contract_ref = ? AND version = ?",
            (contract_ref, version),
        )
        return self._decode_contract(row)

    def get_latest_contract(self, contract_ref: str) -> dict | None:
        row = self._one(
            "SELECT * FROM contracts WHERE contract_ref = ? ORDER BY version DESC LIMIT 1",
            (contract_ref,),
        )
        return self._decode_contract(row)

    def list_contract_versions(self, contract_ref: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM contracts WHERE contract_ref = ? ORDER BY version", (contract_ref,)
        )
        return [self._decode_contract(row) for row in rows]

    # ---- 素材 ----

    def insert_material(self, row: dict) -> None:
        self._run(
            "INSERT INTO materials(material_ref, source_ref, contract_ref, contract_version, kind,"
            " acquired_at, timezone, unit, payload_ref, payload_sha256, status, withdrawn_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["material_ref"],
                row["source_ref"],
                row["contract_ref"],
                row["contract_version"],
                row["kind"],
                row["acquired_at"],
                row.get("timezone"),
                row["unit"],
                row.get("payload_ref"),
                row.get("payload_sha256"),
                row.get("status", "available"),
                row.get("withdrawn_at"),
                row["created_at"],
            ),
        )

    def get_material(self, material_ref: str) -> dict | None:
        return self._one("SELECT * FROM materials WHERE material_ref = ?", (material_ref,))

    def mark_material_withdrawn(self, material_ref: str, withdrawn_at: str) -> None:
        self._run(
            "UPDATE materials SET status = 'withdrawn', withdrawn_at = ? WHERE material_ref = ?",
            (withdrawn_at, material_ref),
        )

    def list_materials_by_contract(self, contract_ref: str) -> list[dict]:
        return self._all(
            "SELECT * FROM materials WHERE contract_ref = ? ORDER BY created_at", (contract_ref,)
        )

    def list_materials_by_source(self, source_ref: str) -> list[dict]:
        return self._all(
            "SELECT * FROM materials WHERE source_ref = ? ORDER BY created_at", (source_ref,)
        )

    def list_materials(self) -> list[dict]:
        return self._all("SELECT * FROM materials ORDER BY created_at")

    # ---- 衍生图表 ----

    def insert_derivative(self, row: dict, parents: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO derivatives(derivative_ref, attribution_text, note, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    row["derivative_ref"],
                    row.get("attribution_text"),
                    row.get("note"),
                    row.get("created_by"),
                    row["created_at"],
                ),
            )
            for material_ref in parents:
                self._conn.execute(
                    "INSERT INTO derivative_parents(derivative_ref, material_ref) VALUES (?, ?)",
                    (row["derivative_ref"], material_ref),
                )
            self._conn.commit()

    def get_derivative(self, derivative_ref: str) -> dict | None:
        return self._one("SELECT * FROM derivatives WHERE derivative_ref = ?", (derivative_ref,))

    def get_derivative_parents(self, derivative_ref: str) -> list[dict]:
        return self._all(
            "SELECT m.* FROM derivative_parents dp JOIN materials m ON m.material_ref = dp.material_ref"
            " WHERE dp.derivative_ref = ? ORDER BY m.material_ref",
            (derivative_ref,),
        )

    def find_derivatives_with_parents(self, material_refs: list[str]) -> list[str]:
        if not material_refs:
            return []
        placeholders = ",".join("?" for _ in material_refs)
        rows = self._all(
            f"SELECT DISTINCT derivative_ref FROM derivative_parents WHERE material_ref IN ({placeholders})",
            tuple(material_refs),
        )
        return [row["derivative_ref"] for row in rows]

    def add_derivative_editor(self, derivative_ref: str, editor_ref: str, joined_at: str) -> None:
        self._run(
            "INSERT OR IGNORE INTO derivative_editors(derivative_ref, editor_ref, joined_at)"
            " VALUES (?, ?, ?)",
            (derivative_ref, editor_ref, joined_at),
        )

    def list_derivative_editors(self, derivative_ref: str) -> list[str]:
        rows = self._all(
            "SELECT editor_ref FROM derivative_editors WHERE derivative_ref = ? ORDER BY joined_at",
            (derivative_ref,),
        )
        return [row["editor_ref"] for row in rows]

    # ---- 禁发与例外批准 ----

    def insert_embargo(self, row: dict) -> None:
        self._run(
            "INSERT INTO embargoes(embargo_ref, event_ref, source_ref, kind, channel, region,"
            " embargo_until, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["embargo_ref"],
                row["event_ref"],
                row.get("source_ref"),
                row.get("kind"),
                row.get("channel"),
                row.get("region"),
                row["embargo_until"],
                row["created_at"],
            ),
        )

    def get_embargo(self, embargo_ref: str) -> dict | None:
        return self._one("SELECT * FROM embargoes WHERE embargo_ref = ?", (embargo_ref,))

    def list_embargoes(self) -> list[dict]:
        return self._all("SELECT * FROM embargoes ORDER BY created_at")

    def insert_approval(self, row: dict) -> None:
        self._run(
            "INSERT INTO embargo_approvals(approval_ref, embargo_ref, approver_ref, channel, section,"
            " region, expires_at, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["approval_ref"],
                row["embargo_ref"],
                row["approver_ref"],
                row.get("channel"),
                row.get("section"),
                row.get("region"),
                row.get("expires_at"),
                row["reason"],
                row["created_at"],
            ),
        )

    def list_approvals(self, embargo_ref: str) -> list[dict]:
        return self._all(
            "SELECT * FROM embargo_approvals WHERE embargo_ref = ? ORDER BY created_at",
            (embargo_ref,),
        )

    # ---- 审查结果（只增不改） ----

    @staticmethod
    def _decode_review(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        for field in ("reasons", "attributions", "rule_snapshot"):
            row[field] = _load(row[field])
        return row

    def insert_review(self, row: dict) -> None:
        self._run(
            "INSERT INTO reviews(review_ref, subject_type, subject_ref, planned_publish_at, channel,"
            " section, region, verdict, reasons, attributions, rule_snapshot, trigger, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["review_ref"],
                row["subject_type"],
                row["subject_ref"],
                row["planned_publish_at"],
                row["channel"],
                row["section"],
                row["region"],
                row["verdict"],
                _dump(row["reasons"]),
                _dump(row["attributions"]),
                _dump(row["rule_snapshot"]),
                row["trigger"],
                row["created_at"],
            ),
        )

    def get_review(self, review_ref: str) -> dict | None:
        return self._decode_review(
            self._one("SELECT * FROM reviews WHERE review_ref = ?", (review_ref,))
        )

    def list_reviews_for_subject(self, subject_type: str, subject_ref: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM reviews WHERE subject_type = ? AND subject_ref = ? ORDER BY created_at",
            (subject_type, subject_ref),
        )
        return [self._decode_review(row) for row in rows]

    # ---- 文章与版本 ----

    def insert_article(self, row: dict) -> None:
        self._run(
            "INSERT INTO articles(article_ref, desk, created_at) VALUES (?, ?, ?)",
            (row["article_ref"], row.get("desk"), row["created_at"]),
        )

    def get_article(self, article_ref: str) -> dict | None:
        return self._one("SELECT * FROM articles WHERE article_ref = ?", (article_ref,))

    @staticmethod
    def _decode_version(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        row["channels"] = _load(row["channels"])
        row["references"] = _load(row.pop("references_json"))
        return row

    def insert_article_version(self, row: dict) -> None:
        self._run(
            "INSERT INTO article_versions(article_ref, version_no, channels, section, region,"
            " planned_publish_at, references_json, status, published_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["article_ref"],
                row["version_no"],
                _dump(row["channels"]),
                row["section"],
                row["region"],
                row["planned_publish_at"],
                _dump(row["references"]),
                row.get("status", "draft"),
                row.get("published_at"),
                row["created_at"],
            ),
        )

    def get_article_version(self, article_ref: str, version_no: int) -> dict | None:
        row = self._one(
            "SELECT * FROM article_versions WHERE article_ref = ? AND version_no = ?",
            (article_ref, version_no),
        )
        return self._decode_version(row)

    def get_latest_version(self, article_ref: str) -> dict | None:
        row = self._one(
            "SELECT * FROM article_versions WHERE article_ref = ? ORDER BY version_no DESC LIMIT 1",
            (article_ref,),
        )
        return self._decode_version(row)

    def get_published_version(self, article_ref: str) -> dict | None:
        row = self._one(
            "SELECT * FROM article_versions WHERE article_ref = ? AND status = 'published'"
            " ORDER BY version_no DESC LIMIT 1",
            (article_ref,),
        )
        return self._decode_version(row)

    def list_published_versions(self) -> list[dict]:
        rows = self._all(
            "SELECT * FROM article_versions WHERE status = 'published' ORDER BY article_ref"
        )
        return [self._decode_version(row) for row in rows]

    def list_latest_versions(self) -> list[dict]:
        """每篇文章的最新版本（含草稿），用于规则变化时生成新的审查结果。"""
        rows = self._all(
            "SELECT av.* FROM article_versions av"
            " JOIN (SELECT article_ref, MAX(version_no) AS max_no"
            " FROM article_versions GROUP BY article_ref) latest"
            " ON av.article_ref = latest.article_ref AND av.version_no = latest.max_no"
        )
        return [self._decode_version(row) for row in rows]

    def update_version_planned(self, article_ref: str, version_no: int, planned_publish_at: str) -> None:
        self._run(
            "UPDATE article_versions SET planned_publish_at = ? WHERE article_ref = ? AND version_no = ?",
            (planned_publish_at, article_ref, version_no),
        )

    def set_version_status(
        self, article_ref: str, version_no: int, status: str, published_at: str | None = None
    ) -> None:
        self._run(
            "UPDATE article_versions SET status = ?, published_at = COALESCE(?, published_at)"
            " WHERE article_ref = ? AND version_no = ?",
            (status, published_at, article_ref, version_no),
        )

    # ---- 放行凭证 ----

    @staticmethod
    def _decode_voucher(row: dict | None) -> dict | None:
        if row is None:
            return None
        row = dict(row)
        row["rule_snapshot"] = _load(row["rule_snapshot"])
        return row

    def insert_voucher(self, row: dict) -> None:
        self._run(
            "INSERT INTO vouchers(voucher_ref, article_ref, version_no, issued_by, rule_snapshot,"
            " status, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["voucher_ref"],
                row["article_ref"],
                row["version_no"],
                row["issued_by"],
                _dump(row["rule_snapshot"]),
                row.get("status", "active"),
                row["issued_at"],
            ),
        )

    def get_voucher(self, voucher_ref: str) -> dict | None:
        return self._decode_voucher(
            self._one("SELECT * FROM vouchers WHERE voucher_ref = ?", (voucher_ref,))
        )

    def get_active_voucher(self, article_ref: str, version_no: int) -> dict | None:
        row = self._one(
            "SELECT * FROM vouchers WHERE article_ref = ? AND version_no = ? AND status = 'active'"
            " ORDER BY issued_at DESC LIMIT 1",
            (article_ref, version_no),
        )
        return self._decode_voucher(row)

    # ---- 处置 ----

    def insert_remediation(self, row: dict) -> None:
        self._run(
            "INSERT INTO remediations(case_ref, article_ref, version_no, channel, subject_type,"
            " subject_ref, action_type, reason, review_ref, status, opened_at, closed_at,"
            " resolution_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["case_ref"],
                row["article_ref"],
                row.get("version_no"),
                row["channel"],
                row.get("subject_type"),
                row.get("subject_ref"),
                row["action_type"],
                row["reason"],
                row.get("review_ref"),
                row.get("status", "open"),
                row["opened_at"],
                row.get("closed_at"),
                row.get("resolution_note"),
            ),
        )

    def get_remediation(self, case_ref: str) -> dict | None:
        return self._one("SELECT * FROM remediations WHERE case_ref = ?", (case_ref,))

    def find_open_remediation(
        self, article_ref: str, channel: str, subject_ref: str | None, action_type: str
    ) -> dict | None:
        if subject_ref is None:
            return self._one(
                "SELECT * FROM remediations WHERE article_ref = ? AND channel = ? AND subject_ref IS NULL"
                " AND action_type = ? AND status = 'open'",
                (article_ref, channel, action_type),
            )
        return self._one(
            "SELECT * FROM remediations WHERE article_ref = ? AND channel = ? AND subject_ref = ?"
            " AND action_type = ? AND status = 'open'",
            (article_ref, channel, subject_ref, action_type),
        )

    def close_remediation(self, case_ref: str, closed_at: str, resolution_note: str | None) -> None:
        self._run(
            "UPDATE remediations SET status = 'closed', closed_at = ?, resolution_note = ?"
            " WHERE case_ref = ?",
            (closed_at, resolution_note, case_ref),
        )

    def list_remediations(
        self, article_ref: str | None = None, status: str | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM remediations"
        clauses, params = [], []
        if article_ref is not None:
            clauses.append("article_ref = ?")
            params.append(article_ref)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY opened_at"
        return self._all(sql, tuple(params))
