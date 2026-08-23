import run as bot

core = bot.core
CARD_NAME = "Credit Card RBC"
CHEQUING_NAME = "Chequing RBC"

_original_budget_control_text = bot.budget_control_text
_original_chequing_statement_summary = bot.chequing_statement_summary
_original_chequing_dashboard = bot.chequing_dashboard
_original_card_statement_summary = core.card_statement_summary
_original_card_dashboard = core.card_dashboard
_original_handle_message = core.handle_message


def budget_control_text(user_id, month=None):
    return _original_budget_control_text(user_id, month).replace("Chequing:", f"{CHEQUING_NAME}:")


def chequing_statement_summary(data, user_id=None):
    text = _original_chequing_statement_summary(data, user_id=user_id)
    return text.replace("RBC Chequing", CHEQUING_NAME)


def chequing_dashboard(user_id):
    text = _original_chequing_dashboard(user_id)
    return text.replace("RBC Chequing", CHEQUING_NAME).replace("Chequing:", f"{CHEQUING_NAME}:")


def card_statement_summary(data):
    text = _original_card_statement_summary(data)
    return text.replace("صورت‌حساب", f"{CARD_NAME} — صورت‌حساب", 1)


def card_dashboard(user_id):
    text = _original_card_dashboard(user_id)
    return text.replace("کنترل کارت شخصی", f"کنترل {CARD_NAME}").replace("Chequing:", f"{CHEQUING_NAME}:")


def main_menu():
    return {
        "keyboard": [
            [{"text": "🧾 رسید جدید"}, {"text": "📊 گزارش ماهانه"}],
            [{"text": f"💳 {CARD_NAME}"}, {"text": f"📈 کنترل {CARD_NAME}"}],
            [{"text": f"🏦 {CHEQUING_NAME}"}, {"text": f"📊 کنترل {CHEQUING_NAME}"}],
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
        f"💳 {CARD_NAME}: PDF صورت‌حساب کارت را بفرست.\n"
        f"🏦 {CHEQUING_NAME}: PDF صورت‌حساب چکینگ را بفرست.\n"
        "🎯 گزارش‌ها هزینه واقعی را با شاخص‌های بودجه ماهانه مقایسه می‌کنند.",
        reply_markup=main_menu(),
    )


def handle_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == f"💳 {CARD_NAME}":
        core.set_session(user_id)
        core.send_message(
            chat_id,
            f"📎 فایل PDF صورت‌حساب <b>{CARD_NAME}</b> را همین‌جا ارسال کن.",
            reply_markup=main_menu(),
        )
        return
    if text in {"/card", f"📈 کنترل {CARD_NAME}"}:
        core.send_message(chat_id, card_dashboard(user_id), reply_markup=main_menu())
        return
    if text in {"/chequing", f"🏦 {CHEQUING_NAME}"}:
        core.set_session(user_id, "await_chequing", {})
        core.send_message(
            chat_id,
            f"📎 فایل PDF صورت‌حساب <b>{CHEQUING_NAME}</b> را همین‌جا ارسال کن.",
            reply_markup=main_menu(),
        )
        return
    if text in {"/chequing_control", f"📊 کنترل {CHEQUING_NAME}"}:
        core.send_message(chat_id, chequing_dashboard(user_id), reply_markup=main_menu())
        return

    _original_handle_message(message)


bot.budget_control_text = budget_control_text
bot.chequing_statement_summary = chequing_statement_summary
bot.chequing_dashboard = chequing_dashboard
core.card_statement_summary = card_statement_summary
core.card_dashboard = card_dashboard
core.main_menu = main_menu
core.send_welcome = send_welcome
core.handle_message = handle_message

application = core.app
