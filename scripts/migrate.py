"""按文件名顺序应用 migrations/ 下的全部迁移。

每个迁移文件需在末尾自行 `INSERT OR IGNORE INTO schema_migrations`；
本脚本依据 schema_migrations 跳过已应用的版本，可重复执行。
"""

import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def migrate(database_path: str) -> list[str]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        done = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for sql_file in sorted((REPO_ROOT / "migrations").glob("*.sql")):
            if sql_file.stem in done:
                continue
            connection.executescript(sql_file.read_text(encoding="utf-8"))
            applied.append(sql_file.stem)
    return applied


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    applied = migrate(database_path)
    summary = "、".join(applied) if applied else "无（均已应用）"
    print(f"数据库迁移完成：{database_path}（本次应用：{summary}）")


if __name__ == "__main__":
    main()
