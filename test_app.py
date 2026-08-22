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
    commands = calls[1][1]["commands"]
    assert any(command["command"] == "card" for command in commands)


def test_parse_rbc_statement(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)
    path = "upload/MasterCard Statement-8250 2026-07-23.pdf"
    with open(path, "rb") as statement:
        parsed = module.parse_rbc_statement(statement.read())

    assert parsed["statement_date"] == "2026-07-23"
    assert parsed["purchases_debits"] == 4376.20
    assert parsed["interest"] == 57.44
    assert parsed["ending_balance"] == 5075.94
    assert parsed["credit_limit"] == 18000.00
    assert parsed["payment_due_date"] == "2026-08-17"


def test_card_dashboard_uses_clean_baseline(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)
    with module.db() as connection:
        for date, spend, interest, balance in (
            ("2026-03-23", 3666.56, 124.69, 2058.39),
            ("2026-04-23", 6044.39, 0, 2941.89),
            ("2026-05-25", 5491.51, 0, 5193.44),
        ):
            connection.execute(
                """
                INSERT INTO card_statements(
                    user_id, statement_date, period_end, purchases_debits,
                    interest, ending_balance, credit_limit, source_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (123, date, date, spend, interest, balance, 18000, date, date),
            )

    dashboard = module.card_dashboard(123)
    assert "تعداد دوره‌ها: 2" in dashboard
    assert "$11,535.90" in dashboard
    assert "مجموع بهره: <b>$0.00</b>" in dashboard
