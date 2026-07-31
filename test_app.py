import importlib
import os
import sys


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    sys.modules.pop("app", None)
    module = importlib.import_module("app")
    return module


def test_health(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)
    response = module.app.test_client().get("/")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_webhook_rejects_wrong_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "correct-secret")
    module = load_app(tmp_path, monkeypatch)
    response = module.app.test_client().post(
        "/webhook/telegram",
        json={"message": {}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert response.status_code == 403
