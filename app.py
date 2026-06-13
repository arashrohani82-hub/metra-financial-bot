import os, io, json, logging, random, base64
from datetime import datetime
from flask import Flask, request, jsonify
import requests as req
from anthropic import Anthropic
from concurrent.futures import ThreadPoolExecutor
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.barcharts import VerticalBarChart
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.chart import BarChart, PieChart, Reference
from openpyxl.chart.series import DataPoint

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4)
client = Anthropic()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8632709979:AAGUxEXPk80YRVvrEnEQCpIKIYwLFC635ts")

# ── Persistent storage ──────────────────────────────────────────────────────
DATA_FILE = '/tmp/financial_data.json'
user_data = {}   # session state per user
expenses = {}    # all expenses per user: {uid: [expense, ...]}

def load_data():
    global user_data, expenses
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r') as f:
                saved = json.load(f)
                user_data = saved.get('sessions', {})
                expenses = saved.get('expenses', {})
            logger.info(f"Loaded {sum(len(v) for v in expenses.values())} expenses")
    except Exception as e:
        logger.warning(f"load_data error: {e}")

def save_data():
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump({'sessions': user_data, 'expenses': expenses}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"save_data error: {e}")

load_data()

# ── Canadian accounting categories ─────────────────────────────────────────
COMPANY_CATEGORIES = [
    "🚗 Transport & Véhicule",
    "🍽️ Repas & Représentation",
    "🏢 Bureau & Loyer",
    "💻 Technologie & Logiciels",
    "📞 Télécom & Internet",
    "🔧 Matériel & Équipement",
    "📋 Services Professionnels",
    "📢 Marketing & Publicité",
    "✈️ Voyage & Déplacement",
    "📚 Formation & Développement",
    "🏥 Assurances",
    "🏛️ Taxes & Licences",
    "💼 Fournitures de Bureau",
    "🔨 Sous-traitance",
    "❓ Autre dépense",
]

PERSONAL_CATEGORIES = [
    "🛒 Épicerie & Alimentation",
    "🍔 Restaurants & Sorties",
    "🚗 Transport & Essence",
    "🏠 Logement & Services",
    "👕 Vêtements & Mode",
    "🏥 Santé & Médical",
    "🎬 Loisirs & Divertissement",
    "📱 Abonnements & Tech",
    "✈️ Voyage & Vacances",
    "🎁 Cadeaux & Dons",
    "💰 Épargne & Investissement",
    "❓ Autre dépense",
]

# ── Telegram helpers ────────────────────────────────────────────────────────
def tg(chat_id, text, keyboard=None):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    if keyboard:
        payload['reply_markup'] = {'inline_keyboard': keyboard}
    r = req.post(f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage', json=payload, timeout=15)
    logger.info(f"tg sent: {r.status_code} to {chat_id}")
    return r

def tg_doc(chat_id, buf, filename, caption):
    try:
        buf.seek(0)
        file_bytes = buf.read()
        logger.info(f"tg_doc: sending {filename}, size={len(file_bytes)} bytes")
        resp = req.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendDocument',
            data={'chat_id': chat_id, 'caption': caption},
            files={'document': (filename, file_bytes)},
            timeout=60)
        logger.info(f"tg_doc response: {resp.status_code}")
    except Exception as e:
        import traceback
        logger.error(f"tg_doc error: {e}\n{traceback.format_exc()}")
        tg(chat_id, "❌ Erreur envoi fichier: " + str(e))

def category_keyboard(categories):
    """Build inline keyboard from category list (2 per row)"""
    kb = []
    for i in range(0, len(categories), 2):
        row = [{'text': categories[i], 'callback_data': f'cat_{i}'}]
        if i+1 < len(categories):
            row.append({'text': categories[i+1], 'callback_data': f'cat_{i+1}'})
        kb.append(row)
    return kb

# ── Extract expense from photo ──────────────────────────────────────────────
def do_extract_receipt(chat_id, uid, file_id):
    uid = str(uid)
    try:
        r = req.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}', timeout=10)
        fpath = r.json()['result']['file_path']
        img_r = req.get(f'https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}', timeout=15)
        img_b64 = base64.b64encode(img_r.content).decode()

        prompt = """Extract expense info from this receipt/invoice. Return ONLY JSON:
{"merchant":"","date":"","amount":0.0,"currency":"CAD","description":"","tax_gst":0.0,"tax_qst":0.0}
date format: YYYY-MM-DD. amount: total amount paid. ONLY JSON."""

        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            messages=[{"role":"user","content":[
                {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":img_b64}},
                {"type":"text","text":prompt}
            ]}]
        )
        result = ''.join(b.text for b in response.content if hasattr(b,'text'))
        info = json.loads(result.replace('```json','').replace('```','').strip())

        session = user_data.get(uid, {})
        session['pending_expense'] = {
            'merchant': info.get('merchant','—'),
            'date': datetime.now().strftime('%Y-%m-%d'),  # always today
            'amount': float(info.get('amount') or 0),
            'currency': info.get('currency','CAD'),
            'description': info.get('description',''),
            'tax_gst': float(info.get('tax_gst') or 0),
            'tax_qst': float(info.get('tax_qst') or 0),
        }
        user_data[uid] = session
        save_data()

        exp = session['pending_expense']
        msg = (f"🧾 *Reçu détecté*\n\n"
               f"🏪 {exp['merchant']}\n"
               f"📅 {exp['date']}\n"
               f"💰 ${exp['amount']:.2f} {exp['currency']}\n"
               f"📝 {exp['description']}\n\n"
               f"Cette dépense est *personnelle* ou *d'entreprise*?")
        kb = [
            [{'text':'🏢 Entreprise','callback_data':'type_company'},
             {'text':'👤 Personnelle','callback_data':'type_personal'}]
        ]
        tg(chat_id, msg, kb)

    except Exception as e:
        import traceback
        logger.error(f"do_extract_receipt error: {e}\n{traceback.format_exc()}")
        tg(chat_id, "❌ Erreur extraction: " + str(e))

# ── Generate monthly PDF report ─────────────────────────────────────────────
def generate_pdf_report(uid, month, year):
    uid = str(uid)
    user_expenses = expenses.get(uid, [])
    month_expenses = [e for e in user_expenses
                      if e.get('date','').startswith(f"{year}-{month:02d}")]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=50, leftMargin=50,
                            topMargin=60, bottomMargin=60)
    story = []

    def style(name, font='Helvetica', size=11, bold=False, color=colors.black, align='LEFT', space=6):
        return ParagraphStyle(name, fontName=font+('-Bold' if bold else ''),
                              fontSize=size, textColor=color,
                              leading=size+4, spaceAfter=space,
                              alignment={'LEFT':0,'CENTER':1,'RIGHT':2}[align])

    title_s = style('title', size=18, bold=True, color=colors.HexColor('#2E7D32'), align='CENTER')
    sub_s = style('sub', size=12, color=colors.grey, align='CENTER')
    h2_s = style('h2', size=13, bold=True, color=colors.HexColor('#1B5E20'), space=4)
    normal_s = style('normal', size=10)

    story.append(Paragraph("📊 Rapport de Dépenses Mensuel", title_s))
    story.append(Paragraph(f"Métra Structure Inc. — {datetime(year,month,1).strftime('%B %Y')}", sub_s))
    story.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#2E7D32'), spaceAfter=12))

    if not month_expenses:
        story.append(Paragraph("Aucune dépense enregistrée ce mois.", normal_s))
        doc.build(story)
        buf.seek(0)
        return buf

    # Summary by category
    by_cat = {}
    total = 0
    for e in month_expenses:
        cat = e.get('category', 'Autre')
        by_cat[cat] = by_cat.get(cat, 0) + e.get('amount', 0)
        total += e.get('amount', 0)

    story.append(Paragraph(f"Total du mois: ${total:,.2f} CAD", h2_s))
    story.append(Spacer(1,8))

    # Table header
    table_data = [['Catégorie', 'Montant', '%']]
    for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
        pct = (amt/total*100) if total else 0
        table_data.append([cat, f"${amt:,.2f}", f"{pct:.1f}%"])
    table_data.append(['TOTAL', f"${total:,.2f}", "100%"])

    t = Table(table_data, colWidths=[3.5*inch, 1.5*inch, 1*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#2E7D32')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('ROWBACKGROUNDS', (0,1), (-1,-2), [colors.white, colors.HexColor('#F1F8E9')]),
        ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#C8E6C9')),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#A5D6A7')),
        ('ALIGN', (1,0), (-1,-1), 'RIGHT'),
        ('PADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(t)
    story.append(Spacer(1,16))

    # Expense detail
    story.append(Paragraph("Détail des dépenses", h2_s))
    detail_data = [['Date', 'Marchand', 'Catégorie', 'Type', 'Montant']]
    for e in sorted(month_expenses, key=lambda x: x.get('date','')):
        detail_data.append([
            e.get('date',''),
            (e.get('merchant','') or '')[:20],
            (e.get('category','') or '')[:22],
            'Cie' if e.get('expense_type')=='company' else 'Pers.',
            f"${e.get('amount',0):,.2f}"
        ])

    dt = Table(detail_data, colWidths=[0.9*inch, 1.6*inch, 1.8*inch, 0.6*inch, 1*inch])
    dt.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#388E3C')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#F9FBE7')]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#DCEDC8')),
        ('ALIGN', (4,0), (4,-1), 'RIGHT'),
        ('PADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(dt)

    doc.build(story)
    buf.seek(0)
    return buf

# ── Generate Excel report ───────────────────────────────────────────────────
def generate_excel_report(uid, month, year):
    uid = str(uid)
    user_expenses = expenses.get(uid, [])
    month_expenses = [e for e in user_expenses
                      if e.get('date','').startswith(f"{year}-{month:02d}")]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Dépenses"

    green = "2E7D32"
    light = "F1F8E9"
    header_font = Font(bold=True, color="FFFFFF", size=11)
    green_fill = PatternFill("solid", fgColor=green)

    headers = ['Date', 'Marchand', 'Description', 'Catégorie', 'Type', 'Montant (CAD)', 'TPS', 'TVQ']
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = green_fill
        cell.alignment = Alignment(horizontal='center')

    for row, e in enumerate(sorted(month_expenses, key=lambda x: x.get('date','')), 2):
        ws.cell(row=row, column=1, value=e.get('date',''))
        ws.cell(row=row, column=2, value=e.get('merchant',''))
        ws.cell(row=row, column=3, value=e.get('description',''))
        ws.cell(row=row, column=4, value=e.get('category',''))
        ws.cell(row=row, column=5, value='Entreprise' if e.get('expense_type')=='company' else 'Personnelle')
        ws.cell(row=row, column=6, value=e.get('amount',0)).number_format = '#,##0.00'
        ws.cell(row=row, column=7, value=e.get('tax_gst',0)).number_format = '#,##0.00'
        ws.cell(row=row, column=8, value=e.get('tax_qst',0)).number_format = '#,##0.00'
        if row % 2 == 0:
            for col in range(1,9):
                ws.cell(row=row, column=col).fill = PatternFill("solid", fgColor=light)

    # Summary sheet
    ws2 = wb.create_sheet("Résumé")
    by_cat = {}
    total = 0
    for e in month_expenses:
        cat = e.get('category','Autre')
        by_cat[cat] = by_cat.get(cat,0) + e.get('amount',0)
        total += e.get('amount',0)

    ws2.cell(1,1,"Catégorie").font = header_font
    ws2.cell(1,1).fill = green_fill
    ws2.cell(1,2,"Montant").font = header_font
    ws2.cell(1,2).fill = green_fill
    for r, (cat,amt) in enumerate(sorted(by_cat.items(), key=lambda x:-x[1]), 2):
        ws2.cell(r,1,cat)
        ws2.cell(r,2,amt).number_format = '#,##0.00'

    # Pie chart
    if by_cat:
        pie = PieChart()
        pie.title = f"Dépenses {datetime(year,month,1).strftime('%B %Y')}"
        data = Reference(ws2, min_col=2, min_row=1, max_row=len(by_cat)+1)
        cats = Reference(ws2, min_col=1, min_row=2, max_row=len(by_cat)+1)
        pie.add_data(data, titles_from_data=True)
        pie.set_categories(cats)
        pie.style = 10
        ws2.add_chart(pie, "D2")

    out = '/tmp/report.xlsx'
    wb.save(out)
    with open(out, 'rb') as f:
        buf = io.BytesIO(f.read())
    os.remove(out)
    buf.seek(0)
    return buf

# ── Handle update ───────────────────────────────────────────────────────────
def handle_update(data):
    try:
        msg = data.get('message', {})
        cb = data.get('callback_query', {})

        if cb:
            uid = str(cb['from']['id'])
            chat_id = cb['message']['chat']['id']
            cdata = cb.get('data','')
            try:
                req.post(f'https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery',
                         json={'callback_query_id': cb['id']}, timeout=3)
            except: pass

            session = user_data.get(uid, {})
            pending = session.get('pending_expense', {})

            if cdata == 'type_company':
                pending['expense_type'] = 'company'
                session['pending_expense'] = pending
                session['current_categories'] = COMPANY_CATEGORIES
                user_data[uid] = session
                save_data()
                kb = category_keyboard(COMPANY_CATEGORIES)
                kb.append([{'text':'🔄 Nouveau reçu','callback_data':'nouveau'}])
                tg(chat_id, "📂 *Catégorie* (entreprise):", kb)

            elif cdata == 'type_personal':
                pending['expense_type'] = 'personal'
                session['pending_expense'] = pending
                session['current_categories'] = PERSONAL_CATEGORIES
                user_data[uid] = session
                save_data()
                kb = category_keyboard(PERSONAL_CATEGORIES)
                kb.append([{'text':'🔄 Nouveau reçu','callback_data':'nouveau'}])
                tg(chat_id, "📂 *Catégorie* (personnelle):", kb)

            elif cdata.startswith('cat_'):
                idx = int(cdata.split('_')[1])
                cats = session.get('current_categories', COMPANY_CATEGORIES)
                if idx < len(cats):
                    pending['category'] = cats[idx]
                    # Save expense
                    uid_expenses = expenses.get(uid, [])
                    uid_expenses.append(pending)
                    expenses[uid] = uid_expenses
                    session['pending_expense'] = {}
                    user_data[uid] = session
                    save_data()
                    exp = pending
                    tg(chat_id,
                       f"✅ *Dépense enregistrée!*\n\n"
                       f"🏪 {exp.get('merchant','—')}\n"
                       f"💰 ${exp.get('amount',0):.2f} CAD\n"
                       f"📂 {exp.get('category','—')}\n"
                       f"📅 {exp.get('date','—')}\n\n"
                       "Total ce mois (" + datetime.now().strftime('%B %Y') + f"): ${sum(e['amount'] for e in expenses.get(uid,[]) if e.get('date','').startswith(datetime.now().strftime('%Y-%m'))):.2f} CAD",
                       [[{'text':'📸 Nouveau reçu','callback_data':'nouveau'},
                         {'text':'📊 Rapport mensuel','callback_data':'report'}]])

            elif cdata == 'report':
                now = datetime.now()
                do_report(chat_id, uid, now.month, now.year)

            elif cdata == 'nouveau':
                session['pending_expense'] = {}
                user_data[uid] = session
                save_data()
                tg(chat_id, "📸 Envoyez une photo du reçu ou facture.")

            elif cdata.startswith('report_'):
                parts = cdata.split('_')
                m, y = int(parts[1]), int(parts[2])
                do_report(chat_id, uid, m, y)

        elif msg:
            uid = str(msg['from']['id'])
            chat_id = msg['chat']['id']

            if msg.get('text'):
                text = msg['text']
                if text in ('/start', '/nouveau'):
                    session = user_data.get(uid, {})
                    session['pending_expense'] = {}
                    user_data[uid] = session
                    save_data()
                    tg(chat_id,
                       "👋 *Bienvenue — Métra Finance*\n\n"
                       "📸 Envoyez une photo de votre reçu ou facture\n"
                       "📊 /rapport — Rapport mensuel\n"
                       "🔄 /nouveau — Nouveau reçu",
                       [[{'text':'📊 Rapport ce mois','callback_data':'report'},
                         {'text':'📸 Scanner un reçu','callback_data':'nouveau'}]])
                elif text == '/rapport':
                    now = datetime.now()
                    do_report(chat_id, uid, now.month, now.year)
                else:
                    tg(chat_id, "📸 Envoyez une photo du reçu.\n/rapport pour voir le rapport mensuel.")

            elif msg.get('photo'):
                file_id = msg['photo'][-1]['file_id']
                tg(chat_id, "🔍 Analyse du reçu en cours...")
                executor.submit(do_extract_receipt, chat_id, uid, file_id)

    except Exception as e:
        import traceback
        logger.error(f"handle_update error: {e}\n{traceback.format_exc()}")

def do_report(chat_id, uid, month, year):
    uid = str(uid)
    tg(chat_id, f"⏳ Génération du rapport {datetime(year,month,1).strftime('%B %Y')}...")
    try:
        buf_pdf = generate_pdf_report(uid, month, year)
        month_str = datetime(year,month,1).strftime('%B-%Y')
        tg_doc(chat_id, buf_pdf, f"Rapport_{month_str}.pdf", f"📊 Rapport PDF — {month_str}")
        buf_xl = generate_excel_report(uid, month, year)
        tg_doc(chat_id, buf_xl, f"Rapport_{month_str}.xlsx", f"📊 Rapport Excel — {month_str}")
    except Exception as e:
        import traceback
        logger.error(f"do_report error: {e}\n{traceback.format_exc()}")
        tg(chat_id, "❌ Erreur rapport: " + str(e))

# ── Routes ──────────────────────────────────────────────────────────────────
@app.route('/webhook/telegram', methods=['POST'])
def webhook():
    data = request.get_json(force=True, silent=True)
    if data:
        executor.submit(handle_update, data)
    return 'ok', 200

@app.route('/setup')
def setup():
    railway_url = os.environ.get('RAILWAY_PUBLIC_DOMAIN','')
    if not railway_url:
        railway_url = 'metra-financial-bot.up.railway.app'
    webhook_url = f"https://{railway_url}/webhook/telegram"
    r = req.post(f'https://api.telegram.org/bot{BOT_TOKEN}/setWebhook',
                 json={"url": webhook_url, "allowed_updates": ["message","callback_query"]}, timeout=15)
    info = req.get(f'https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo', timeout=10)
    return jsonify({"set": r.json(), "info": info.json()})

@app.route('/')
def index():
    return jsonify({"status":"ok","bot":"@METRA_FINANCIAL_BOT"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
