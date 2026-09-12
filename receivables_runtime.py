import hashlib
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

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

OUTFLOW_CLASSIFICATIONS = {
    "expense": "company_expense",
    "recorded": "already_recorded",
    "card": "credit_card_payment",
    "transfer": "internal_transfer",
    "loan": "loan_or_tax_payment",
    "personal": "owner_or_personal",
    "refund": "client_refund",
    "other": "other_non_expense",
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

        CREATE TABLE IF NOT EXISTS company_outflow_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            statement_total_withdrawals REAL NOT NULL DEFAULT 0,
            extracted_total_withdrawals REAL NOT NULL DEFAULT 0,
            confirmed_new_expenses REAL NOT NULL DEFAULT 0,
            excluded_outflows REAL NOT NULL DEFAULT 0,
            pending_outflows REAL NOT NULL DEFAULT 0,
            reconciliation_difference REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'analyzing',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(statement_id) REFERENCES company_receipt_statements(id)
        );

        CREATE TABLE IF NOT EXISTS company_bank_outflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL,
            statement_id INTEGER NOT NULL,
            txn_date TEXT,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            classification TEXT NOT NULL DEFAULT 'pending',
            suggested_classification TEXT,
            confidence REAL NOT NULL DEFAULT 0,
            reason TEXT,
            matched_expense_id INTEGER,
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(analysis_id) REFERENCES company_outflow_analyses(id),
            FOREIGN KEY(statement_id) REFERENCES company_receipt_statements(id)
        );

        CREATE INDEX IF NOT EXISTS idx_company_outflows_review
        ON company_bank_outflows(analysis_id, classification, id);
        """
    )
    statement_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(company_receipt_statements)")
    }
    if "original_filename" not in statement_columns:
        connection.execute(
            "ALTER TABLE company_receipt_statements ADD COLUMN original_filename TEXT"
        )


def _menu():
    menu = _base_main_menu()
    rows = [list(row) for row in menu["keyboard"]]
    insert_at = max(0, len(rows) - 1)
    rows.insert(
        insert_at,
        [{"text": "💰 دریافتی شرکت"}, {"text": "📥 گزارش دریافتی"}],
    )
    rows.insert(
        insert_at + 1,
        [{"text": "💸 بررسی هزینه‌های بانکی"}, {"text": "📉 گزارش هزینه شرکت"}],
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


def extract_company_outflows(pdf_bytes):
    text = _pdf_text(pdf_bytes)
    prompt = """
Extract EVERY outgoing debit/withdrawal transaction from this COMPANY bank statement.
Return JSON only, with this exact shape:
{
  "statement_total_withdrawals":0.00,
  "outflows":[
    {"date":"YYYY-MM-DD","description":"exact bank description","amount":0.00,
     "suggested_classification":"company_expense|credit_card_payment|internal_transfer|loan_or_tax_payment|owner_or_personal|client_refund|other_non_expense|unknown",
     "confidence":0.0,"reason":"short reason"}
  ]
}

STRICT RULES:
1. Use exact printed amounts; never estimate, round, combine, or invent a transaction.
2. Include every debit/withdrawal contributing to the bank's Total withdrawals/debits.
3. Do not include deposits, opening/closing balances, or summary totals as transactions.
4. A credit-card payment, own-account transfer, loan principal, tax remittance, owner draw,
   client refund or other money movement is not automatically an accounting expense.
5. Use company_expense only for a clearly identifiable business operating cost.
6. If uncertain, use unknown. Human review makes the final decision.
7. statement_total_withdrawals must be the exact summary total printed by the bank.
8. Preserve each printed transaction description exactly, as far as text extraction permits.

BANK STATEMENT TEXT:
""" + text[:140000]
    model = _choose_model(core.ANTHROPIC_API_KEY)
    response = core.Anthropic(api_key=core.ANTHROPIC_API_KEY).messages.create(
        model=model,
        max_tokens=7000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _json_object(raw)
    allowed = set(OUTFLOW_CLASSIFICATIONS.values()) | {"unknown"}
    outflows = []
    for item in data.get("outflows") or []:
        amount = round(abs(float(item.get("amount") or 0)), 2)
        description = str(item.get("description") or "").strip()
        if amount <= 0 or not description:
            continue
        suggestion = str(item.get("suggested_classification") or "unknown")
        if suggestion not in allowed:
            suggestion = "unknown"
        outflows.append({
            "date": _iso_date(item.get("date")),
            "description": description[:500],
            "amount": amount,
            "suggested_classification": suggestion,
            "confidence": min(1.0, max(0.0, float(item.get("confidence") or 0))),
            "reason": str(item.get("reason") or "")[:300],
        })
    return {
        "statement_total_withdrawals": round(
            abs(float(data.get("statement_total_withdrawals") or 0)), 2
        ),
        "outflows": outflows,
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
    statement_label = statement["original_filename"] or f"گزارش #{statement_id}"
    statement_period = statement["period_end"] or statement["statement_date"]
    core.send_message(
        chat_id,
        f"🔎 <b>بررسی واریزی {reviewed + 1} از {total}</b>\n"
        f"📄 فایل: <b>{core.safe(statement_label)}</b>\n"
        f"🗓 دوره گزارش: <b>{core.safe(statement_period)}</b>\n\n"
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
        core.send_message(
            chat_id,
            "⚠️ <b>گزارش نیاز به تطبیق دارد</b>\n\n"
            f"جمع اعلامی بانک: {_money(statement['statement_total_deposits'])}\n"
            f"جمع ردیف‌های استخراج‌شده: {_money(statement['extracted_total_deposits'])}\n"
            f"اختلاف: <b>{_money(abs(difference))}</b>\n\n"
            "برای جلوگیری از ثبت عدد اشتباه، این ماه هنوز وارد دریافتی قطعی نشده است.",
            reply_markup=main_menu(),
        )


def handle_company_statement(message, pdf_bytes=None):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    document = message.get("document") or {}
    if not (document.get("file_name", "").lower().endswith(".pdf") or document.get("mime_type") == "application/pdf"):
        core.send_message(chat_id, "فایل گزارش بانکی باید PDF باشد.", reply_markup=main_menu())
        return
    core.send_message(chat_id, "🔎 گزارش بانکی در حال استخراج و تطبیق است…")
    if pdf_bytes is None:
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
        connection.execute(
            "UPDATE company_receipt_statements SET original_filename=? WHERE id=?",
            (document.get("file_name", "")[:300], statement_id),
        )
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
    core.set_session(user_id)
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
        statements = connection.execute(
            """
            SELECT s.*,
              SUM(CASE WHEN d.classification='pending' THEN 1 ELSE 0 END) AS pending_count
            FROM company_receipt_statements s
            LEFT JOIN company_deposits d ON d.statement_id=s.id
            WHERE s.user_id=?
            GROUP BY s.id
            ORDER BY COALESCE(s.period_end, s.statement_date) DESC, s.id DESC
            LIMIT 18
            """,
            (user_id,),
        ).fetchall()
    lines = []
    status_labels = {
        "finalized": "✅ نهایی",
        "review": "⏳ در انتظار بررسی",
        "reconciliation_required": "⚠️ نیازمند تطبیق",
    }
    for statement in statements:
        period = statement["period_end"] or statement["statement_date"]
        filename = statement["original_filename"] or f"گزارش #{statement['id']}"
        status = status_labels.get(statement["status"], statement["status"])
        if statement["status"] == "finalized":
            detail = f"دریافتی {_money(statement['confirmed_revenue'])}"
        elif statement["status"] == "review":
            detail = f"{int(statement['pending_count'] or 0)} واریزی بررسی‌نشده"
        else:
            detail = f"اختلاف {_money(abs(statement['reconciliation_difference']))}"
        lines.append(
            f"• <b>{core.safe(period)}</b> — {status}\n"
            f"  {core.safe(filename)}\n"
            f"  {detail}"
        )
    statement_list = "\n\n".join(lines) if lines else "هنوز گزارشی ثبت نشده است."
    return (
        f"📥 <b>گزارش دریافتی شرکت — {year}</b>\n\n"
        f"💰 دریافتی قطعی: <b>{_money(ytd['received'])}</b>\n"
        f"📄 گزارش‌های نهایی‌شده: <b>{int(ytd['months'] or 0)}</b>\n"
        f"⏳ گزارش‌های نیازمند بررسی/تطبیق: <b>{int(pending or 0)}</b>\n\n"
        "فقط واریزی‌های تأییدشده مشتریان در عدد بالا محاسبه شده‌اند.\n\n"
        f"<b>وضعیت فایل‌ها:</b>\n{statement_list}"
    )


def receivables_dashboard_keyboard(user_id):
    with core.db() as connection:
        statements = connection.execute(
            """
            SELECT s.id, COALESCE(s.period_end, s.statement_date) AS period,
                   SUM(CASE WHEN d.classification='pending' THEN 1 ELSE 0 END) AS pending_count
            FROM company_receipt_statements s
            JOIN company_deposits d ON d.statement_id=s.id
            WHERE s.user_id=? AND s.status='review'
            GROUP BY s.id
            HAVING pending_count > 0
            ORDER BY period DESC, s.id DESC
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()
    return [
        [{
            "text": f"▶️ ادامه {row['period']} ({int(row['pending_count'])} مورد)",
            "callback_data": f"recvstmt:{row['id']}",
        }]
        for row in statements
    ]


def _receipt_match(connection, user_id, txn_date, amount):
    if not txn_date:
        return None
    return connection.execute(
        """
        SELECT id, merchant, expense_date, total
        FROM expenses
        WHERE user_id=? AND expense_type='company'
          AND ABS(total - ?) <= 0.02
          AND ABS(julianday(expense_date) - julianday(?)) <= 5
        ORDER BY ABS(julianday(expense_date) - julianday(?)), id
        LIMIT 1
        """,
        (user_id, amount, txn_date, txn_date),
    ).fetchone()


def _outflow_totals(connection, analysis_id):
    return connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN classification='company_expense' THEN amount ELSE 0 END), 0) AS expenses,
          COALESCE(SUM(CASE WHEN classification NOT IN ('company_expense','pending') THEN amount ELSE 0 END), 0) AS excluded,
          COALESCE(SUM(CASE WHEN classification='pending' THEN amount ELSE 0 END), 0) AS pending,
          SUM(CASE WHEN classification='pending' THEN 1 ELSE 0 END) AS pending_count
        FROM company_bank_outflows WHERE analysis_id=?
        """,
        (analysis_id,),
    ).fetchone()


def _review_next_outflow(chat_id, user_id, analysis_id):
    with core.db() as connection:
        analysis = connection.execute(
            """
            SELECT a.*, s.original_filename, s.period_end, s.statement_date
            FROM company_outflow_analyses a
            JOIN company_receipt_statements s ON s.id=a.statement_id
            WHERE a.id=? AND a.user_id=?
            """,
            (analysis_id, user_id),
        ).fetchone()
        outflow = connection.execute(
            "SELECT * FROM company_bank_outflows WHERE analysis_id=? AND classification='pending' ORDER BY id LIMIT 1",
            (analysis_id,),
        ).fetchone()
        reviewed = connection.execute(
            "SELECT COUNT(*) AS n FROM company_bank_outflows WHERE analysis_id=? AND classification!='pending'",
            (analysis_id,),
        ).fetchone()["n"]
        total = connection.execute(
            "SELECT COUNT(*) AS n FROM company_bank_outflows WHERE analysis_id=?",
            (analysis_id,),
        ).fetchone()["n"]
        match = None
        if outflow and outflow["matched_expense_id"]:
            match = connection.execute(
                "SELECT merchant, expense_date, total FROM expenses WHERE id=?",
                (outflow["matched_expense_id"],),
            ).fetchone()
    if not analysis:
        core.send_message(chat_id, "تحلیل هزینه پیدا نشد.", reply_markup=main_menu())
        return
    if not outflow:
        _finalize_outflows(chat_id, user_id, analysis_id)
        return
    labels = {
        "company_expense": "هزینه جدید شرکت",
        "already_recorded": "قبلاً با رسید ثبت شده",
        "credit_card_payment": "پرداخت کارت اعتباری",
        "internal_transfer": "انتقال داخلی",
        "loan_or_tax_payment": "وام یا پرداخت مالیاتی",
        "owner_or_personal": "شخصی / برداشت مالک",
        "client_refund": "برگشت وجه مشتری",
        "other_non_expense": "سایر غیرهزینه",
        "unknown": "نامشخص",
    }
    suggestion = labels.get(outflow["suggested_classification"], "نامشخص")
    match_text = ""
    if match:
        match_text = (
            f"\n🧾 تطبیق احتمالی با رسید: <b>{core.safe(match['merchant'])}</b> — "
            f"{core.safe(match['expense_date'])} — {_money(match['total'])}"
        )
    core.send_message(
        chat_id,
        f"💸 <b>بررسی برداشت {reviewed + 1} از {total}</b>\n"
        f"📄 فایل: <b>{core.safe(analysis['original_filename'] or ('گزارش #' + str(analysis['statement_id'])))}</b>\n"
        f"🗓 دوره: <b>{core.safe(analysis['period_end'] or analysis['statement_date'])}</b>\n\n"
        f"📅 تاریخ: <b>{core.safe(outflow['txn_date'] or 'ثبت نشده')}</b>\n"
        f"📝 شرح بانک: <b>{core.safe(outflow['description'])}</b>\n"
        f"💵 مبلغ: <b>{_money(outflow['amount'])}</b>\n"
        f"🤖 پیشنهاد: {suggestion} — {float(outflow['confidence'] or 0) * 100:.0f}%"
        f"{match_text}\n\nنوع این برداشت را تأیید کن:",
        [
            [
                {"text": "✅ هزینه جدید", "callback_data": f"out:{outflow['id']}:expense"},
                {"text": "🧾 قبلاً ثبت شده", "callback_data": f"out:{outflow['id']}:recorded"},
            ],
            [
                {"text": "💳 پرداخت کارت", "callback_data": f"out:{outflow['id']}:card"},
                {"text": "🔁 انتقال داخلی", "callback_data": f"out:{outflow['id']}:transfer"},
            ],
            [
                {"text": "🏦 وام/مالیات", "callback_data": f"out:{outflow['id']}:loan"},
                {"text": "👤 شخصی/مالک", "callback_data": f"out:{outflow['id']}:personal"},
            ],
            [
                {"text": "↩️ برگشت مشتری", "callback_data": f"out:{outflow['id']}:refund"},
                {"text": "➖ سایر", "callback_data": f"out:{outflow['id']}:other"},
            ],
        ],
    )


def _finalize_outflows(chat_id, user_id, analysis_id):
    with core.db() as connection:
        analysis = connection.execute(
            "SELECT * FROM company_outflow_analyses WHERE id=? AND user_id=?",
            (analysis_id, user_id),
        ).fetchone()
        totals = _outflow_totals(connection, analysis_id)
        difference = round(
            float(analysis["statement_total_withdrawals"])
            - float(analysis["extracted_total_withdrawals"]), 2
        )
        reconciled = abs(difference) <= 0.01
        status = "finalized" if reconciled and not totals["pending_count"] else "reconciliation_required"
        connection.execute(
            """
            UPDATE company_outflow_analyses
            SET confirmed_new_expenses=?, excluded_outflows=?, pending_outflows=?,
                reconciliation_difference=?, status=?, updated_at=? WHERE id=?
            """,
            (
                totals["expenses"], totals["excluded"], totals["pending"], difference,
                status, datetime.utcnow().isoformat(), analysis_id,
            ),
        )
    if status == "finalized":
        core.send_message(
            chat_id,
            "✅ <b>هزینه‌های این گزارش نهایی شد</b>\n\n"
            f"🏦 کل برداشت بانک: <b>{_money(analysis['statement_total_withdrawals'])}</b>\n"
            f"💸 هزینه جدید شرکت: <b>{_money(totals['expenses'])}</b>\n"
            f"➖ انتقال/موارد غیرهزینه یا قبلاً ثبت‌شده: <b>{_money(totals['excluded'])}</b>\n\n"
            "فقط «هزینه جدید شرکت» به مجموع هزینه‌ها اضافه می‌شود.",
            [[{"text": "▶️ پردازش گزارش بعدی", "callback_data": "outnext"}]],
        )
    else:
        core.send_message(
            chat_id,
            "⚠️ <b>برداشت‌های گزارش نیازمند تطبیق است</b>\n\n"
            f"جمع بانک: {_money(analysis['statement_total_withdrawals'])}\n"
            f"جمع استخراج‌شده: {_money(analysis['extracted_total_withdrawals'])}\n"
            f"اختلاف: <b>{_money(abs(difference))}</b>\n"
            "تا رفع اختلاف، هزینه‌های این فایل وارد جمع قطعی نمی‌شود.",
            reply_markup=main_menu(),
        )


def start_next_expense_statement(chat_id, user_id):
    with core.db() as connection:
        statement = connection.execute(
            """
            SELECT s.* FROM company_receipt_statements s
            LEFT JOIN company_outflow_analyses a ON a.statement_id=s.id
            WHERE s.user_id=? AND s.status='finalized' AND a.id IS NULL
            ORDER BY COALESCE(s.period_end, s.statement_date), s.id
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if statement:
            now = datetime.utcnow().isoformat()
            cursor = connection.execute(
                """
                INSERT INTO company_outflow_analyses(statement_id, user_id, status, created_at, updated_at)
                VALUES (?, ?, 'analyzing', ?, ?)
                """,
                (statement["id"], user_id, now, now),
            )
            analysis_id = cursor.lastrowid
    if not statement:
        core.send_message(
            chat_id,
            "✅ همه گزارش‌های بانکی ذخیره‌شده برای هزینه‌ها پردازش شده‌اند.",
            reply_markup=main_menu(),
        )
        return
    filename = statement["original_filename"] or f"گزارش #{statement['id']}"
    core.send_message(chat_id, f"🔎 استخراج برداشت‌های <b>{core.safe(filename)}</b> در حال انجام است…")
    try:
        pdf_bytes = Path(statement["source_path"]).read_bytes()
        data = extract_company_outflows(pdf_bytes)
        extracted_total = round(sum(item["amount"] for item in data["outflows"]), 2)
        now = datetime.utcnow().isoformat()
        with core.db() as connection:
            rows = []
            for item in data["outflows"]:
                match = _receipt_match(connection, user_id, item["date"], item["amount"])
                suggestion = item["suggested_classification"]
                reason = item["reason"]
                confidence = item["confidence"]
                if match:
                    suggestion = "already_recorded"
                    confidence = max(confidence, 0.95)
                    reason = f"Matched existing company receipt #{match['id']}"
                rows.append((
                    analysis_id, statement["id"], item["date"], item["description"],
                    item["amount"], suggestion, confidence, reason,
                    match["id"] if match else None, now,
                ))
            connection.executemany(
                """
                INSERT INTO company_bank_outflows(
                    analysis_id, statement_id, txn_date, description, amount,
                    classification, suggested_classification, confidence, reason,
                    matched_expense_id, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                rows,
            )
            difference = round(data["statement_total_withdrawals"] - extracted_total, 2)
            connection.execute(
                """
                UPDATE company_outflow_analyses
                SET statement_total_withdrawals=?, extracted_total_withdrawals=?,
                    pending_outflows=?, reconciliation_difference=?, status='review', updated_at=?
                WHERE id=?
                """,
                (
                    data["statement_total_withdrawals"], extracted_total, extracted_total,
                    difference, now, analysis_id,
                ),
            )
        core.send_message(
            chat_id,
            f"📄 <b>{len(data['outflows'])} برداشت استخراج شد</b>\n"
            f"جمع ردیف‌ها: {_money(extracted_total)}\n"
            f"جمع اعلامی بانک: {_money(data['statement_total_withdrawals'])}\n\n"
            "هر برداشت را تأیید کن؛ تطبیق‌های احتمالی با رسید مشخص شده‌اند.",
        )
        _review_next_outflow(chat_id, user_id, analysis_id)
    except Exception:
        with core.db() as connection:
            connection.execute("DELETE FROM company_bank_outflows WHERE analysis_id=?", (analysis_id,))
            connection.execute("DELETE FROM company_outflow_analyses WHERE id=?", (analysis_id,))
        raise


def expense_dashboard(user_id):
    year = datetime.utcnow().strftime("%Y")
    with core.db() as connection:
        receipts = connection.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS total FROM expenses
            WHERE user_id=? AND expense_type='company' AND substr(expense_date,1,4)=?
            """,
            (user_id, year),
        ).fetchone()
        bank = connection.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(confirmed_new_expenses),0) AS total
            FROM company_outflow_analyses a
            JOIN company_receipt_statements s ON s.id=a.statement_id
            WHERE a.user_id=? AND a.status='finalized' AND substr(s.statement_date,1,4)=?
            """,
            (user_id, year),
        ).fetchone()
        remaining = connection.execute(
            """
            SELECT COUNT(*) AS n FROM company_receipt_statements s
            LEFT JOIN company_outflow_analyses a ON a.statement_id=s.id
            WHERE s.user_id=? AND s.status='finalized' AND (a.id IS NULL OR a.status!='finalized')
            """,
            (user_id,),
        ).fetchone()["n"]
    combined = float(receipts["total"] or 0) + float(bank["total"] or 0)
    return (
        f"📉 <b>هزینه‌های شرکت — {year}</b>\n\n"
        f"🧾 هزینه ثبت‌شده با رسید: <b>{_money(receipts['total'])}</b> ({int(receipts['n'] or 0)} مورد)\n"
        f"🏦 هزینه جدید تأییدشده از بانک: <b>{_money(bank['total'])}</b> ({int(bank['n'] or 0)} گزارش)\n"
        f"💸 مجموع بدون تکرار: <b>{_money(combined)}</b>\n"
        f"⏳ گزارش‌های باقی‌مانده/ناتمام: <b>{int(remaining or 0)}</b>"
    )


def expense_dashboard_keyboard(user_id):
    with core.db() as connection:
        analyses = connection.execute(
            """
            SELECT a.id, COALESCE(s.period_end,s.statement_date) AS period,
              SUM(CASE WHEN o.classification='pending' THEN 1 ELSE 0 END) AS pending_count
            FROM company_outflow_analyses a
            JOIN company_receipt_statements s ON s.id=a.statement_id
            JOIN company_bank_outflows o ON o.analysis_id=a.id
            WHERE a.user_id=? AND a.status='review'
            GROUP BY a.id
            HAVING pending_count > 0
            ORDER BY period, a.id
            LIMIT 12
            """,
            (user_id,),
        ).fetchall()
    rows = [
        [{
            "text": f"▶️ ادامه هزینه {row['period']} ({int(row['pending_count'])} مورد)",
            "callback_data": f"outstmt:{row['id']}",
        }]
        for row in analyses
    ]
    rows.append([{"text": "➕ پردازش گزارش بعدی", "callback_data": "outnext"}])
    return rows


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
        core.set_session(user_id)
        review_buttons = receivables_dashboard_keyboard(user_id)
        core.send_message(
            chat_id,
            receivables_dashboard(user_id),
            review_buttons if review_buttons else None,
        )
        core.send_message(chat_id, "از منوی زیر ادامه بده:", reply_markup=main_menu())
        return
    if text in {"/bank_expenses", "💸 بررسی هزینه‌های بانکی"}:
        core.set_session(user_id)
        start_next_expense_statement(chat_id, user_id)
        return
    if text in {"/expenses_report", "📉 گزارش هزینه شرکت"}:
        core.set_session(user_id)
        core.send_message(chat_id, expense_dashboard(user_id), expense_dashboard_keyboard(user_id))
        core.send_message(chat_id, "از منوی زیر ادامه بده:", reply_markup=main_menu())
        return
    if message.get("document"):
        document = message["document"]
        is_pdf = (
            document.get("file_name", "").lower().endswith(".pdf")
            or document.get("mime_type") == "application/pdf"
        )
        if step == "await_company_statement":
            handle_company_statement(message)
            return
        if is_pdf:
            pdf_bytes = core.download_telegram_file(document["file_id"])
            try:
                preview = _pdf_text(pdf_bytes)[:5000].lower()
            except Exception:
                preview = ""
            business_markers = (
                "business account", "business banking", "business deposit account",
                "compte d'entreprise", "compte entreprise",
            )
            if any(marker in preview for marker in business_markers):
                handle_company_statement(message, pdf_bytes=pdf_bytes)
                return
    if step == "review_company_deposits" and not message.get("document"):
        core.send_message(chat_id, "لطفاً نوع واریزی نمایش‌داده‌شده را از دکمه‌ها انتخاب کن.")
        return
    _previous_handle_message(message)


def handle_callback(callback):
    action = callback.get("data", "")
    if not (
        action.startswith("recv:") or action.startswith("recvstmt:")
        or action.startswith("out:") or action.startswith("outstmt:")
        or action == "outnext"
    ):
        _previous_handle_callback(callback)
        return
    core.answer_callback(callback["id"])
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    if not core.is_allowed(user_id):
        core.send_message(chat_id, "⛔️ دسترسی مجاز نیست.")
        return
    if action == "outnext":
        start_next_expense_statement(chat_id, user_id)
        return
    if action.startswith("outstmt:"):
        try:
            analysis_id = int(action.split(":", 1)[1])
        except ValueError:
            core.send_message(chat_id, "گزارش هزینه نامعتبر بود.")
            return
        _review_next_outflow(chat_id, user_id, analysis_id)
        return
    if action.startswith("out:"):
        try:
            _, raw_id, key = action.split(":", 2)
            outflow_id = int(raw_id)
            classification = OUTFLOW_CLASSIFICATIONS[key]
        except (ValueError, KeyError):
            core.send_message(chat_id, "انتخاب هزینه نامعتبر بود.")
            return
        now = datetime.utcnow().isoformat()
        with core.db() as connection:
            outflow = connection.execute(
                """
                SELECT o.id, o.analysis_id FROM company_bank_outflows o
                JOIN company_outflow_analyses a ON a.id=o.analysis_id
                WHERE o.id=? AND a.user_id=? AND o.classification='pending'
                """,
                (outflow_id, user_id),
            ).fetchone()
            if not outflow:
                core.send_message(chat_id, "این برداشت قبلاً بررسی شده یا متعلق به شما نیست.")
                return
            analysis_id = int(outflow["analysis_id"])
            connection.execute(
                "UPDATE company_bank_outflows SET classification=?, reviewed_by=?, reviewed_at=? WHERE id=?",
                (classification, user_id, now, outflow_id),
            )
        _review_next_outflow(chat_id, user_id, analysis_id)
        return
    if action.startswith("recvstmt:"):
        try:
            statement_id = int(action.split(":", 1)[1])
        except ValueError:
            core.send_message(chat_id, "گزارش نامعتبر بود.")
            return
        with core.db() as connection:
            owned = connection.execute(
                "SELECT id FROM company_receipt_statements WHERE id=? AND user_id=?",
                (statement_id, user_id),
            ).fetchone()
        if not owned:
            core.send_message(chat_id, "این گزارش پیدا نشد.")
            return
        _review_next(chat_id, user_id, statement_id)
        return
    try:
        _, raw_id, key = action.split(":", 2)
        deposit_id = int(raw_id)
        classification, _ = CLASSIFICATIONS[key]
    except (ValueError, KeyError):
        core.send_message(chat_id, "انتخاب نامعتبر بود.")
        return
    now = datetime.utcnow().isoformat()
    with core.db() as connection:
        deposit = connection.execute(
            """
            SELECT d.id, d.statement_id FROM company_deposits d
            JOIN company_receipt_statements s ON s.id=d.statement_id
            WHERE d.id=? AND s.user_id=? AND d.classification='pending'
            """,
            (deposit_id, user_id),
        ).fetchone()
        if not deposit:
            core.send_message(chat_id, "این واریزی قبلاً بررسی شده یا متعلق به این گزارش نیست.")
            return
        statement_id = int(deposit["statement_id"])
        connection.execute(
            "UPDATE company_deposits SET classification=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (classification, user_id, now, deposit_id),
        )
    _review_next(chat_id, user_id, statement_id)


named_accounting.main_menu = main_menu
core.main_menu = main_menu
core.handle_message = handle_message
core.handle_callback = handle_callback
