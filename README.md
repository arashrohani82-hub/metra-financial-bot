# Métra Accounting Bot

Private Telegram bookkeeping assistant for Métra Structure Inc.

## Features

- Extracts merchant, date, subtotal, GST, QST and total from receipt photos.
- Separates company and personal expenses.
- Assigns company expenses to a project code.
- Blocks duplicate receipts using a SHA-256 fingerprint.
- Stores bookkeeping data and receipt images on a persistent Railway volume.
- Exports monthly Excel reports.
- Restricts access to explicitly allowed Telegram user IDs.

## Required Railway configuration

Create a persistent volume mounted at `/data`, then configure the variables shown
in `.env.example`. Never commit real tokens or API keys.

After deployment, register the Telegram webhook once:

`https://YOUR_PUBLIC_URL/setup?key=YOUR_SETUP_SECRET`

The endpoint uses `WEBHOOK_SECRET` to authenticate Telegram webhook requests.


## Project control MVP

The Telegram bot now also provides a lightweight operational dashboard:

- Register active projects with contract value and client.
- Track technical progress, invoiced amounts, and collected amounts.
- Track referral commissions as 15%, 20%, or another configured rate.
- Calculate earned value, billing gaps, outstanding receivables, and accrued referral commission.
- Set a monthly cash-collection target and show target achievement.
- Flag projects whose technical/financial gap needs management attention.

### Telegram workflow

Use the persistent buttons or these commands:

- `/project` — register a project.
- `/projects` — list active projects.
- `/progress` — update technical progress.
- `/money` — record an issued invoice or received payment.
- `/target` — set this month's collection target.
- `/dashboard` — view the management summary.

This MVP records financial events; it does not yet generate/send invoice PDFs or synchronize OneDrive.
