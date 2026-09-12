import hashlib
import io
import json
import os
import re
from datetime import datetime

from pypdf import PdfReader

import named_run as named_accounting
from receipt_ai import _choose_model


core = named_accounting.core
application = named_accounting.application
_base_main_menu = named_accounting.main_menu
COMPANY_STATEMENT_DIR = core.DATA_DIR / "company_receipt_statements"
COMPANY_STATEMENT_DIR.mkdir(parents=True, exist_ok=True)

CLASSIFICATIONS = {
    "client": ("client_payment", "✅ درآمد مشتری"),
    "transfer": ("internal_transfer", "🔁 انتقال داخلی"),
    "refund": ("refund", "↩️ برگشت وجه"),
    "credit": ("loan_or_credit", "💳 وام / اعتبار"),
    "other": ("other_non_revenue", "➖ سایر غیر درآمد"),
}


with core.db() as connection:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS company_receipt_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            statement_date TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            account_last4 TEXT,
            statement_total_deposits REAL NOT NULL DEFAULT 0,
            extracted_total_deposits REAL NOT NULL DEFAULT 0,
            confirmed_revenue REAL NOT NULL DEFAULT 0,
            excluded_deposits REAL NOT NULL DEFAULT 0,
            pending_deposits REAL NOT NULL DEFAULT 0,
            reconciliation_difference REAL NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL,
            source_path TEXT,
            status TEXT NOT NULL DEFAULT 'review',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, source_hash)
        );

        CREATE TABLE IF NOT EXISTS company_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL,
            txn_date TEXT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            classification TEXT NOT NULL DEFAULT 'pending',
            suggested_classification TEXT,
            confidence REAL NOT NULL DEFAULT 0,
            reason TEXT,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(statement_id) REFERENCES company_receipt_statements(id)
        );

        CREATE INDEX IF NOT EXISTS idx_company_deposits_review
        ON company_deposits(statement_id, classification, id);
        """
    )


def _menu():
    menu = _base_main_menu()
    rows = [list(row) for row in menu["keyboard"]]
    insert_at = max(0, len(rows) - 1)
    rows.insert(
        insert_at,
        [{"text": "💰 دریافتی شرکت"}, {"text": "📥 گزارش دریافتی"}],
    )
    result = dict(menu)
    result["keyboard"] = rows
    result["input_field_placeholder"] = "رسید یا PDF گزارش بانکی را ارسال کنید…"
    return result


main_menu = _menu


def _pdf_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        raise ValueError("PDF has no readable text")
    return text


def _json_object(raw):
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("No JSON object returned for bank statement")
    return json.loads(match.group(0))


def _iso_date(value, fallback=""):
    value = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    return fallback


def extract_company_deposits(pdf_bytes):
    text = _pdf_text(pdf_bytes)
    prompt = """
Extract EVERY incoming deposit/credit transaction from this COMPANY bank statement.
Return JSON only, with this exact shape:
{
  "statement_date":"YYYY-MM-DD",
  "period_start":"YYYY-MM-DD",
  "period_end":"YYYY-MM-DD",
  "account_last4":"",
  "statement_total_deposits":0.00,
  "deposits":[
    {"date":"YYYY-MM-DD","description":"exact bank description","amount":0.00,
     "suggested_classification":"client_payment|internal_transfer|refund|loan_or_credit|other_non_revenue|unknown",
     "confidence":0.0,"reason":"short reason"}
  ]
}

STRICT RULES:
1. Use exact printed amounts; never estimate, round, combine, or invent a transaction.
2. Include every positive deposit/credit contributing to the statement's Total deposits.
3. Do not include withdrawals, purchases, fees, or opening/closing balances.
4. client_payment only when the printed description clearly supports customer/project revenue.
5. Own-account transfers, reversals/refunds, loans/credit advances, interest, tax credits and unexplained deposits are NOT client revenue.
6. If uncertain, use unknown. Human review decides the final classification.
7. statement_total_deposits must be the exact summary value printed by the bank, not your calculated sum.
8. Preserve the transaction description exactly as printed, as far as text extraction permits.

BANK STATEMENT TEXT:
""" + text[:140000]
    model = _choose_model(core.ANTHROPIC_API_KEY)
    response = core.Anthropic(api_key=core.ANTHROPIC_API_KEY).messages.create(
        model=model,
        max_tokens=5000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _json_object(raw)
    deposits = []
    allowed = {
        "client_payment", "internal_transfer", "refund", "loan_or_credit",
        "other_non_revenue", "unknown",
    }
    for item in data.get("deposits") or []:
        amount = round(abs(float(item.get("amount") or 0)), 2)
        description = str(item.get("description") or "").strip()
        if amount <= 0 or not description:
            continue
        suggestion = str(item.get("suggested_classification") or "unknown")
        if suggestion not in allowed:
            suggestion = "unknown"
        deposits.append({
            "date": _iso_date(item.get("date")),
            "description": description[:500],
            "amount": amount,
            "suggested_classification": suggestion,
            "confidence": min(1.0, max(0.0, float(item.get("confidence") or 0))),
            "reason": str(item.get("reason") or "")[:300],
        })
    statement_date = _iso_date(data.get("statement_date"))
    period_end = _iso_date(data.get("period_end"))
    if not statement_date:
        statement_date = period_end or datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "statement_date": statement_date,
        "period_start": _iso_date(data.get("period_start")),
        "period_end": period_end,
        "account_last4": re.sub(r"\D", "", str(data.get("account_last4") or ""))[-4:],
        "statement_total_deposits": round(abs(float(data.get("statement_total_deposits") or 0)), 2),
        "deposits": deposits,
    }


def _money(value):
    return f"${float(value or 0):,.2f} CAD"


def _statement_totals(connection, statement_id):
    return connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN classification='client_payment' THEN amount ELSE 0 END), 0) AS revenue,
          COALESCE(SUM(CASE WHEN classification NOT IN ('client_payment','pending') THEN amount ELSE 0 END), 0) AS excluded,
          COALESCE(SUM(CASE WHEN classification='pending' THEN amount ELSE 0 END), 0) AS pending,
          SUM(CASE WHEN classification='pending' THEN 1 ELSE 0 END) AS pending_count
        FROM company_deposits WHERE statement_id=?
        """,
        (statement_id,),
    ).fetchone()


def _review_next(chat_id, user_id, statement_id):
    with core.db() as connection:
        statement = connection.execute(
            "SELECT * FROM company_receipt_statements WHERE id=? AND user_id=?",
            (statement_id, user_id),
        ).fetchone()
        deposit = connection.execute(
            "SELECT * FROM company_deposits WHERE statement_id=? AND classification='pending' ORDER BY id LIMIT 1",
            (statement_id,),
        ).fetchone()
        reviewed = connection.execute(
            "SELECT COUNT(*) AS n FROM company_deposits WHERE statement_id=? AND classification!='pending'",
            (statement_id,),
        ).fetchone()["n"]
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM company_deposits WHERE statement_id=?",
            (statement_id,),
        ).fetchone()["n"]
    if not statement:
        core.send_message(chat_id, "گزارش بانکی پیدا نشد.", reply_markup=main_menu())
        return
    if not deposit:
        _finalize_statement(chat_id, user_id, statement_id)
        return
    suggestion_labels = {
        "client_payment": "درآمد مشتری", "internal_transfer": "انتقال داخلی",
        "refund": "برگشت وجه", "loan_or_credit": "وام / اعتبار",
        "other_non_revenue": "سایر غیر درآمد", "unknown": "نامشخص",
    }
    suggestion = suggestion_labels.get(deposit["suggested_classification"], "نامشخص")
    core.send_message(
        chat_id,
        f"🔎 <b>بررسی واریزی {reviewed + 1} از {total}</b>\n\n"
        f"📅 تاریخ: <b>{core.safe(deposit['txn_date'] or 'ثبت نشده')}</b>\n"
        f"📝 شرح بانک: <b>{core.safe(deposit['description'])}</b>\n"
        f"💵 مبلغ: <b>{_money(deposit['amount'])}</b>\n\n"
        f"پیشنهاد سیستم: {suggestion} — اطمینان {float(deposit['confidence'] or 0) * 100:.0f}%\n"
        "برای ثبت قطعی، نوع این واریزی را انتخاب کن:",
        [
            [{"text": "✅ درآمد مشتری", "callback_data": f"recv:{deposit['id']}:client"}],
            [
                {"text": "🔁 انتقال داخلی", "callback_data": f"recv:{deposit['id']}:transfer"},
                {"text": "↩️ برگشت وجه", "callback_data": f"recv:{deposit['id']}:refund"},
            ],
            [
                {"text": "💳 وام/اعتبار", "callback_data": f"recv:{deposit['id']}:credit"},
                {"text": "➖ سایر", "callback_data": f"recv:{deposit['id']}:other"},
            ],
        ],
    )


def _finalize_statement(chat_id, user_id, statement_id):
    with core.db() as connection:
        statement = connection.execute(
            "SELECT * FROM company_receipt_statements WHERE id=? AND user_id=?",
            (statement_id, user_id),
        ).fetchone()
        totals = _statement_totals(connection, statement_id)
        difference = round(
            float(statement["statement_total_deposits"]) - float(statement["extracted_total_deposits"]), 2
        )
        reconciled = abs(difference) <= 0.01
        status = "finalized" if reconciled and not totals["pending_count"] else "reconciliation_required"
        now = datetime.utcnow().isoformat()
        connection.execute(
            """
            UPDATE company_receipt_statements
            SET confirmed_revenue=?, excluded_deposits=?, pending_deposits=?,
                reconciliation_difference=?, status=?, updated_at=? WHERE id=?
            """,
            (totals["revenue"], totals["excluded"], totals["pending"], difference, status, now, statement_id),
        )
    if status == "finalized":
        core.set_session(user_id)
        core.send_message(
            chat_id,
            "✅ <b>دریافتی این گزارش قطعی شد</b>\n\n"
            f"📅 دوره: {core.safe(statement['period_start'] or '—')} تا {core.safe(statement['period_end'] or statement['statement_date'])}\n"
            f"🏦 کل واریزی بانک: <b>{_money(statement['statement_total_deposits'])}</b>\n"
            f"💰 دریافتی واقعی مشتریان: <b>{_money(totals['revenue'])}</b>\n"
            f"➖ واریزی‌های غیر درآمدی: <b>{_money(totals['excluded'])}</b>\n"
            "🔒 فقط مبلغ تأییدشده مشتریان وارد داشبورد می‌شود.",
            reply_markup=main_menu(),
        )
    else:
        core.set_session(user_id)
        core.send_message(
            chat_id,
            "⚠️ <b>گزارش نیاز به تطبیق دارد</b>\n\n"
            f"جمع اعلامی بانک: {_money(statement['statement_total_deposits'])}\n"
            f"جمع ردیف‌های استخراج‌شده: {_money(statement['extracted_total_deposits'])}\n"
            f"اختلاف: <b>{_money(abs(difference))}</b>\n\n"
            "برای جلوگیری از ثبت عدد اشتباه، این ماه هنوز وارد دریافتی قطعی نشده است.",
            reply_markup=main_menu(),
        )


def handle_company_statement(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    document = message.get("document") or {}
    if not (document.get("file_name", "").lower().endswith(".pdf") or document.get("mime_type") == "application/pdf"):
        core.send_message(chat_id, "فایل گزارش بانکی باید PDF باشد.", reply_markup=main_menu())
        return
    core.send_message(chat_id, "🔎 گزارش بانکی در حال استخراج و تطبیق است…")
    pdf_bytes = core.download_telegram_file(document["file_id"])
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    with core.db() as connection:
        duplicate = connection.execute(
            "SELECT id, status, confirmed_revenue FROM company_receipt_statements WHERE user_id=? AND source_hash=?",
            (user_id, digest),
        ).fetchone()
    if duplicate:
        core.send_message(
            chat_id,
            f"⚠️ این گزارش قبلاً ثبت شده است. وضعیت: <b>{core.safe(duplicate['status'])}</b> — "
            f"دریافتی قطعی: <b>{_money(duplicate['confirmed_revenue'])}</b>",
            reply_markup=main_menu(),
        )
        return
    data = extract_company_deposits(pdf_bytes)
    if not data["deposits"]:
        raise ValueError("No incoming deposits were extracted")
    extracted_total = round(sum(item["amount"] for item in data["deposits"]), 2)
    now = datetime.utcnow().isoformat()
    path = COMPANY_STATEMENT_DIR / f"{user_id}_{digest[:20]}.pdf"
    path.write_bytes(pdf_bytes)
    with core.db() as connection:
        cursor = connection.execute(
            """
            INSERT INTO company_receipt_statements(
                user_id, statement_date, period_start, period_end, account_last4,
                statement_total_deposits, extracted_total_deposits, pending_deposits,
                reconciliation_difference, source_hash, source_path, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'review', ?, ?)
            """,
            (
                user_id, data["statement_date"], data["period_start"], data["period_end"],
                data["account_last4"], data["statement_total_deposits"], extracted_total,
                extracted_total, round(data["statement_total_deposits"] - extracted_total, 2),
                digest, str(path), now, now,
            ),
        )
        statement_id = cursor.lastrowid
        connection.executemany(
            """
            INSERT INTO company_deposits(
                statement_id, txn_date, description, amount, classification,
                suggested_classification, confidence, reason, created_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            [
                (
                    statement_id, item["date"], item["description"], item["amount"],
                    item["suggested_classification"], item["confidence"], item["reason"], now,
                )
                for item in data["deposits"]
            ],
        )
    core.set_session(user_id, "review_company_deposits", {"statement_id": statement_id})
    core.send_message(
        chat_id,
        f"📄 <b>{len(data['deposits'])} واریزی استخراج شد</b>\n"
        f"جمع ردیف‌ها: {_money(extracted_total)}\n"
        f"جمع اعلامی بانک: {_money(data['statement_total_deposits'])}\n\n"
        "حالا هر واریزی را برای ثبت دقیق تأیید کن.",
    )
    _review_next(chat_id, user_id, statement_id)


def receivables_dashboard(user_id):
    year = datetime.utcnow().strftime("%Y")
    with core.db() as connection:
        ytd = connection.execute(
            """
            SELECT COUNT(*) AS months, COALESCE(SUM(confirmed_revenue), 0) AS received
            FROM company_receipt_statements
            WHERE user_id=? AND status='finalized' AND substr(statement_date,1,4)=?
            """,
            (user_id, year),
        ).fetchone()
        pending = connection.execute(
            "SELECT COUNT(*) AS n FROM company_receipt_statements WHERE user_id=? AND status!='finalized'",
            (user_id,),
        ).fetchone()["n"]
    return (
        f"📥 <b>گزارش دریافتی شرکت — {year}</b>\n\n"
        f"💰 دریافتی قطعی: <b>{_money(ytd['received'])}</b>\n"
        f"📄 گزارش‌های نهایی‌شده: <b>{int(ytd['months'] or 0)}</b>\n"
        f"⏳ گزارش‌های نیازمند بررسی/تطبیق: <b>{int(pending or 0)}</b>\n\n"
        "فقط واریزی‌های تأییدشده مشتریان در عدد بالا محاسبه شده‌اند."
    )


_previous_handle_message = core.handle_message
_previous_handle_callback = core.handle_callback


def handle_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    if not core.is_allowed(user_id):
        core.send_message(chat_id, "⛔️ این ربات خصوصی است.")
        return
    text = message.get("text", "").strip()
    step, payload = core.get_session(user_id)
    if text in {"/receipts", "💰 دریافتی شرکت"}:
        core.set_session(user_id, "await_company_statement", {})
        core.send_message(
            chat_id,
            "📎 فایل PDF <b>گزارش ماهانه حساب شرکت</b> را ارسال کن.\n\n"
            "تمام واریزی‌ها استخراج می‌شوند و قبل از ورود به دریافتی قطعی، یکی‌یکی تأییدشان می‌کنی.",
            reply_markup=main_menu(),
        )
        return
    if text in {"/receipts_report", "📥 گزارش دریافتی"}:
        core.send_message(chat_id, receivables_dashboard(user_id), reply_markup=main_menu())
        return
    if message.get("document") and step == "await_company_statement":
        handle_company_statement(message)
        return
    if step == "review_company_deposits" and not message.get("document"):
        core.send_message(chat_id, "لطفاً نوع واریزی نمایش‌داده‌شده را از دکمه‌ها انتخاب کن.")
        return
    _previous_handle_message(message)


def handle_callback(callback):
    action = callback.get("data", "")
    if not action.startswith("recv:"):
        _previous_handle_callback(callback)
        return
    core.answer_callback(callback["id"])
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    if not core.is_allowed(user_id):
        core.send_message(chat_id, "⛔️ دسترسی مجاز نیست.")
        return
    try:
        _, raw_id, key = action.split(":", 2)
        deposit_id = int(raw_id)
        classification, _ = CLASSIFICATIONS[key]
    except (ValueError, KeyError):
        core.send_message(chat_id, "انتخاب نامعتبر بود.")
        return
    step, payload = core.get_session(user_id)
    statement_id = int(payload.get("statement_id") or 0)
    if step != "review_company_deposits" or not statement_id:
        core.send_message(chat_id, "جلسه بررسی پایان یافته؛ گزارش دریافتی را دوباره باز کن.")
        return
    now = datetime.utcnow().isoformat()
    with core.db() as connection:
        deposit = connection.execute(
            """
            SELECT d.id FROM company_deposits d
            JOIN company_receipt_statements s ON s.id=d.statement_id
            WHERE d.id=? AND d.statement_id=? AND s.user_id=? AND d.classification='pending'
            """,
            (deposit_id, statement_id, user_id),
        ).fetchone()
        if not deposit:
            core.send_message(chat_id, "این واریزی قبلاً بررسی شده یا متعلق به این گزارش نیست.")
            return
        connection.execute(
            "UPDATE company_deposits SET classification=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (classification, user_id, now, deposit_id),
        )
    _review_next(chat_id, user_id, statement_id)


named_accounting.main_menu = main_menu
core.main_menu = main_menu
core.handle_message = handle_message
core.handle_callback = handle_callback
