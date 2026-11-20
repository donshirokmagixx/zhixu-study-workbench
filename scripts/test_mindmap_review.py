"""M2 全文顺序、分批证据与覆盖提示回归。使用合成资料和假模型。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import config
import db
import llm


class MindmapReviewTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="zhixu_mindmap_review_")
        self.addCleanup(directory.cleanup)
        data = Path(directory.name)
        paths = patch.multiple(config, DATA_DIR=data, DB_PATH=data / "app.db",
                               UPLOAD_DIR=data / "uploads", EXPORT_DIR=data / "exports",
                               TMP_DIR=data / "tmp", CONFIG_PATH=data / "config.json")
        self.addCleanup(paths.stop)
        paths.start()
        original_connect = db.connect

        @contextmanager
        def close_after_use():
            with closing(original_connect()) as connection, connection:
                yield connection

        connection = patch.object(db, "connect", close_after_use)
        self.addCleanup(connection.stop)
        connection.start()
        db.init_db()
        self.zone = db.new_id()
        db.x("INSERT INTO zones (id, name, created_at) VALUES (?,?,?)",
             (self.zone, "高数复习 " + self.zone, db.now()))

    def make_document(self, count, *, length=1, warning=None):
        document = db.new_id()
        db.x(
            "INSERT INTO documents (id, zone_id, filename, ext, size, sha256, status,"
            " page_count, warning, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (document, self.zone, "幂级数讲义.pdf", ".pdf", 1, document, "ready",
             count + (12 if warning else 0), warning, db.now()),
        )
        texts = ["收敛半径的定义"] + ["比值法与适用条件" * length] * (count - 2) + ["收敛圆周反例与例题"]
        rows = [(db.new_id(), document, self.zone, i, None, i + 1, text, str(i))
                for i, text in enumerate(texts)]
        db.xmany("INSERT INTO chunks (id, document_id, zone_id, ordinal, heading_path, page,"
                 " text, text_sha256) VALUES (?,?,?,?,?,?,?,?)", rows)
        return document, [row[0] for row in rows]

    def generate(self, *documents):
        inputs = []

        def model(_skill, _message, **kwargs):
            pack = json.loads(kwargs["system_extra"])
            inputs.append(pack)
            entries = pack["evidence"]
            if pack.get("phase") == "summarize":
                refs = [ref for entry in entries for ref in
                        (entry.get("refs") or [entry["evidence_id"]])]
                return {"sections": [{"title": "本段知识", "text": "定义、条件、公式与例题",
                                     "refs": [refs[0], refs[-1]]}]}, {}
            def ref(entry, end=False):
                return entry["refs"][-1 if end else 0] if "refs" in entry else entry["evidence_id"]
            return {"status": "ready", "nodes": [
                {"id": "root", "parent_id": None, "title": "幂级数"},
                {"id": "first", "parent_id": "root", "title": "收敛半径的定义", "refs": [ref(entries[0])]},
                {"id": "last", "parent_id": "root", "title": "收敛圆周反例与例题",
                 "refs": [ref(entries[-1], end=True)]},
            ]}, {}

        with patch.object(llm, "chat_json", side_effect=model):
            result = app.create_mindmap({"zone_id": self.zone, "document_ids": list(documents),
                                         "title": "幂级数", "mode": "concept"})
        return result, inputs

    def test_short_document_reads_past_first_twelve_chunks(self):
        document, chunks = self.make_document(16)
        result, inputs = self.generate(document)
        self.assertEqual(len(inputs), 1)
        self.assertEqual([entry["chunk_id"] for entry in inputs[0]["evidence"]], chunks)
        self.assertEqual(result["nodes"][-1]["source_chunk_ids"], chunks[-1:])
        self.assertEqual(result["nodes"][-1]["source_page"], 16)
        self.assertEqual(result["nodes"][-1]["source_snippet"], "收敛圆周反例与例题")

    def test_long_document_reads_all_batches_and_keeps_original_refs(self):
        document, chunks = self.make_document(63, length=48)
        result, inputs = self.generate(document)
        raw = [entry["chunk_id"] for pack in inputs[:-1] for entry in pack["evidence"]
               if "chunk_id" in entry]
        self.assertEqual(raw, chunks)
        self.assertEqual([node["title"] for node in result["nodes"]],
                         ["幂级数", "收敛半径的定义", "收敛圆周反例与例题"])
        self.assertEqual(result["nodes"][1]["source_chunk_ids"], chunks[:1])
        self.assertEqual(result["nodes"][2]["source_chunk_ids"], chunks[-1:])

    def test_ocr_page_limit_is_reported_as_partial_coverage(self):
        document, _ = self.make_document(30, warning="扫描件超过 30 页，仅索引前 30 页")
        result, _ = self.generate(document)
        self.assertEqual(result["status"], "partial")
        self.assertIn("仅索引前 30 页", result["warning"])

    def test_selected_documents_stay_in_selected_order(self):
        first, first_chunks = self.make_document(4)
        second, second_chunks = self.make_document(4)
        _, inputs = self.generate(second, first)
        self.assertEqual([entry["chunk_id"] for entry in inputs[0]["evidence"]],
                         second_chunks + first_chunks)


if __name__ == "__main__":
    unittest.main()
