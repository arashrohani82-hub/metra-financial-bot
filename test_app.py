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


def test_start_shows_persistent_menu(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(module, "telegram", lambda method, payload=None, **kwargs: sent.append((method, payload)))

    module.handle_message(
        {"from": {"id": 123}, "chat": {"id": 456}, "text": "/start"}
    )

    method, payload = sent[0]
    assert method == "sendMessage"
    assert payload["reply_markup"]["is_persistent"] is True
    assert payload["reply_markup"]["keyboard"][0][0]["text"] == "🧾 رسید جدید"


def test_setup_registers_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://example.test")
    monkeypatch.setenv("SETUP_SECRET", "setup-secret")
    module = load_app(tmp_path, monkeypatch)
    calls = []

    def fake_telegram(method, payload=None, **kwargs):
        calls.append((method, payload))
        return {"ok": True, "result": True}

    monkeypatch.setattr(module, "telegram", fake_telegram)
    response = module.app.test_client().get("/setup?key=setup-secret")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert [method for method, _ in calls] == ["setWebhook", "setMyCommands"]
