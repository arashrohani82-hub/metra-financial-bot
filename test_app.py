import importlib
import io
import os
import sys

import openpyxl


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


def test_parse_odometer_accepts_common_formats(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)

    assert module.parse_odometer("84,520 km") == 84520
    assert module.parse_odometer("۸۴۵۲۰") == 84520


def test_fuel_receipt_requires_and_exports_odometer(tmp_path, monkeypatch):
    module = load_app(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        module,
        "telegram",
        lambda method, payload=None, **kwargs: sent.append((method, payload)),
    )
    receipt = {
        "merchant": "Shell",
        "date": "2026-09-01",
        "subtotal": 86.98,
        "gst": 4.35,
        "qst": 8.68,
        "total": 100.01,
        "currency": "CAD",
        "description": "Fuel",
        "expense_type": "company",
        "categories": ["Carburant et kilométrage"],
        "receipt_hash": "fuel-receipt-1",
        "receipt_path": "/data/fuel.jpg",
    }
    module.set_session(123, "choose_category", receipt)

    module.handle_callback(
        {
            "id": "callback-1",
            "from": {"id": 123},
            "message": {"chat": {"id": 456}},
            "data": "category:0",
        }
    )
    step, payload = module.get_session(123)
    assert step == "enter_odometer"
    assert "کیلومتراژ" in sent[-1][1]["text"]

    module.handle_message(
        {"from": {"id": 123}, "chat": {"id": 456}, "text": "84,520 km"}
    )
    step, payload = module.get_session(123)
    assert step == "enter_project"
    assert payload["odometer_km"] == 84520

    module.handle_callback(
        {
            "id": "callback-2",
            "from": {"id": 123},
            "message": {"chat": {"id": 456}},
            "data": "project:none",
        }
    )
    with module.db() as connection:
        saved = connection.execute(
            "SELECT category, odometer_km FROM expenses WHERE receipt_hash=?",
            ("fuel-receipt-1",),
        ).fetchone()
    assert saved["category"] == "Carburant et kilométrage"
    assert saved["odometer_km"] == 84520

    output, count = module.make_excel(123, 2026, 9)
    workbook = openpyxl.load_workbook(io.BytesIO(output.getvalue()))
    sheet = workbook["Expenses"]
    assert count == 1
    assert sheet["G1"].value == "Odometer (km)"
    assert sheet["G2"].value == 84520
