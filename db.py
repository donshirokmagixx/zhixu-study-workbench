"""SQLite 建库、连接与查询封装。表结构见规格第 5 节，字段名不得修改。"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_nodes (
  id TEXT PRIMARY KEY,
  zone_id TEXT NOT NULL,
  parent_id TEXT,
  name TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  zone_id TEXT NOT NULL,
  node_id TEXT,
  filename TEXT NOT NULL,
  ext TEXT NOT NULL,
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  warning TEXT,
  page_count INTEGER,
  chunk_count INTEGER,
  page_index_version INTEGER NOT NULL DEFAULT 0,
  quality REAL NOT NULL DEFAULT 1.0,
  quality_note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  zone_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  heading_path TEXT,
  page INTEGER,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunk_source_snapshots (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  heading_path TEXT,
  page INTEGER,
  text TEXT NOT NULL,
  text_sha256 TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS chunk_sources AS
  SELECT id, document_id, heading_path, page, text, text_sha256, 0 AS archived FROM chunks
  UNION ALL
  SELECT id, document_id, heading_path, page, text, text_sha256, 1 AS archived FROM chunk_source_snapshots;
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  zone_id TEXT NOT NULL,
  node_id TEXT,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  retrieved_pages TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citations (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  document_id TEXT NOT NULL,
  page INTEGER,
  heading_path TEXT,
  snippet TEXT,
  text_hash TEXT
);
CREATE TABLE IF NOT EXISTS mindmaps (
  id TEXT PRIMARY KEY,
  zone_id TEXT NOT NULL,
  title TEXT NOT NULL,
  mode TEXT NOT NULL,
  generated_by TEXT,
  theme TEXT NOT NULL DEFAULT 'fresh',
  layout_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mindmap_nodes (
  id TEXT PRIMARY KEY,
  mindmap_id TEXT NOT NULL,
  node_key TEXT NOT NULL,
  parent_key TEXT,
  title TEXT NOT NULL,
  note TEXT,
  source_document_id TEXT,
  source_chunk_ids TEXT,
  origin TEXT NOT NULL,
  unverified INTEGER NOT NULL DEFAULT 0,
  shape TEXT NOT NULL DEFAULT 'roundrect',
  pos_x REAL,
  pos_y REAL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scholar_notes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  style TEXT NOT NULL CHECK (style IN ('paper', 'journal')),
  zone_id TEXT REFERENCES zones(id) ON DELETE SET NULL,
  node_id TEXT REFERENCES knowledge_nodes(id) ON DELETE SET NULL,
  sources TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quizzes (
  id TEXT PRIMARY KEY,
  zone_id TEXT NOT NULL,
  mindmap_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_questions (
  id TEXT PRIMARY KEY,
  quiz_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  stem TEXT NOT NULL,
  options TEXT,
  answer TEXT NOT NULL,
  explanation TEXT NOT NULL,
  citations TEXT NOT NULL,
  latex TEXT
);
CREATE TABLE IF NOT EXISTS paper_jobs (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  original_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  profile_digest TEXT,
  status TEXT NOT NULL,
  issues TEXT,
  operations TEXT,
  checks TEXT,
  corrected_docx TEXT,
  corrected_pdf TEXT,
  report_pdf TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_logs (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL,
  model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER,
  ok INTEGER NOT NULL,
  error TEXT,
  skill_name TEXT,
  skill_sha256 TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_zone ON chunks(zone_id);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_citations_msg ON citations(message_id);
CREATE INDEX IF NOT EXISTS idx_documents_zone ON documents(zone_id);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_chunk_source_snapshots_document ON chunk_source_snapshots(document_id);
CREATE INDEX IF NOT EXISTS idx_mindmap_nodes_map ON mindmap_nodes(mindmap_id);
CREATE INDEX IF NOT EXISTS idx_nodes_zone ON knowledge_nodes(zone_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON knowledge_nodes(parent_id);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """兼容旧数据库：只加列和索引，不重建已有业务表。"""
    _add_column(conn, "documents", "node_id", "TEXT")
    _add_column(conn, "documents", "quality", "REAL NOT NULL DEFAULT 1.0")
    _add_column(conn, "documents", "quality_note", "TEXT")
    _add_column(conn, "documents", "page_index_version", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "conversations", "node_id", "TEXT")
    _add_column(conn, "messages", "retrieved_pages", "TEXT NOT NULL DEFAULT '[]'")
    _add_column(conn, "citations", "text_hash", "TEXT")
    _add_column(conn, "mindmaps", "generated_by", "TEXT")
    # v3.3 导图编辑器：主题与自由布局（文本/关系线），节点形状与手动位置
    _add_column(conn, "mindmaps", "theme", "TEXT NOT NULL DEFAULT 'fresh'")
    _add_column(conn, "mindmaps", "layout_json", "TEXT")
    _add_column(conn, "mindmap_nodes", "shape", "TEXT NOT NULL DEFAULT 'roundrect'")
    _add_column(conn, "mindmap_nodes", "pos_x", "REAL")
    _add_column(conn, "mindmap_nodes", "pos_y", "REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_node ON documents(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_zone ON conversations(zone_id)")
    # 旧引用若仍能对应到现有 chunk，则回填原文哈希
    conn.execute(
        "UPDATE citations SET text_hash = ("
        " SELECT chunks.text_sha256 FROM chunks WHERE chunks.id = citations.chunk_id"
        ") WHERE text_hash IS NULL AND EXISTS ("
        " SELECT 1 FROM chunks WHERE chunks.id = citations.chunk_id"
        ")"
    )


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        _init_schema(conn)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, text_bigram)"
    )


def _publish_directory(target: Path, populate) -> None:
    """Build beside the destination, then publish a complete directory."""
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=target.parent) as work:
        stage = Path(work) / "store"
        stage.mkdir()
        populate(stage)
        try:
            stage.rename(target)
        except OSError:
            if not target.is_dir():
                raise


def _backup_legacy_data(stage: Path, primary_id: str) -> None:
    legacy = config.DB_PATH.resolve()
    if legacy.is_file():
        with closing(sqlite3.connect(legacy.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(stage / legacy.name)) as destination:
                source.backup(destination)
    for directory in (config.UPLOAD_DIR, config.EXPORT_DIR):
        if directory.is_dir():
            shutil.copytree(directory, stage / directory.name)
    (stage / "owner").write_text(primary_id, encoding="utf-8")


def _rebase_paper_paths(conn: sqlite3.Connection, target: Path) -> None:
    columns = {"original_path": config.UPLOAD_DIR.name, "corrected_docx": config.EXPORT_DIR.name,
               "corrected_pdf": config.EXPORT_DIR.name, "report_pdf": config.EXPORT_DIR.name}
    papers = conn.execute("SELECT * FROM paper_jobs").fetchall()
    for paper in papers:
        paths = []
        for column, directory in columns.items():
            value = paper[column]
            name = Path(value).name if value else ""
            if name in ("", ".", "..") or ":" in name:
                name = ""
            paths.append(str(target / directory / name) if value else value)
        conn.execute(
            "UPDATE paper_jobs SET original_path = ?, corrected_docx = ?, corrected_pdf = ?,"
            " report_pdf = ? WHERE id = ?", (*paths, paper["id"]),
        )


def prepare_account_stores(primary_id: str, user_ids: set[str]) -> None:
    backup = config.legacy_backup_dir()
    _publish_directory(backup, lambda stage: _backup_legacy_data(stage, primary_id))
    if (backup / "owner").read_text(encoding="utf-8") != primary_id:
        raise RuntimeError("旧数据已归属另一主账号，请恢复原来的 ZHIXU_MAIN_USER_ID。")

    for user_id in sorted(user_ids):
        target = config.account_dir(user_id)

        def populate(stage: Path) -> None:
            if user_id == primary_id:
                for path in backup.iterdir():
                    if path.is_dir():
                        shutil.copytree(path, stage / path.name)
                    elif path.name == config.DB_PATH.name:
                        shutil.copy2(path, stage / path.name)
            with config.use_store(stage), closing(connect()) as conn, conn:
                _init_schema(conn)
                if user_id == primary_id:
                    _rebase_paper_paths(conn, target)

        _publish_directory(target, populate)


def fts5_available() -> bool:
    try:
        with connect() as conn:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.Error:
        return False


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute(sql, params).fetchall())


def q1(sql: str, params: tuple = ()):
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def x(sql: str, params: tuple = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def xmany(sql: str, seq: list[tuple]) -> None:
    with connect() as conn:
        conn.executemany(sql, seq)
        conn.commit()
