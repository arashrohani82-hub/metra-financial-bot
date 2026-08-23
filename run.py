import hashlib
import html
import io
import json
import os
import re
from datetime import datetime

import openpyxl
from pypdf import PdfReader

import app as core

CHEQUING_DIR = core.DATA_DIR / "chequing_statements"
CHEQUING_DIR.mkdir(parents=True, exist_ok=True)
CHEQUING_BASELINE_MONTH = os.getenv("CHEQUING_BASELINE_MONTH", "2026-03")
CHEQUING_BUFFER_TARGET = float(os.getenv("CHEQUING_BUFFER_TARGET", "2000"))

with core.db() as connection:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS chequing_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            statement_date TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            opening_balance REAL NOT NULL DEFAULT 0,
            total_deposits REAL NOT NULL DEFAULT 0,
            total_withdrawals REAL NOT NULL DEFAULT 0,
            closing_balance REAL NOT NULL DEFAULT 0,
            overdraft_interest REAL NOT NULL DEFAULT 0,
            nsf_fees REAL NOT NULL DEFAULT 0,
            nsf_count INTEGER NOT NULL DEFAULT 0,
            monthly_fees REAL NOT NULL DEFAULT 0,
            account_last4 TEXT,
            source_hash TEXT NOT NULL,
            source_path TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, source_hash),
            UNIQUE(user_id, statement_date, account_last4)
        );

        CREATE TABLE IF NOT EXISTS personal_budget_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            statement_date TEXT NOT NULL,
            source_type TEXT NOT NULL,
            costco_spend REAL NOT NULL DEFAULT 0,
            other_card_spend REAL NOT NULL DEFAULT 0,
            chequing_spend REAL NOT NULL DEFAULT 0,
            source_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, source_type, source_hash)
        );
        """
    )


def _amount(value):
    return float(value.replace(",", ""))


def _signed_summary_money(text, pattern):
    match = re.search(pattern + r"\s*([+=-]?)\s*(-?)\s*\$?\s*([\d,]+\.\d{2})", text, re.I)
    if not match:
        raise ValueError(f"Missing financial field: {pattern}")
    sign_token, negative, number = match.groups()
    value = _amount(number)
    if sign_token == "-" or negative == "-":
        value = -value
    return value


def _sum_keyword_amounts(lines, keyword_regex):
    total = 0.0
    for line in lines:
        if re.search(keyword_regex, line, re.I):
            nums = re.findall(r"(?<!\d)([\d,]+\.\d{2})(?!\d)", line)
            if nums:
                total += _amount(nums[0])
    return total


def _pdf_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _extract_json(raw):
    cleaned = raw.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        raise ValueError("Budget analysis JSON was not found")
    return json.loads(match.group(0))


def analyze_card_budget(pdf_bytes):
    text = _pdf_text(pdf_bytes)
    prompt = """
Analyze this RBC Mastercard statement only for the user's MONTHLY PERSONAL BUDGET CONTROL.
Return only JSON exactly like this:
{"costco_spend":0.0,"other_card_spend":0.0}

Rules:
1. costco_spend = all personal Costco Wholesale and Costco gasoline purchases in the statement period, net of Costco refunds.
2. other_card_spend = all other personal discretionary purchases on the card, including Walmart, Metro, groceries, restaurants, personal retail and other personal day-to-day purchases.
3. Exclude company/business expenses, card payments, cash transfers, refunds/credits that relate to excluded business items, purchase interest, fees, professional licensing, business software, business equipment, business marketing and other clearly business expenses.
4. Do not count Costco again in other_card_spend.
5. Use the transaction amounts printed on the statement. Do not estimate.

STATEMENT TEXT:
""" + text
    response = core.Anthropic(api_key=core.ANTHROPIC_API_KEY).messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(raw)
    return {
        "costco_spend": max(0.0, float(data.get("costco_spend") or 0)),
        "other_card_spend": max(0.0, float(data.get("other_card_spend") or 0)),
    }


def analyze_chequing_budget(pdf_bytes):
    text = _pdf_text(pdf_bytes)
    prompt = """
Analyze this RBC Chequing statement only for the user's MONTHLY DISCRETIONARY CHEQUING BUDGET.
Return only JSON exactly like this:
{"chequing_spend":0.0}

Count only day-to-day personal purchases paid directly from chequing such as debit/contactless purchases and similar discretionary spending.
Exclude all fixed commitments and money movements, including Toyota Finance, RBC loans, Fairstone or other loan payments, Garderie, Windsor, Bell, Videotron, Hydro, bank fees, overdraft interest, mortgage, credit-card payments, online banking transfers, BR TO BR transfers, e-Transfers sent or received, cash movements between own accounts, NSF fees and any company/business reimbursement.
Use only printed transaction amounts. Do not estimate.

STATEMENT TEXT:
""" + text
    response = core.Anthropic(api_key=core.ANTHROPIC_API_KEY).messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text"))
    data = _extract_json(raw)
    return {"chequing_spend": max(0.0, float(data.get("chequing_spend") or 0))}


def save_budget_observation(user_id, statement_date, source_type, digest, costco=0.0, other=0.0, chequing=0.0):
    with core.db() as connection:
        connection.execute(
            """
            INSERT INTO personal_budget_observations(
                user_id, statement_date, source_type, costco_spend,
                other_card_spend, chequing_spend, source_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, source_type, source_hash) DO UPDATE SET
                statement_date=excluded.statement_date,
                costco_spend=excluded.costco_spend,
                other_card_spend=excluded.other_card_spend,
                chequing_spend=excluded.chequing_spend
            """,
            (
                user_id, statement_date, source_type, costco, other, chequing,
                digest, datetime.utcnow().isoformat(),
            ),
        )


def _budget_status_line(label, actual, target):
    percent = actual / target * 100 if target else 0
    remaining = target - actual
    if actual <= target:
        return f"🟢 {label}: <b>${actual:,.2f} / ${target:,.0f}</b> — {percent:.0f}٪ — مانده ${remaining:,.2f}"
    return f"🔴 {label}: <b>${actual:,.2f} / ${target:,.0f}</b> — {percent:.0f}٪ — اضافه ${abs(remaining):,.2f}"


def budget_control_text(user_id, month=None):
    with core.db() as connection:
        params = [user_id]
        where = "user_id=?"
        if month:
            where += " AND substr(statement_date, 1, 7)=?"
            params.append(month)
        rows = connection.execute(
            f"SELECT * FROM personal_budget_observations WHERE {where} ORDER BY statement_date, id",
            params,
        ).fetchall()
    if not rows:
        return "\n\n🎯 <b>کنترل شاخص‌ها</b>\nهنوز داده کافی برای مقایسه ثبت نشده است."

    card_rows = [r for r in rows if r["source_type"] == "card"]
    cheq_rows = [r for r in rows if r["source_type"] == "chequing"]
    card = card_rows[-1] if card_rows else None
    cheq = cheq_rows[-1] if cheq_rows else None

    lines = ["\n\n🎯 <b>کنترل شاخص‌های ماهانه</b>"]
    if cheq:
        lines.append(_budget_status_line("Chequing", cheq["chequing_spend"], core.PERSONAL_BUDGET_TARGETS["Chequing"]))
    else:
        lines.append("⚪ Chequing: صورت‌حساب این ماه ثبت نشده")
    if card:
        lines.append(_budget_status_line("Costco", card["costco_spend"], core.PERSONAL_BUDGET_TARGETS["Costco"]))
        lines.append(_budget_status_line("Walmart + Metro + Other", card["other_card_spend"], core.PERSONAL_BUDGET_TARGETS["Walmart + Metro + Other"]))
    else:
        lines.append("⚪ Costco: صورت‌حساب کارت این ماه ثبت نشده")
        lines.append("⚪ Walmart + Metro + Other: صورت‌حساب کارت این ماه ثبت نشده")
    return "\n".join(lines)


def parse_rbc_chequing(pdf_bytes):
    text = _pdf_text(pdf_bytes)
    compact = re.sub(r"[ \t]+", " ", text)
    if "RBC personal banking" not in compact or "Total deposits into your account" not in compact:
        raise ValueError("This is not an RBC personal chequing statement")

    period = re.search(
        r"From\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\s+to\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        compact, re.I,
    )
    if not period:
        raise ValueError("Chequing statement period was not found")

    def parse_date(value):
        cleaned = re.sub(r"\s+", " ", value.replace(",", "").strip())
        return datetime.strptime(cleaned.title(), "%B %d %Y").strftime("%Y-%m-%d")

    period_start = parse_date(period.group(1))
    period_end = parse_date(period.group(2))
    opening = _signed_summary_money(compact, r"Your opening balance on\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4}")
    deposits = _signed_summary_money(compact, r"Total deposits into your account")
    withdrawals = abs(_signed_summary_money(compact, r"Total withdrawals from your account"))
    closing = _signed_summary_money(compact, r"Your closing balance on\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4}")

    account = re.search(r"Your account number:\s*\d{5}-(\d{7})", compact, re.I)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    overdraft_interest = _sum_keyword_amounts(lines, r"Overdraft interest")
    nsf_fees = _sum_keyword_amounts(lines, r"NSF item fee")
    monthly_fees = _sum_keyword_amounts(lines, r"(?<!Partial )Monthly fee(?! Rebate)")
    nsf_count = sum(1 for line in lines if re.search(r"Item returned NSF", line, re.I))

    return {
        "statement_date": period_end,
        "period_start": period_start,
        "period_end": period_end,
        "opening_balance": opening,
        "total_deposits": deposits,
        "total_withdrawals": withdrawals,
        "closing_balance": closing,
        "overdraft_interest": overdraft_interest,
        "nsf_fees": nsf_fees,
        "nsf_count": nsf_count,
        "monthly_fees": monthly_fees,
        "account_last4": account.group(1)[-4:] if account else None,
    }


def save_chequing_statement(user_id, data, digest, source_path):
    with core.db() as connection:
        connection.execute(
            """
            INSERT INTO chequing_statements(
                user_id, statement_date, period_start, period_end,
                opening_balance, total_deposits, total_withdrawals, closing_balance,
                overdraft_interest, nsf_fees, nsf_count, monthly_fees, account_last4,
                source_hash, source_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, data["statement_date"], data["period_start"], data["period_end"],
                data["opening_balance"], data["total_deposits"], data["total_withdrawals"],
                data["closing_balance"], data["overdraft_interest"], data["nsf_fees"],
                data["nsf_count"], data["monthly_fees"], data["account_last4"], digest,
                str(source_path), datetime.utcnow().isoformat(),
            ),
        )


def chequing_statement_summary(data, user_id=None):
    net = data["total_deposits"] - data["total_withdrawals"]
    safe_to_spend = data["closing_balance"] - CHEQUING_BUFFER_TARGET
    ratio = (data["total_withdrawals"] / data["total_deposits"] * 100) if data["total_deposits"] else 0
    alerts = []
    if data["closing_balance"] < 0:
        alerts.append("🔴 مانده حساب منفی است")
    elif data["closing_balance"] < CHEQUING_BUFFER_TARGET:
        alerts.append(f"🟠 مانده زیر بافر هدف ${CHEQUING_BUFFER_TARGET:,.0f} است")
    else:
        alerts.append("🟢 بافر نقدی فعلاً بالاتر از هدف است")
    if net < 0:
        alerts.append(f"🔴 خروجی دوره ${abs(net):,.2f} بیشتر از ورودی بوده")
    if data["overdraft_interest"] > 0:
        alerts.append(f"🔴 بهره overdraft: ${data['overdraft_interest']:,.2f}")
    if data["nsf_count"] or data["nsf_fees"]:
        alerts.append(f"🔴 NSF: {data['nsf_count']} مورد / ${data['nsf_fees']:,.2f} کارمزد")
    result = (
        f"🏦 <b>RBC Chequing — {data['statement_date']}</b>\n\n"
        f"ورودی دوره: <b>${data['total_deposits']:,.2f}</b>\n"
        f"برداشت دوره: <b>${data['total_withdrawals']:,.2f}</b>\n"
        f"Cash Flow خالص: <b>${net:,.2f}</b>\n"
        f"مانده پایان: <b>${data['closing_balance']:,.2f}</b>\n"
        f"نسبت برداشت به ورودی: {ratio:.1f}٪\n"
        f"Safe-to-Spend بعد از بافر: <b>${safe_to_spend:,.2f}</b>\n"
        f"Overdraft interest: ${data['overdraft_interest']:,.2f}\n"
        f"NSF fees: ${data['nsf_fees']:,.2f}\n\n" + "\n".join(alerts)
    )
    if user_id:
        result += budget_control_text(user_id, data["statement_date"][:7])
    return result


def chequing_dashboard(user_id):
    with core.db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM chequing_statements
            WHERE user_id=? AND substr(statement_date, 1, 7) >= ?
            ORDER BY statement_date
            """,
            (user_id, CHEQUING_BASELINE_MONTH),
        ).fetchall()
    if not rows:
        return "هنوز صورت‌حساب RBC Chequing ثبت نشده است. PDF صورت‌حساب را ارسال کن."

    deposits = sum(r["total_deposits"] for r in rows)
    withdrawals = sum(r["total_withdrawals"] for r in rows)
    overdraft = sum(r["overdraft_interest"] for r in rows)
    nsf_fees = sum(r["nsf_fees"] for r in rows)
    nsf_count = sum(r["nsf_count"] for r in rows)
    latest = rows[-1]
    avg_withdrawals = withdrawals / len(rows)
    avg_deposits = deposits / len(rows)
    avg_net = (deposits - withdrawals) / len(rows)
    safe_to_spend = latest["closing_balance"] - CHEQUING_BUFFER_TARGET

    if latest["closing_balance"] < 0 or nsf_count:
        status = "🔴 نیازمند اقدام"
    elif latest["closing_balance"] < CHEQUING_BUFFER_TARGET or avg_net < 0:
        status = "🟠 نیازمند توجه"
    else:
        status = "🟢 تحت کنترل"

    return (
        f"📊 <b>کنترل RBC Chequing از {CHEQUING_BASELINE_MONTH}</b>\n\n"
        f"تعداد دوره‌ها: {len(rows)}\n"
        f"میانگین ورودی: <b>${avg_deposits:,.2f}/ماه</b>\n"
        f"میانگین برداشت: <b>${avg_withdrawals:,.2f}/ماه</b>\n"
        f"میانگین Cash Flow خالص: <b>${avg_net:,.2f}/ماه</b>\n"
        f"آخرین مانده: <b>${latest['closing_balance']:,.2f}</b>\n"
        f"بافر هدف: ${CHEQUING_BUFFER_TARGET:,.2f}\n"
        f"Safe-to-Spend: <b>${safe_to_spend:,.2f}</b>\n"
        f"Overdraft interest مجموع: ${overdraft:,.2f}\n"
        f"NSF: {nsf_count} مورد / ${nsf_fees:,.2f}\n"
        f"وضعیت: {status}"
        + budget_control_text(user_id, latest["statement_date"][:7])
    )


def handle_chequing_document(message, pdf_bytes=None):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    document = message["document"]
    filename = document.get("file_name", "chequing.pdf")
    if document.get("mime_type") != "application/pdf" and not filename.lower().endswith(".pdf"):
        core.send_message(chat_id, "فقط PDF صورت‌حساب RBC Chequing قابل پردازش است.")
        return
    core.send_message(chat_id, "🔎 صورت‌حساب RBC Chequing در حال بررسی است…")
    if pdf_bytes is None:
        pdf_bytes = core.download_telegram_file(document["file_id"])
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    data = parse_rbc_chequing(pdf_bytes)

    with core.db() as connection:
        duplicate = connection.execute(
            "SELECT statement_date FROM chequing_statements WHERE user_id=? AND source_hash=?",
            (user_id, digest),
        ).fetchone()

    try:
        budget = analyze_chequing_budget(pdf_bytes)
        save_budget_observation(
            user_id, data["statement_date"], "chequing", digest,
            chequing=budget["chequing_spend"],
        )
    except Exception:
        core.logger.exception("Chequing budget analysis failed")

    if duplicate:
        core.send_message(
            chat_id,
            f"⚠️ صورت‌حساب {duplicate['statement_date']} قبلاً ثبت شده است."
            + budget_control_text(user_id, data["statement_date"][:7]),
            reply_markup=main_menu(),
        )
        core.set_session(user_id)
        return

    path = CHEQUING_DIR / f"{user_id}_{data['statement_date']}_{digest[:12]}.pdf"
    path.write_bytes(pdf_bytes)
    save_chequing_statement(user_id, data, digest, path)
    core.set_session(user_id)
    core.send_message(chat_id, chequing_statement_summary(data, user_id=user_id), reply_markup=main_menu())


_original_card_handler = core.handle_card_document
_original_card_dashboard = core.card_dashboard
_original_make_excel = core.make_excel


def handle_card_document_with_budget(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    document = message["document"]
    pdf_bytes = core.download_telegram_file(document["file_id"])
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        data = core.parse_rbc_statement(pdf_bytes)
        budget = analyze_card_budget(pdf_bytes)
        save_budget_observation(
            user_id, data["statement_date"], "card", digest,
            costco=budget["costco_spend"], other=budget["other_card_spend"],
        )
    except Exception:
        core.logger.exception("Card budget analysis failed")
        data = None

    _original_card_handler(message)
    if data:
        core.send_message(
            chat_id,
            budget_control_text(user_id, data["statement_date"][:7]),
            reply_markup=main_menu(),
        )


def card_dashboard_with_budget(user_id):
    base = _original_card_dashboard(user_id)
    with core.db() as connection:
        latest = connection.execute(
            "SELECT statement_date FROM card_statements WHERE user_id=? ORDER BY statement_date DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    month = latest["statement_date"][:7] if latest else None
    return base + budget_control_text(user_id, month)


def make_excel_with_budget(user_id, year, month):
    output, count = _original_make_excel(user_id, year, month)
    output.seek(0)
    workbook = openpyxl.load_workbook(output)
    sheet = workbook["Summary"]
    target_month = f"{year:04d}-{month:02d}"

    with core.db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM personal_budget_observations
            WHERE user_id=? AND substr(statement_date,1,7)=?
            ORDER BY statement_date, id
            """,
            (user_id, target_month),
        ).fetchall()

    card_rows = [r for r in rows if r["source_type"] == "card"]
    cheq_rows = [r for r in rows if r["source_type"] == "chequing"]
    card = card_rows[-1] if card_rows else None
    cheq = cheq_rows[-1] if cheq_rows else None

    sheet.append([])
    sheet.append(["Budget control", "Actual", "Target", "Status"])
    comparisons = [
        ("Chequing", cheq["chequing_spend"] if cheq else None, core.PERSONAL_BUDGET_TARGETS["Chequing"]),
        ("Costco", card["costco_spend"] if card else None, core.PERSONAL_BUDGET_TARGETS["Costco"]),
        ("Walmart + Metro + Other", card["other_card_spend"] if card else None, core.PERSONAL_BUDGET_TARGETS["Walmart + Metro + Other"]),
    ]
    for label, actual, target in comparisons:
        status = "NO DATA" if actual is None else ("OK" if actual <= target else "OVER")
        sheet.append([label, actual if actual is not None else "", target, status])
    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 16
    sheet.column_dimensions["C"].width = 16
    sheet.column_dimensions["D"].width = 12

    updated = io.BytesIO()
    workbook.save(updated)
    updated.seek(0)
    return updated, count


def main_menu():
    return {
        "keyboard": [
            [{"text": "🧾 رسید جدید"}, {"text": "📊 گزارش ماهانه"}],
            [{"text": "💳 صورت‌حساب کارت"}, {"text": "📈 کنترل کارت"}],
            [{"text": "🏦 صورت‌حساب چکینگ"}, {"text": "📊 کنترل چکینگ"}],
            [{"text": "❓ راهنما"}, {"text": "❌ لغو عملیات"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "رسید یا PDF صورت‌حساب را ارسال کنید…",
    }


def send_welcome(chat_id):
    core.send_message(
        chat_id,
        "👋 <b>ایجنت حسابداری Métra</b>\n\n"
        "🧾 رسید/فاکتور: عکس را بفرست.\n"
        "💳 Mastercard: PDF صورت‌حساب کارت را بفرست.\n"
        "🏦 RBC Chequing: PDF صورت‌حساب چکینگ را بفرست.\n"
        "🎯 گزارش‌ها هزینه واقعی را با سقف Chequing 300، Costco 600 و Walmart + Metro + Other 200 مقایسه می‌کنند.",
        reply_markup=main_menu(),
    )


_old_handle_message = core.handle_message


def handle_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    if not core.is_allowed(user_id):
        core.send_message(chat_id, "⛔️ این ربات خصوصی است.")
        return

    text = message.get("text", "").strip()
    step, _ = core.get_session(user_id)

    if text in {"/chequing", "🏦 صورت‌حساب چکینگ"}:
        core.set_session(user_id, "await_chequing", {})
        core.send_message(chat_id, "📎 فایل PDF صورت‌حساب <b>RBC Chequing</b> را همین‌جا ارسال کن.", reply_markup=main_menu())
        return
    if text in {"/chequing_control", "📊 کنترل چکینگ"}:
        core.send_message(chat_id, chequing_dashboard(user_id), reply_markup=main_menu())
        return
    if text in {"/card", "📈 کنترل کارت"}:
        core.send_message(chat_id, card_dashboard_with_budget(user_id), reply_markup=main_menu())
        return

    if message.get("document"):
        document = message["document"]
        filename = document.get("file_name", "").lower()
        if step == "await_chequing" or "chequing" in filename or "checking" in filename:
            handle_chequing_document(message)
            return
        if filename.endswith(".pdf") or document.get("mime_type") == "application/pdf":
            pdf_bytes = core.download_telegram_file(document["file_id"])
            try:
                preview = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf_bytes)).pages[:1])
            except Exception:
                preview = ""
            if "RBC personal banking" in preview and "Total deposits into your account" in preview:
                handle_chequing_document(message, pdf_bytes=pdf_bytes)
                return
            handle_card_document_with_budget(message)
            return

    _old_handle_message(message)


core.main_menu = main_menu
core.send_welcome = send_welcome
core.handle_message = handle_message
core.handle_card_document = handle_card_document_with_budget
core.card_dashboard = card_dashboard_with_budget
core.make_excel = make_excel_with_budget

application = core.app
