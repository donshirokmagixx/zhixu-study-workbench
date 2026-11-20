r"""Run with .venv\Scripts\python.exe scripts\test_chat_page_previews.py."""
from __future__ import annotations

import gc
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf
from fastapi.testclient import TestClient
from pptx import Presentation

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ChatPagePreviews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="zhixu_page_previews_")
        cls.addClassCleanup(cls.scratch.cleanup)
        os.environ["ZHIXU_DATA_DIR"] = cls.scratch.name
        import app
        cls.app = app
        cls.client = TestClient(app.app, base_url="http://127.0.0.1")
        cls.client.__enter__()
        cls.addClassCleanup(gc.collect)
        cls.addClassCleanup(cls.client.__exit__, None, None, None)

    def create_zone(self, name):
        return self.client.post("/api/zones", json={"name": name}).json()["id"]

    def upload(self, zone, filename, data, node=""):
        response = self.client.post("/api/documents", data={"zone_id": zone, "node_id": node},
                                    files={"file": (filename, data)})
        self.assertEqual(response.json().get("status"), "ready", response.text)
        return response.json()["document_id"]

    def test_legacy_migration_keeps_existing_sources_and_indexes_exact_pages(self):
        zone = self.create_zone("旧索引")
        with pymupdf.open() as pdf:
            for text in ("original opening words", "legacyconversion second page words"):
                pdf.new_page().insert_text((50, 50), text)
            document = self.upload(zone, "legacy.pdf", pdf.tobytes())
        self.app.db.x("UPDATE documents SET page_index_version = 0 WHERE id = ?", (document,))
        old = self.app.db.q1("SELECT id FROM chunks WHERE document_id = ? AND page = 1", (document,))["id"]
        self.app.db.x("UPDATE chunks SET text = ? WHERE id = ?", ("original combined legacy source", old))

        self.app.rag.refresh_page_indexes()

        result = self.app.rag.retrieve(zone, "legacyconversion")
        self.assertEqual(result[0]["page"], 2)
        self.assertEqual(result[0]["page_index_version"], self.app.rag.PAGE_INDEX_VERSION)
        node = {"source_chunk_ids": [old]}
        self.app._enrich_mindmap_sources({"nodes": [node]})
        self.assertEqual(node["source_snippet"], "original combined legacy source")
        self.assertIsNone(self.app.db.q1("SELECT id FROM chunks WHERE id = ?", (old,)))

    def test_retrieved_pages_survive_model_failure_and_keep_scope(self):
        zone = self.create_zone("预览课件")
        other_zone = self.create_zone("隔离课件")
        node = self.client.post("/api/nodes", json={"zone_id": zone, "name": "所选章节"}).json()["id"]
        with pymupdf.open() as pdf:
            for color, text in [((1, 0, 0), "chapter red introduction"), ((0, 0, 1), "spectralproof chapter blue")]:
                page = pdf.new_page()
                page.draw_rect(page.rect, fill=color)
                page.insert_text((50, 50), text)
            pdf_data = pdf.tobytes()
        pdf_id = self.upload(zone, "proof.pdf", pdf_data, node)
        sibling_id = self.upload(zone, "outside-node.pdf", pdf_data + b"\n", "")
        self.upload(other_zone, "outside-zone.pdf", pdf_data)

        deck = Presentation()
        for text in ("slides introduction", "spectralproof slide explanation"):
            slide = deck.slides.add_slide(deck.slide_layouts[5])
            slide.shapes.title.text = "Same heading"
            slide.shapes.add_textbox(0, 0, 1000000, 1000000).text_frame.text = text
        deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "titleonlypreview"
        slide_data = io.BytesIO()
        deck.save(slide_data)
        ppt_id = self.upload(zone, "proof.pptx", slide_data.getvalue(), node)
        self.assertEqual(self.app.rag.retrieve(zone, "titleonlypreview")[0]["page"], 3)

        conversation = self.client.post("/api/conversations", json={"zone_id": zone, "node_id": node}).json()["id"]
        failure = self.app.llm.AppError("model_request_failed", "test model offline")
        with patch.object(self.app.llm, "chat_json", side_effect=failure):
            response = self.client.post("/api/chat?stream=true", json={"conversation_id": conversation,
                                                                     "question": "spectralproof"})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([event["type"] for event in events], ["retrieved", "error"])
        pages = events[0]["message"]["retrieved_pages"]
        self.assertEqual({(item["document_id"], item["page"]) for item in pages}, {(pdf_id, 2), (ppt_id, 2)})
        history = self.client.get(f"/api/conversations/{conversation}/messages").json()
        self.assertEqual(history[0]["retrieved_pages"], pages)

        base = f"/api/conversations/{conversation}/documents"
        preview = self.client.get(f"{base}/{pdf_id}/pages/2/preview")
        self.assertEqual(preview.headers.get("content-type"), "image/png")
        self.assertEqual(pymupdf.Pixmap(preview.content).pixel(10, 10), (0, 0, 255))
        self.assertEqual(self.client.get(f"{base}/{pdf_id}/pages/9/preview").status_code, 404)
        self.assertEqual(self.client.get(f"{base}/{sibling_id}/pages/2/preview").status_code, 404)
        with patch.object(self.app.officedoc, "pptx_slide_preview", return_value=preview.content) as render:
            slide = self.client.get(f"{base}/{ppt_id}/pages/2/preview")
            self.assertEqual(slide.headers.get("content-type"), "image/png")
            self.assertEqual(render.call_args.args[1], 2)
        self.assertEqual(self.client.get(f"{base}/{ppt_id}/original").content, slide_data.getvalue())

        answer = {"status": "answered", "answer": "Proof from the course.[e1]", "citations": ["e1"]}
        with patch.object(self.app.llm, "chat_json", return_value=(answer, {})):
            response = self.client.post("/api/chat?stream=true", json={"conversation_id": conversation,
                                                                     "question": "spectralproof"})
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual([event["type"] for event in events], ["retrieved", "answer"])
        citation = events[1]["result"]["citations"][0]
        self.assertIn(citation["document_id"], (pdf_id, ppt_id))
        self.assertEqual(citation["page"], 2)


if __name__ == "__main__":
    unittest.main()
