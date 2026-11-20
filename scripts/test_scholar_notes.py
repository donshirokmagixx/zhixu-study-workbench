r"""Run with .venv\Scripts\python.exe scripts\test_scholar_notes.py."""
from __future__ import annotations

import gc
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import config
import llm


class ScholarNotes(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="zhixu_notes_")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.enterContext(patch.multiple(
            config, DATA_DIR=root, UPLOAD_DIR=root / "uploads", EXPORT_DIR=root / "exports",
            TMP_DIR=root / "tmp", CONFIG_PATH=root / "config.json", DB_PATH=root / "app.db",
        ))
        self.addCleanup(gc.collect)
        self.client = self.enterContext(TestClient(app.app))
        self.model = self.enterContext(patch.object(llm, "_post", side_effect=self.model_response))
        self.model_output = {"body": "## 核心概念\n这是根据讲义整理的笔记。", "source_ids": ["e1"]}
        self.prompt_data = None

    def model_response(self, messages, **kwargs):
        self.prompt_data = json.loads(messages[1]["content"])
        return {
            "model": "scholar-notes-test",
            "choices": [{"message": {"content": json.dumps(self.model_output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

    def zone(self, name):
        response = self.client.post("/api/zones", json={"name": name})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def node(self, zone_id, name, parent_id=None):
        response = self.client.post("/api/nodes", json={
            "zone_id": zone_id, "name": name, "parent_id": parent_id,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def upload(self, zone_id, node_id, name, text):
        response = self.client.post(
            "/api/documents", data={"zone_id": zone_id, "node_id": node_id or ""},
            files={"file": (name, text.encode("utf-8"), "text/plain")},
        )
        self.assertEqual(response.json().get("status"), "ready", response.text)
        return response.json()["document_id"]

    def test_manual_note_roundtrip_style_change_and_clear(self):
        payload = {"title": "手写笔记", "body": "## 小节\n保留二级标题。", "style": "paper"}
        response = self.client.post("/api/notes", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        note = response.json()
        path = f"/api/notes/{note['id']}"
        self.assertEqual(self.client.get(path).json()["body"], payload["body"])
        self.assertEqual(note["sources"], [])
        self.assertIsNone(note["zone_id"])
        summary = self.client.get("/api/notes").json()
        self.assertEqual(set(summary[0]), {
            "id", "title", "style", "zone_id", "node_id", "created_at", "updated_at",
        })
        modified = {"title": "复习札记", "body": "更新后的内容", "style": "journal"}
        response = self.client.put(path, json=modified)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["style"], "journal")
        self.assertEqual(self.client.get(path).json()["body"], modified["body"])
        self.assertEqual(self.client.delete(path).status_code, 204)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.put(path, json=modified).status_code, 404)
        self.assertEqual(self.client.post("/api/notes", json=payload).status_code, 200)
        self.assertEqual(self.client.post("/api/data/clear", json={"confirm": "CLEAR"}).status_code, 200)
        self.assertEqual(self.client.get("/api/notes").json(), [])
        self.model.assert_not_called()

    def test_note_rejects_invalid_style_title_scope_and_source_page(self):
        zone = self.zone("数学")
        other = self.zone("物理")
        node = self.node(other, "课件")
        document = self.upload(zone, None, "notes.txt", "Fibonacci recurrence example")
        basic = {"title": "笔记", "body": "", "style": "journal", "zone_id": zone}
        for updates in (
            {"style": "other"}, {"title": "  "}, {"title": "长" * (config.NOTE_TITLE_MAX_CHARS + 1)},
            {"body": "长" * (config.NOTE_BODY_MAX_CHARS + 1)}, {"node_id": node}, {"zone_id": None, "node_id": node},
            {"sources": [{"document_id": document, "page": 99}]},
            {"sources": [{"document_id": document, "page": True}]},
            {"zone_id": other, "sources": [{"document_id": document, "page": None}]},
        ):
            with self.subTest(updates=list(updates)):
                response = self.client.post("/api/notes", json={**basic, **updates})
                self.assertEqual(response.status_code, 400, response.text[:300])
        self.assertEqual(self.client.get("/api/notes").json(), [])

    def test_draft_uses_scope_and_returns_only_real_citations(self):
        zone = self.zone("数学")
        other = self.zone("物理")
        node = self.node(zone, "复习范围")
        child = self.node(zone, "第二节", node)
        document = self.upload(zone, child, "in-scope.txt", "Fibonacci recurrence INSIDE_SCOPE")
        self.upload(zone, None, "outside-node.txt", "Fibonacci recurrence OUTSIDE_NODE")
        self.upload(other, None, "outside-zone.txt", "Fibonacci recurrence OUTSIDE_ZONE")
        self.model_output["source_ids"] = ["e1", "fake-citation", "e1"]
        scope = {"zone_id": zone, "node_id": node}
        response = self.client.post("/api/notes/draft", json={"title": "Fibonacci recurrence", **scope})
        self.assertEqual(response.status_code, 200, response.text)
        draft = response.json()
        expected_source = {"document_id": document, "filename": "in-scope.txt", "page": None, "ext": ".txt"}
        self.assertEqual(draft["sources"], [expected_source])
        prompt = json.dumps(self.prompt_data)
        self.assertIn("INSIDE_SCOPE", prompt)
        self.assertNotIn("OUTSIDE_NODE", prompt)
        self.assertNotIn("OUTSIDE_ZONE", prompt)
        self.assertEqual(self.client.get("/api/notes").json(), [])
        response = self.client.post("/api/notes", json={
            "title": "稿件", "body": draft["body"], "style": "paper", **scope,
            "sources": [{**expected_source, "filename": "forged.txt", "ext": ".pdf"}],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sources"], [expected_source])
        path = f"/api/notes/{response.json()['id']}"
        self.assertEqual(self.client.delete(f"/api/documents/{document}").status_code, 204)
        self.assertEqual(self.client.get(path).json()["sources"], [])

    def test_scoped_draft_with_no_topic_uses_available_passages(self):
        zone = self.zone("数学")
        node = self.node(zone, "课件")
        document = self.upload(zone, node, "recurrence.txt", "Fibonacci recurrence INSIDE_SCOPE")
        self.upload(zone, None, "out.txt", "Fibonacci recurrence OUTSIDE_SCOPE")
        response = self.client.post("/api/notes/draft", json={"zone_id": zone, "node_id": node})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sources"][0]["document_id"], document)
        prompt = json.dumps(self.prompt_data)
        self.assertIn("INSIDE_SCOPE", prompt)
        self.assertNotIn("OUTSIDE_SCOPE", prompt)

    def test_scoped_draft_without_a_match_or_pasted_text_returns_422(self):
        zone = self.zone("数学")
        self.upload(zone, None, "recurrence.txt", "Fibonacci recurrence")
        response = self.client.post("/api/notes/draft", json={"zone_id": zone, "title": "photosynthesis"})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "insufficient_evidence")
        self.model.assert_not_called()

    def test_unscoped_topic_and_scoped_pasted_text_can_be_drafted(self):
        zone = self.zone("数学")
        self.model_output["source_ids"] = []
        for payload in ({"title": "Fibonacci"}, {"zone_id": zone, "body": "出自我的课堂记录。"}):
            response = self.client.post("/api/notes/draft", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"body": self.model_output["body"], "sources": []})

    def test_invalid_model_body_or_unverifiable_scoped_draft_is_rejected(self):
        self.model_output["body"] = "<b>静态笔记</b>"
        response = self.client.post("/api/notes/draft", json={"title": "笔记"})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(response.json()["error"]["code"], "model_output_invalid")
        self.model_output = {"body": "没有引文", "source_ids": ["never-in-prompt"]}
        zone = self.zone("数学")
        self.upload(zone, None, "recurrence.txt", "Fibonacci recurrence")
        response = self.client.post("/api/notes/draft", json={"zone_id": zone, "title": "Fibonacci"})
        self.assertEqual(response.status_code, 502, response.text)

    def test_technical_generics_are_plain_note_text(self):
        self.model_output = {"body": "## 泛型\nList<T> 是一个泛型类型，其中 <T> 代表类型参数。", "source_ids": []}
        response = self.client.post("/api/notes/draft", json={"title": "泛型"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("List<T>", response.json()["body"])

    def test_scope_deletion_preserves_manual_note(self):
        zone = self.zone("数学")
        node = self.node(zone, "课件")
        response = self.client.post("/api/notes", json={
            "title": "笔记", "body": "保留我", "style": "journal", "zone_id": zone, "node_id": node,
        })
        self.assertEqual(response.status_code, 200, response.text)
        path = f"/api/notes/{response.json()['id']}"
        self.assertEqual(self.client.delete(f"/api/nodes/{node}").status_code, 204)
        self.assertIsNone(self.client.get(path).json()["node_id"])
        self.assertEqual(self.client.delete(f"/api/zones/{zone}").status_code, 204)
        note = self.client.get(path).json()
        self.assertIsNone(note["zone_id"])
        self.assertEqual(note["body"], "保留我")


if __name__ == "__main__":
    unittest.main(verbosity=2)
