import base64
import hashlib
import html
import io
import json
import logging
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import openpyxl
import requests
from anthropic import Anthropic
from flask import Flask, jsonify, request
from openpyxl.styles import Alignment, Font, PatternFill

from project_control import (
    create_project,
    dashboard,
    get_project,
    init_project_control,
    list_projects,
    project_metrics,
    record_money,
    set_monthly_target,
    update_progress,
)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("metra_accounting")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
SETUP_SECRET = os.getenv("SETUP_SECRET", "")
ALLOWED_USERS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

if not ALLOWED_USERS:
    raise RuntimeError("ALLOWED_TELEGRAM_USER_IDS is required")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
RECEIPT_DIR = DATA_DIR / "receipts"
RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "accounting.db"

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4)

COMPANY_CATEGORIES = [
    "Transport et véhicule",
    "Repas et représentation",
    "Bureau et loyer",
    "Technologie et logiciels",
    "Télécom et internet",
    "Matériel et équipement",
    "Services professionnels",
    "Marketing et publicité",
    "Voyage et déplacement",
    "Formation",
    "Assurances",
    "Taxes et licences",
    "Fournitures de bureau",
    "Sous-traitance",
    "Autre dépense",
]

PERSONAL_CATEGORIES = [
    "Épicerie et alimentation",
    "Restaurants et sorties",
    "Transport et essence",
    "Logement et services",
    "Vêtements",
    "Santé et médical",
    "Loisirs",
    "Abonnements et technologie",
    "Voyage",
    "Cadeaux et dons",
    "Autre dépense",
]


def db():
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db():
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                user_id INTEGER PRIMARY KEY,
                step TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                merchant TEXT NOT NULL,
                expense_date TEXT NOT NULL,
                subtotal REAL NOT NULL DEFAULT 0,
                gst REAL NOT NULL DEFAULT 0,
                qst REAL NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'CAD',
                description TEXT,
                expense_type TEXT NOT NULL,
                category TEXT NOT NULL,
                project_code TEXT,
                receipt_hash TEXT NOT NULL,
                receipt_path TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, receipt_hash)
            );
            """
        )
        init_project_control(connection)


init_db()


def telegram(method, payload=None, files=None, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(url, json=payload, files=files, timeout=timeout)
    response.raise_for_status()
    return response.json()


def send_message(chat_id, text, keyboard=None, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    elif keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    telegram("sendMessage", payload)


def main_menu():
    return {
        "keyboard": [
            [{"text": "🎯 داشبورد"}, {"text": "📁 پروژه‌ها"}],
            [{"text": "➕ پروژه جدید"}, {"text": "📈 ثبت پیشرفت"}],
            [{"text": "🎯 تعیین تارگت"}, {"text": "💵 ثبت مالی"}],
            [{"text": "🧾 رسید جدید"}, {"text": "📊 گزارش هزینه‌ها"}],
            [{"text": "❌ لغو عملیات"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "عکس رسید را ارسال کنید…",
    }


def send_welcome(chat_id):
    send_message(
        chat_id,
        "👋 <b>ایجنت حسابداری Métra Structure</b>\n\n"
        "برای ثبت هزینه، عکس رسید یا فاکتور را بفرست.\n"
        "همچنین می‌توانی از دکمه‌های پایین صفحه استفاده کنی.",
        reply_markup=main_menu(),
    )


def answer_callback(callback_id):
    try:
        telegram("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        logger.exception("Unable to answer callback")


def set_session(user_id, step=None, payload=None):
    with db() as connection:
        connection.execute(
            """
            INSERT INTO sessions(user_id, step, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                step=excluded.step,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                user_id,
                step,
                json.dumps(payload or {}, ensure_ascii=False),
                datetime.utcnow().isoformat(),
            ),
        )


def get_session(user_id):
    with db() as connection:
        row = connection.execute(
            "SELECT step, payload FROM sessions WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return None, {}
    return row["step"], json.loads(row["payload"])


def is_allowed(user_id):
    return user_id in ALLOWED_USERS


def safe(value):
    return html.escape(str(value or "—"))


def category_keyboard(categories):
    rows = []
    for index, category in enumerate(categories):
        rows.append(
            [{"text": category, "callback_data": f"category:{index}"}]
        )
    rows.append([{"text": "لغو", "callback_data": "cancel"}])
    return rows


def download_receipt(file_id):
    metadata = telegram("getFile", {"file_id": file_id})
    file_path = metadata["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def extract_receipt(image_bytes):
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = """
Extract bookkeeping data from this Canadian receipt or invoice.
Return only valid JSON with this exact schema:
{
  "merchant": "string",
  "date": "YYYY-MM-DD",
  "subtotal": 0.0,
  "gst": 0.0,
  "qst": 0.0,
  "total": 0.0,
  "currency": "CAD",
  "description": "short string"
}
Use 0 for taxes that are not shown. Do not estimate a tax that is not visible.
The total must be the amount paid. If the date is unclear, return an empty string.
"""
    response = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=700,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    raw = "".join(block.text for block in response.content if hasattr(block, "text"))
    raw = raw.replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)
    extracted_date = str(parsed.get("date") or "")
    try:
        datetime.strptime(extracted_date, "%Y-%m-%d")
    except ValueError:
        extracted_date = datetime.now().strftime("%Y-%m-%d")
    return {
        "merchant": str(parsed.get("merchant") or "Unknown"),
        "date": extracted_date,
        "subtotal": float(parsed.get("subtotal") or 0),
        "gst": float(parsed.get("gst") or 0),
        "qst": float(parsed.get("qst") or 0),
        "total": float(parsed.get("total") or 0),
        "currency": str(parsed.get("currency") or "CAD").upper(),
        "description": str(parsed.get("description") or ""),
    }


def receipt_summary(data):
    return (
        "🧾 <b>اطلاعات رسید</b>\n\n"
        f"فروشنده: {safe(data['merchant'])}\n"
        f"تاریخ: {safe(data['date'])}\n"
        f"قبل از مالیات: ${data['subtotal']:.2f}\n"
        f"GST: ${data['gst']:.2f}\n"
        f"QST: ${data['qst']:.2f}\n"
        f"<b>جمع: ${data['total']:.2f} {safe(data['currency'])}</b>\n\n"
        "این هزینه مربوط به شرکت است یا شخصی؟"
    )


def save_expense(user_id, payload):
    with db() as connection:
        connection.execute(
            """
            INSERT INTO expenses(
                user_id, merchant, expense_date, subtotal, gst, qst, total,
                currency, description, expense_type, category, project_code,
                receipt_hash, receipt_path, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                payload["merchant"],
                payload["date"],
                payload["subtotal"],
                payload["gst"],
                payload["qst"],
                payload["total"],
                payload["currency"],
                payload["description"],
                payload["expense_type"],
                payload["category"],
                payload.get("project_code"),
                payload["receipt_hash"],
                payload.get("receipt_path"),
                datetime.utcnow().isoformat(),
            ),
        )


def make_excel(user_id, year, month):
    with db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM expenses
            WHERE user_id=? AND substr(expense_date, 1, 7)=?
            ORDER BY expense_date, id
            """,
            (user_id, f"{year:04d}-{month:02d}"),
        ).fetchall()

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Expenses"
    headers = [
        "Date",
        "Merchant",
        "Description",
        "Type",
        "Category",
        "Project",
        "Subtotal",
        "GST",
        "QST",
        "Total",
        "Currency",
    ]
    sheet.append(headers)
    navy = "102A43"
    orange = "F5A623"
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        sheet.append(
            [
                row["expense_date"],
                row["merchant"],
                row["description"],
                row["expense_type"],
                row["category"],
                row["project_code"] or "",
                row["subtotal"],
                row["gst"],
                row["qst"],
                row["total"],
                row["currency"],
            ]
        )

    for column in "GHIJ":
        for cell in sheet[column][1:]:
            cell.number_format = '$#,##0.00'
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    widths = [12, 24, 32, 12, 28, 16, 14, 12, 12, 14, 10]
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width

    summary = workbook.create_sheet("Summary")
    summary.append(["Metric", "Amount"])
    summary.append(["Subtotal", sum(row["subtotal"] for row in rows)])
    summary.append(["GST", sum(row["gst"] for row in rows)])
    summary.append(["QST", sum(row["qst"] for row in rows)])
    summary.append(["Total", sum(row["total"] for row in rows)])
    for cell in summary[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=orange)
    for cell in summary["B"][1:]:
        cell.number_format = '$#,##0.00'
    summary.column_dimensions["A"].width = 22
    summary.column_dimensions["B"].width = 16

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output, len(rows)


def send_excel(chat_id, user_id, year, month):
    output, count = make_excel(user_id, year, month)
    filename = f"Metra_Bookkeeping_{year:04d}-{month:02d}.xlsx"
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={
            "chat_id": chat_id,
            "caption": f"گزارش {year:04d}-{month:02d} — {count} سند",
        },
        files={
            "document": (
                filename,
                output.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        timeout=60,
    )
    response.raise_for_status()


def report_keyboard():
    now = datetime.now()
    rows = []
    year, month = now.year, now.month
    for _ in range(12):
        rows.append(
            [
                {
                    "text": f"{year:04d}-{month:02d}",
                    "callback_data": f"report:{year}:{month}",
                }
            ]
        )
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return rows


def handle_photo(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    send_message(chat_id, "🔎 رسید در حال بررسی است…")
    image_bytes = download_receipt(message["photo"][-1]["file_id"])
    digest = hashlib.sha256(image_bytes).hexdigest()

    with db() as connection:
        duplicate = connection.execute(
            "SELECT id, merchant, total FROM expenses WHERE user_id=? AND receipt_hash=?",
            (user_id, digest),
        ).fetchone()
    if duplicate:
        send_message(
            chat_id,
            f"⚠️ این رسید قبلاً ثبت شده است: {safe(duplicate['merchant'])} — "
            f"${duplicate['total']:.2f}",
        )
        return

    extension = ".jpg"
    receipt_path = RECEIPT_DIR / f"{user_id}_{digest[:20]}{extension}"
    receipt_path.write_bytes(image_bytes)
    data = extract_receipt(image_bytes)
    data["receipt_hash"] = digest
    data["receipt_path"] = str(receipt_path)
    set_session(user_id, "choose_type", data)
    send_message(
        chat_id,
        receipt_summary(data),
        [
            [
                {"text": "🏢 شرکت", "callback_data": "type:company"},
                {"text": "👤 شخصی", "callback_data": "type:personal"},
            ],
            [{"text": "لغو", "callback_data": "cancel"}],
        ],
    )


def handle_callback(callback):
    answer_callback(callback["id"])
    user_id = callback["from"]["id"]
    chat_id = callback["message"]["chat"]["id"]
    if not is_allowed(user_id):
        send_message(chat_id, "⛔️ دسترسی مجاز نیست.")
        return

    action = callback.get("data", "")
    step, payload = get_session(user_id)

    if action == "money:invoice":
        set_session(user_id, "project_invoice", {})
        send_message(chat_id, "شماره پروژه و مبلغ فاکتور را وارد کن:\n<code>P26-101 | 2500</code>")
        return

    if action == "money:payment":
        set_session(user_id, "project_payment", {})
        send_message(chat_id, "شماره پروژه و مبلغ دریافتی را وارد کن:\n<code>P26-101 | 2500</code>")
        return

    if action == "cancel":
        set_session(user_id)
        send_message(chat_id, "لغو شد.")
        return

    if action.startswith("type:") and step == "choose_type":
        expense_type = action.split(":", 1)[1]
        payload["expense_type"] = expense_type
        categories = (
            COMPANY_CATEGORIES if expense_type == "company" else PERSONAL_CATEGORIES
        )
        payload["categories"] = categories
        set_session(user_id, "choose_category", payload)
        send_message(chat_id, "دسته هزینه را انتخاب کن:", category_keyboard(categories))
        return

    if action.startswith("category:") and step == "choose_category":
        index = int(action.split(":", 1)[1])
        categories = payload.get("categories", [])
        if index >= len(categories):
            raise ValueError("Invalid category")
        payload["category"] = categories[index]
        payload.pop("categories", None)
        if payload.get("expense_type") == "personal":
            payload["project_code"] = None
            save_expense(user_id, payload)
            set_session(user_id)
            send_message(chat_id, "✅ هزینه شخصی با موفقیت ثبت شد.")
            return
        set_session(user_id, "enter_project", payload)
        send_message(
            chat_id,
            "کد پروژه را بنویس؛ مثلاً <b>ODS26-076</b>.\n"
            "اگر هزینه عمومی شرکت است، دکمه زیر را بزن.",
            [[{"text": "هزینه عمومی شرکت", "callback_data": "project:none"}]],
        )
        return

    if action == "project:none" and step == "enter_project":
        payload["project_code"] = None
        save_expense(user_id, payload)
        set_session(user_id)
        send_message(chat_id, "✅ هزینه با موفقیت ثبت شد.")
        return

    if action.startswith("report:"):
        _, year, month = action.split(":")
        send_excel(chat_id, user_id, int(year), int(month))
        return



def money(value):
    return f"${float(value or 0):,.2f}"


def send_dashboard(chat_id, user_id):
    with db() as connection:
        data = dashboard(connection, user_id)
    send_message(
        chat_id,
        "🎯 <b>داشبورد پروژه‌ها</b>\n\n"
        f"پروژه‌های فعال: <b>{data['active_count']}</b>\n"
        f"ارزش قراردادها: <b>{money(data['contract'])}</b>\n"
        f"فاکتور شده: {money(data['invoiced'])}\n"
        f"وصول شده: <b>{money(data['collected'])}</b>\n"
        f"مطالبات باز: {money(data['outstanding'])}\n"
        f"کار انجام‌شده ولی فاکتور نشده: {money(data['billing_gap'])}\n"
        f"کمیسیون معرفِ ایجادشده: {money(data['commissions'])}\n"
        f"موارد نیازمند توجه: <b>{data['at_risk']}</b>\n\n"
        f"🎯 تارگت وصول {safe(data['month'])}: {money(data['collection_target'])}\n"
        f"تحقق تارگت: <b>{data['target_achievement']:.1f}٪</b>\n"
        f"مانده تا تارگت: {money(data['target_remaining'])}",
        reply_markup=main_menu(),
    )


def send_project_list(chat_id, user_id):
    with db() as connection:
        projects = list_projects(connection, user_id)
    if not projects:
        send_message(chat_id, "هنوز پروژه فعالی ثبت نشده است.", reply_markup=main_menu())
        return
    lines = ["📁 <b>پروژه‌های فعال</b>\n"]
    for project in projects[:20]:
        item = project_metrics(project)
        warning = " 🔴" if item["billing_gap"] > max(250, item["contract"] * 0.1) else ""
        lines.append(
            f"<b>{safe(project['project_code'])}</b> — {safe(project['title'])}{warning}\n"
            f"پیشرفت {item['progress']:.0f}٪ | فاکتور {money(item['invoiced'])} | "
            f"وصول {money(item['collected'])}"
        )
    send_message(chat_id, "\n\n".join(lines), reply_markup=main_menu())


def begin_new_project(chat_id, user_id):
    set_session(user_id, "new_project", {})
    send_message(
        chat_id,
        "➕ <b>پروژه جدید</b>\n\n"
        "اطلاعات را در یک خط و با | جدا کن:\n"
        "<code>شماره پروژه | عنوان | مشتری | مبلغ قرارداد | معرف | درصد</code>\n\n"
        "مثال:\n"
        "<code>P26-101 | Inspection façade | ABC Inc. | 10000 | Habitation | 15</code>\n\n"
        "اگر معرف ندارد، دو قسمت آخر را خالی بگذار.",
    )


def handle_new_project_text(chat_id, user_id, text):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < 4:
        send_message(chat_id, "❌ حداقل چهار بخش لازم است: شماره | عنوان | مشتری | مبلغ")
        return
    referrer = parts[4] if len(parts) > 4 else ""
    rate = parts[5] if len(parts) > 5 and parts[5] else 0
    with db() as connection:
        create_project(
            connection, user_id, parts[0], parts[1], parts[2],
            float(parts[3].replace(",", "")), referrer, float(rate)
        )
    set_session(user_id)
    send_message(
        chat_id,
        f"✅ پروژه <b>{safe(parts[0].upper())}</b> ثبت شد.\n"
        "اکنون می‌توانی پیشرفت، فاکتور و پرداخت آن را ثبت کنی.",
        reply_markup=main_menu(),
    )


def handle_progress_text(chat_id, user_id, text):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2:
        send_message(chat_id, "❌ فرمت درست: <code>P26-101 | 40</code>")
        return
    with db() as connection:
        update_progress(connection, user_id, parts[0], float(parts[1]))
    set_session(user_id)
    send_message(
        chat_id,
        f"✅ پیشرفت پروژه <b>{safe(parts[0].upper())}</b> روی {float(parts[1]):.0f}٪ ثبت شد.",
        reply_markup=main_menu(),
    )


def handle_target_text(chat_id, user_id, text):
    amount = float(text.replace(",", "").replace("$", "").strip())
    with db() as connection:
        set_monthly_target(connection, user_id, amount)
    set_session(user_id)
    send_message(
        chat_id,
        f"✅ تارگت وصول این ماه روی <b>{money(amount)}</b> تنظیم شد.",
        reply_markup=main_menu(),
    )


def handle_money_text(chat_id, user_id, text, event_type):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) != 2:
        send_message(chat_id, "❌ فرمت درست: <code>P26-101 | 2500</code>")
        return
    amount = float(parts[1].replace(",", ""))
    with db() as connection:
        record_money(connection, user_id, parts[0], event_type, amount)
    set_session(user_id)
    label = "فاکتور" if event_type == "invoice" else "دریافت"
    send_message(
        chat_id,
        f"✅ {label} {money(amount)} برای پروژه <b>{safe(parts[0].upper())}</b> ثبت شد.",
        reply_markup=main_menu(),
    )


def handle_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    if not is_allowed(user_id):
        send_message(chat_id, "⛔️ این ربات خصوصی است.")
        return

    text = message.get("text", "").strip()
    step, payload = get_session(user_id)

    if text in {"/start", "/help", "❓ راهنما"}:
        send_welcome(chat_id)
    elif text in {"/dashboard", "🎯 داشبورد"}:
        send_dashboard(chat_id, user_id)
    elif text in {"/projects", "📁 پروژه‌ها"}:
        send_project_list(chat_id, user_id)
    elif text in {"/project", "➕ پروژه جدید"}:
        begin_new_project(chat_id, user_id)
    elif text in {"/progress", "📈 ثبت پیشرفت"}:
        set_session(user_id, "project_progress", {})
        send_message(chat_id, "شماره پروژه و درصد پیشرفت را وارد کن:\n<code>P26-101 | 40</code>")
    elif text in {"/target", "🎯 تعیین تارگت"}:
        set_session(user_id, "monthly_target", {})
        send_message(chat_id, "مبلغ تارگت وصول این ماه را وارد کن؛ مثلاً <code>60000</code>")
    elif text in {"/money", "💵 ثبت مالی"}:
        send_message(
            chat_id,
            "چه چیزی ثبت شود؟",
            [[
                {"text": "📄 فاکتور صادرشده", "callback_data": "money:invoice"},
                {"text": "💰 مبلغ وصول‌شده", "callback_data": "money:payment"},
            ]],
        )
    elif text in {"/cancel", "❌ لغو عملیات"}:
        set_session(user_id)
        send_message(chat_id, "لغو شد.", reply_markup=main_menu())
    elif text in {"/report", "📊 گزارش ماهانه", "📊 گزارش هزینه‌ها"}:
        send_message(chat_id, "ماه گزارش را انتخاب کن:", report_keyboard())
    elif text in {"/new", "🧾 رسید جدید"}:
        set_session(user_id)
        send_message(
            chat_id,
            "📷 عکس واضح رسید یا فاکتور را همین‌جا ارسال کن.",
            reply_markup=main_menu(),
        )
    elif step == "new_project" and text:
        handle_new_project_text(chat_id, user_id, text)
    elif step == "project_progress" and text:
        handle_progress_text(chat_id, user_id, text)
    elif step == "monthly_target" and text:
        handle_target_text(chat_id, user_id, text)
    elif step == "project_invoice" and text:
        handle_money_text(chat_id, user_id, text, "invoice")
    elif step == "project_payment" and text:
        handle_money_text(chat_id, user_id, text, "payment")
    elif step == "enter_project" and text:
        payload["project_code"] = text.upper()[:50]
        save_expense(user_id, payload)
        set_session(user_id)
        send_message(
            chat_id,
            f"✅ هزینه برای پروژه <b>{safe(payload['project_code'])}</b> ثبت شد.",
        )
    elif message.get("photo"):
        handle_photo(message)
    else:
        send_message(chat_id, "عکس رسید را بفرست یا از /report استفاده کن.")


def process_update(update):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except sqlite3.IntegrityError:
        logger.warning("Duplicate receipt rejected")
        chat_id = (
            update.get("message", {}).get("chat", {}).get("id")
            or update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
        )
        if chat_id:
            send_message(chat_id, "⚠️ این رسید قبلاً ثبت شده است.")
    except Exception:
        logger.exception("Update processing failed")
        chat_id = (
            update.get("message", {}).get("chat", {}).get("id")
            or update.get("callback_query", {}).get("message", {}).get("chat", {}).get("id")
        )
        if chat_id:
            send_message(chat_id, "❌ پردازش انجام نشد. دوباره امتحان کن.")


@app.post("/webhook/telegram")
def webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if supplied != WEBHOOK_SECRET:
            return "forbidden", 403
    update = request.get_json(silent=True)
    if update:
        executor.submit(process_update, update)
    return "ok", 200


@app.get("/setup")
def setup():
    if not SETUP_SECRET or request.args.get("key") != SETUP_SECRET:
        return "forbidden", 403
    if not PUBLIC_URL:
        return jsonify({"error": "PUBLIC_URL is required"}), 400
    payload = {
        "url": f"{PUBLIC_URL}/webhook/telegram",
        "allowed_updates": ["message", "callback_query"],
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET
    webhook_result = telegram("setWebhook", payload)
    commands_result = telegram(
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "شروع و نمایش منوی اصلی"},
                {"command": "project", "description": "ثبت پروژه جدید"},
                {"command": "projects", "description": "فهرست پروژه‌ها"},
                {"command": "dashboard", "description": "داشبورد مدیریت"},
                {"command": "progress", "description": "ثبت پیشرفت پروژه"},
                {"command": "money", "description": "ثبت فاکتور یا وصول"},
                {"command": "target", "description": "تعیین تارگت ماهانه"},
                {"command": "new", "description": "ثبت رسید جدید"},
                {"command": "report", "description": "گزارش هزینه‌ها"},
                {"command": "help", "description": "راهنمای استفاده"},
                {"command": "cancel", "description": "لغو عملیات جاری"},
            ]
        },
    )
    return jsonify(
        {
            "ok": bool(webhook_result.get("ok") and commands_result.get("ok")),
            "webhook": webhook_result,
            "commands": commands_result,
        }
    )


@app.get("/")
def health():
    return jsonify({"status": "ok", "service": "metra-accounting-bot"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
