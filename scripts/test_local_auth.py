r"""Run with .venv\Scripts\python.exe scripts\test_local_auth.py."""
from __future__ import annotations

import gc
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import config
import db


PRIMARY = "10000000-0000-4000-8000-000000000001"
SECONDARY = "10000000-0000-4000-8000-000000000002"
OUTSIDER = "10000000-0000-4000-8000-000000000003"
AUTH_ENV = {
    "ZHIXU_AUTH_REQUIRED": "1",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
    "SUPABASE_ANON_KEY": "",
    "ZHIXU_AUTH_USER_IDS": f"{PRIMARY},{SECONDARY}",
    "ZHIXU_MAIN_USER_ID": PRIMARY,
}


class LocalAuth(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="zhixu_local_auth_")
        self.addCleanup(scratch.cleanup)
        self.addCleanup(gc.collect)
        self.data = Path(scratch.name)
        self.enterContext(patch.multiple(
            config, DATA_DIR=self.data, DB_PATH=self.data / "app.db",
            UPLOAD_DIR=self.data / "uploads", EXPORT_DIR=self.data / "exports",
            TMP_DIR=self.data / "tmp", CONFIG_PATH=self.data / "config.json",
        ))
        self.enterContext(patch.dict(os.environ, AUTH_ENV))
        self.provider_calls = []
        self.provider_status = None
        self.token_seconds = 3600
        real_client = httpx.AsyncClient
        self.enterContext(patch.object(
            httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(
                transport=httpx.MockTransport(self.provider), **kwargs,
            ),
        ))

    def provider(self, request):
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.host, "example.supabase.co")
        self.assertEqual(request.url.path, "/auth/v1/token")
        self.assertEqual(request.headers["apikey"], "sb_publishable_test")
        grant = request.url.params["grant_type"]
        self.provider_calls.append(grant)
        data = json.loads(request.content)
        if self.provider_status:
            return httpx.Response(self.provider_status)
        if grant == "password":
            if data["password"] != "synthetic-password":
                return httpx.Response(400, json={"error_code": "invalid_credentials"})
            user_id = {
                "primary@example.test": PRIMARY,
                "secondary@example.test": SECONDARY,
                "outsider@example.test": OUTSIDER,
            }[data["email"]]
        else:
            self.assertEqual(grant, "refresh_token")
            user_id = data["refresh_token"].removeprefix("local-test-token-")
        email = {
            PRIMARY: "primary@example.test", SECONDARY: "secondary@example.test",
            OUTSIDER: "outsider@example.test",
        }[user_id]
        return httpx.Response(200, json={
            "user": {"id": user_id, "email": email},
            "access_token": "local-test-access",
            "refresh_token": f"local-test-token-{user_id}",
            "expires_in": self.token_seconds,
        })

    def client(self):
        return TestClient(app.app, base_url="http://127.0.0.1")

    def login(self, client, account, password="synthetic-password"):
        return client.post("/api/auth/login", json={
            "email": f"{account}@example.test", "password": password,
        })

    def test_no_settings_preserves_demo_and_partial_settings_lock_data(self):
        for key in AUTH_ENV:
            os.environ.pop(key, None)
        with self.client() as client:
            self.assertEqual(client.get("/api/auth/session").json(), {
                "mode": "demo", "configured": False, "authenticated": False,
            })
            self.assertEqual(client.get("/api/zones").status_code, 200)
        os.environ["SUPABASE_URL"] = AUTH_ENV["SUPABASE_URL"]
        with self.client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/api/auth/session").json(), {
                "mode": "supabase", "configured": False, "authenticated": False,
            })
            self.assertEqual(client.get("/api/zones").status_code, 503)
            self.assertEqual(self.login(client, "primary").status_code, 503)
            self.assertEqual(client.get("/exports/file.pdf").status_code, 503)
        self.assertEqual(self.provider_calls, [])

    def test_cookie_sign_in_allowlist_refresh_and_sign_out(self):
        with self.client() as client:
            self.assertEqual(client.get("/api/zones").status_code, 401)
            self.assertEqual(client.get("/exports/file.pdf").status_code, 401)
            self.assertEqual(client.post("/api/auth/signup", json={}).status_code, 401)
            self.assertEqual(client.post("/api/auth/login", json={}, headers={
                "Origin": "http://127.0.0.1:9999",
            }).status_code, 403)
            self.assertEqual(self.login(client, "primary", "wrong").status_code, 401)
            self.assertEqual(self.login(client, "outsider").status_code, 403)
            self.token_seconds = 1
            result = self.login(client, "primary")
            self.assertEqual(result.json(), {
                "mode": "supabase", "configured": True, "authenticated": True,
                "user": {"id": PRIMARY, "email": "primary@example.test", "is_primary": True},
            })
            self.assertNotIn("local-test", result.text + result.headers["set-cookie"])
            self.assertIn("httponly", result.headers["set-cookie"].lower())
            self.assertIn("samesite=strict", result.headers["set-cookie"].lower())
            self.token_seconds = 3600
            cookie = client.cookies.get("zhixu_session")
            self.provider_status = 503
            self.assertEqual(client.get("/api/zones").status_code, 503)
            self.provider_status = None
            self.assertEqual(client.get("/api/auth/session").json(), result.json())
            self.assertEqual(self.provider_calls, ["password"] * 3 + ["refresh_token"] * 2)
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertFalse(client.get("/api/auth/session").json()["authenticated"])
            client.cookies.set("zhixu_session", cookie)
            self.assertEqual(client.get("/api/zones").status_code, 401)

    def test_legacy_backup_and_account_isolation(self):
        db.init_db()
        db.x("INSERT INTO zones VALUES (?, ?, ?)", ("legacy-zone", "旧知识区", db.now()))
        source = self.data / "uploads" / "legacy-paper.docx"
        source.write_bytes(b"paper-placeholder")
        export = self.data / "exports" / "legacy-export.pdf"
        export.write_bytes(b"legacy-export")
        db.x(
            "INSERT INTO paper_jobs(id, filename, original_path, sha256, status, corrected_pdf, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy-paper", "paper.docx", str(source), "hash", "uploaded", str(export), db.now()),
        )
        with self.client() as first, self.client() as second:
            self.assertEqual(self.login(first, "primary").status_code, 200)
            self.assertEqual(self.login(second, "secondary").status_code, 200)
            self.assertEqual(first.get("/api/zones").json()[0]["id"], "legacy-zone")
            self.assertEqual(second.get("/api/zones").json(), [])
            self.assertEqual(first.get("/exports/legacy-export.pdf").content, b"legacy-export")
            self.assertEqual(first.get("/exports/legacy-export.pdf").headers["cache-control"], "no-store")
            self.assertEqual(second.get("/exports/legacy-export.pdf").status_code, 409)
            self.assertEqual(second.put("/api/settings", json={"base_url": "http://example.test", "model": "test"})
                             .status_code, 403)

            note = first.post("/api/notes", json={
                "title": "Private", "body": "Private note", "style": "paper",
            }).json()["id"]
            self.assertEqual(second.get(f"/api/notes/{note}").status_code, 404)
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((30, 30), "spectralproof from primary")
                content = pdf.tobytes()
            document = first.post(
                "/api/documents", data={"zone_id": "legacy-zone"},
                files={"file": ("source.pdf", content)},
            ).json()["document_id"]
            conversation = first.post("/api/conversations", json={"zone_id": "legacy-zone"}).json()["id"]
            preview = f"/api/conversations/{conversation}/documents/{document}"
            self.assertEqual(first.get(f"{preview}/original").content, content)
            self.assertEqual(first.get(f"{preview}/pages/1/preview").headers["content-type"], "image/png")
            self.assertEqual(second.get(f"{preview}/original").status_code, 404)
            self.assertEqual(second.get(f"{preview}/pages/1/preview").status_code, 404)
            self.assertEqual(second.get(f"/api/conversations/{conversation}/messages").status_code, 404)

            zone = second.post("/api/zones", json={"name": "第二账户"}).json()["id"]
            second.post("/api/nodes", json={"zone_id": zone, "name": "第二账户知识节点"})
            own_conversation = second.post("/api/conversations", json={"zone_id": zone}).json()["id"]
            answer = {"status": "answered", "answer": "Proof from the source. [e1]", "citations": ["e1"]}
            with patch.object(app.llm, "chat_json", return_value=(answer, {})), ThreadPoolExecutor(2) as workers:
                result = workers.submit(first.post, "/api/chat?stream=true", json={
                    "conversation_id": conversation, "question": "spectralproof",
                })
                own_result = workers.submit(second.post, "/api/chat?stream=true", json={
                    "conversation_id": own_conversation, "question": "spectralproof",
                })
                events = [json.loads(line) for line in result.result().text.splitlines()]
                self.assertEqual([event["type"] for event in events], ["retrieved", "answer"])
                self.assertEqual(events[1]["result"]["citations"][0]["document_id"], document)
                own_events = [json.loads(line) for line in own_result.result().text.splitlines()]
                self.assertEqual(own_events[0]["message"]["retrieved_pages"], [])
                self.assertEqual(own_events[1]["result"]["citations"], [])
            self.assertEqual(first.get(f"/api/conversations/{conversation}/messages").json()[-1]["role"],
                             "assistant")
            self.assertEqual(second.get(f"/api/conversations/{own_conversation}/messages").json()[-1]["role"],
                             "assistant")
            self.assertEqual(
                second.post("/api/data/clear", json={"confirm": "CLEAR"}).status_code, 200,
            )
            self.assertEqual(first.get(f"/api/notes/{note}").status_code, 200)
            self.assertEqual(first.get(f"{preview}/original").status_code, 200)

        main_path = self.data / "accounts" / PRIMARY
        backup_path = self.data / "auth-backups" / "legacy-v1"
        with closing(sqlite3.connect(main_path / "app.db")) as conn:
            paper_path, export_path = conn.execute(
                "SELECT original_path, corrected_pdf FROM paper_jobs",
            ).fetchone()
        self.assertEqual(Path(paper_path), main_path / "uploads" / source.name)
        self.assertEqual(Path(export_path), main_path / "exports" / export.name)
        self.assertEqual((backup_path / "uploads" / source.name).read_bytes(), source.read_bytes())
        self.assertEqual((backup_path / "exports" / export.name).read_bytes(), export.read_bytes())
        with closing(sqlite3.connect(backup_path / "app.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0], 1)
        self.assertEqual(db.q1("SELECT COUNT(*) AS c FROM scholar_notes")["c"], 0)
        with closing(sqlite3.connect(self.data / "accounts" / SECONDARY / "app.db")) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0], 0)

        with self.client() as restarted:
            self.assertEqual(self.login(restarted, "primary").status_code, 200)
            self.assertEqual(restarted.get(f"/api/notes/{note}").status_code, 200)
        for key in AUTH_ENV:
            os.environ.pop(key, None)
        with self.client() as unconfigured:
            self.assertEqual(unconfigured.get("/api/zones").status_code, 503)
            self.assertEqual(unconfigured.get("/exports/legacy-export.pdf").status_code, 503)


if __name__ == "__main__":
    unittest.main()
