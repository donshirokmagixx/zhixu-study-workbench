"""v3.2 功能 A 验收：解析质量评分（不调模型、不碰正式 data/）。"""
from __future__ import annotations

import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "data" / "tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
TMP = Path(tempfile.mkdtemp(prefix="zhixu_quality_", dir=TMP_ROOT))
os.environ["ZHIXU_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import quality  # noqa: E402
import rag  # noqa: E402

PASS = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail="") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  |  {str(detail)[:300]}")


class FakeUpload:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


def run_upload(filename: str, content: bytes, zone_id: str):
    """不建事件循环，直接驱动无嵌套线程的 upload_document 协程。"""
    coro = app.upload_document(file=FakeUpload(filename, content), zone_id=zone_id, node_id="")
    try:
        yielded = coro.send(None)
        while True:
            if inspect.iscoroutine(yielded):
                try:
                    yielded.send(None)
                except StopIteration as stop:
                    yielded = stop.value
                else:
                    raise RuntimeError("测试上传协程包含未预期 await")
                yielded = coro.send(yielded)
            else:
                break
    except StopIteration as stop:
        return stop.value
    raise RuntimeError("upload_document 未返回结果")


try:
    db.init_db()

    normal = "信号与系统的线性性质包括叠加性和齐次性。线性时不变系统可以用卷积描述。" * 5
    bad = "信号性质 " + "\ufffd" * 800 + "!!!!!!!!!!!!!!!!" * 30
    s_normal, note_normal = quality.score_text(normal)
    s_bad, note_bad = quality.score_text(bad)
    check("A1 正常中文长文本 >=0.85", s_normal >= 0.85, (s_normal, note_normal))
    check("A2 乱码文本 <0.6 且有原因", s_bad < 0.6 and bool(note_bad), (s_bad, note_bad))
    check("A3 空文本", quality.score_text("") == (0.0, "空文本"), quality.score_text(""))
    empty = "一段正常主题说明。" * 10
    texts = [normal, empty, "短句"]
    scores = [quality.score_text(text)[0] for text in texts]
    expected = round(sum(score * len(text) for score, text in zip(scores, texts))
                     / sum(len(text) for text in texts), 3)
    weighted, weighted_note = quality.score_chunks([{"text": text} for text in texts])
    check("A3b score_chunks 字数加权", weighted == expected, (weighted, expected, weighted_note))
    check("A3c is_low 阈值", quality.is_low(0.59) and not quality.is_low(0.6), (s_bad, s_normal))

    zone = app.create_zone({"name": "质量测试"})
    normal_result = run_upload("正常讲义.md", normal.encode("utf-8"), zone["id"])
    check("A4 上传正常文档返回质量", normal_result.get("quality", 0) >= 0.85
          and "quality_note" in normal_result, normal_result)
    low_result = run_upload("乱码讲义.txt", bad.encode("utf-8"), zone["id"])
    check("A5 上传乱码文档为低质量", low_result.get("quality", 1) < 0.6
          and low_result.get("quality_note"), low_result)

    documents = app.list_documents(zone_id=zone["id"])
    by_name = {item["filename"]: item for item in documents}
    check("A4b 文档列表含质量字段", all("quality" in item and "quality_note" in item for item in documents), documents)
    check("A5b 列表低质量值", by_name["乱码讲义.txt"]["quality"] < 0.6, by_name["乱码讲义.txt"])

    results = rag.retrieve(zone["id"], "信号性质")
    normal_id = normal_result["document_id"]
    low_id = low_result["document_id"]
    check("A6 检索返回质量字段", any(item["document_id"] == normal_id and "quality" in item for item in results), results)
    normal_item = next((item for item in results if item["document_id"] == normal_id), None)
    low_item = next((item for item in results if item["document_id"] == low_id), None)
    check("A6b 正常资料排序优先", normal_item and low_item and results.index(normal_item) < results.index(low_item), results)

    low_path = config.UPLOAD_DIR / f"{low_id}.txt"
    low_path.write_text("重新整理后的干净文本，说明信号性质与系统分类。" * 20, encoding="utf-8")
    reindexed = app.reindex_document(low_id)
    refreshed = {item["id"]: item for item in app.list_documents(zone_id=zone["id"])}
    check("A7 重建索引重算质量", reindexed.get("status") == "ready"
          and refreshed[low_id]["quality"] >= 0.85, (reindexed, refreshed[low_id]))
    check("A7b 质量已写回数据库", refreshed[low_id]["quality"] >= 0.85, refreshed[low_id])

    # A8：旧 documents 表没有 quality/quality_note，init_db 只加列并保留旧行
    legacy = TMP / "legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    legacy_db = legacy / "app.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        "CREATE TABLE documents (id TEXT PRIMARY KEY, zone_id TEXT NOT NULL, node_id TEXT, filename TEXT NOT NULL,"
        " ext TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT,"
        " warning TEXT, page_count INTEGER, chunk_count INTEGER, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO documents VALUES ('old-doc','old-zone',NULL,'旧资料.md','.md',10,'sha','ready',NULL,NULL,1,1,'2026-01-01T00:00:00+08:00')"
    )
    conn.commit()
    conn.close()
    saved_path = config.DB_PATH
    config.DB_PATH = legacy_db
    try:
        db.init_db()
        columns = {row["name"] for row in db.q("PRAGMA table_info(documents)")}
        old_row = db.q1("SELECT quality, quality_note FROM documents WHERE id='old-doc'")
        check("A8 旧库自动加列且默认 1.0", "quality" in columns and "quality_note" in columns
              and old_row["quality"] == 1.0 and old_row["quality_note"] is None, (columns, dict(old_row) if old_row else None))
    finally:
        config.DB_PATH = saved_path

    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败:", FAILS)
        sys.exit(1)
finally:
    shutil.rmtree(TMP, ignore_errors=True)