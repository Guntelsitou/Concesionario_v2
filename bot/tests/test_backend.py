"""Security, health and bootstrap regression tests for the FastAPI backend."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestBackendDeployment(unittest.TestCase):
    def test_health_and_webhook_auth_from_project_root(self):
        project = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="concesionario-backend-") as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            (web / "vehiculos.json").write_text("[]", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "WEB_DIR": str(web),
                "DATA_DIR": str(root / "data"),
                "WORKERS": "34600000000:Test",
                "WA_TOKEN": "token-test",
                "WA_PHONE_ID": "phone-test",
                "WA_VERIFY_TOKEN": "verify-test",
                "WA_APP_SECRET": "secret-test",
            })
            code = r'''
import hashlib, hmac, json
from fastapi.testclient import TestClient
from bot.whatsapp_bot import app
with TestClient(app) as c:
    health = c.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["estado"] == "ok", health.text
    assert "token-test" not in health.text and "secret-test" not in health.text
    verify = c.get("/webhook", params={"hub.mode":"subscribe", "hub.verify_token":"verify-test", "hub.challenge":"42"})
    assert verify.status_code == 200 and verify.text == "42", verify.text
    body = b'{"entry":[]}'
    bad = c.post("/webhook", content=body, headers={"X-Hub-Signature-256":"sha256=bad"})
    assert bad.status_code == 403, bad.text
    signature = "sha256=" + hmac.new(b"secret-test", body, hashlib.sha256).hexdigest()
    good = c.post("/webhook", content=body, headers={"X-Hub-Signature-256":signature})
    assert good.status_code == 200, good.text
'''
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=project, env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_webhook_fails_closed_when_app_secret_is_missing(self):
        project = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="concesionario-backend-") as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            (web / "vehiculos.json").write_text("[]", encoding="utf-8")
            env = os.environ.copy()
            env.update({"WEB_DIR": str(web), "DATA_DIR": str(root / "data"), "WA_APP_SECRET": ""})
            code = r'''
from fastapi.testclient import TestClient
from bot.whatsapp_bot import app
with TestClient(app) as c:
    response = c.post("/webhook", content=b'{"entry":[]}')
    assert response.status_code == 503, response.text
'''
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=project, env=env,
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_git_sync_funciona_con_web_dentro_de_un_repositorio_mayor(self):
        """Regresión: web/ no tiene .git propio cuando el proyecto es un solo repositorio; el push debe funcionar igual."""
        project = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="concesionario-git-") as tmp:
            root = Path(tmp)
            def git(*a, cwd=root):
                subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)
            git("init", "--bare", "-b", "main", "remoto.git")
            git("clone", "remoto.git", "repo")
            repo = root / "repo"
            git("config", "user.email", "t@t.es", cwd=repo)
            git("config", "user.name", "Test", cwd=repo)
            (repo / "web").mkdir()
            (repo / "web" / "vehiculos.json").write_text("[]", encoding="utf-8")
            (repo / "otro.txt").write_text("no tocar", encoding="utf-8")
            git("add", "-A", cwd=repo); git("commit", "-m", "base", cwd=repo); git("push", "-u", "origin", "HEAD:main", cwd=repo)
            (repo / "web" / "vehiculos.json").write_text('[{"ref":"0001"}]', encoding="utf-8")
            (repo / "otro.txt").write_text("cambio ajeno", encoding="utf-8")
            env = os.environ.copy()
            env.update({"WEB_DIR": str(repo / "web"), "DATA_DIR": str(root / "data"), "WA_APP_SECRET": "x"})
            code = "from bot.whatsapp_bot import _git_sync; assert _git_sync('alta 0001') is True"
            result = subprocess.run([sys.executable, "-c", code], cwd=project, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            subprocess.run(["git", "--git-dir", str(root / "remoto.git"), "show", "main:web/vehiculos.json"], check=True, capture_output=True, text=True)
            published = subprocess.run(["git", "--git-dir", str(root / "remoto.git"), "show", "main:web/vehiculos.json"], capture_output=True, text=True).stdout
            self.assertIn("0001", published)
            ajeno = subprocess.run(["git", "--git-dir", str(root / "remoto.git"), "show", "main:otro.txt"], capture_output=True, text=True).stdout
            self.assertEqual(ajeno, "no tocar")      # solo se publica web/


if __name__ == "__main__":
    unittest.main()
