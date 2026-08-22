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
- Accepts RBC Mastercard PDF statements and tracks personal-card spending.
- Uses April 2026 as the clean post-consolidation baseline by default.
- Alerts on interest, fees, spending above budget and high credit utilization.

## Required Railway configuration

Create a persistent volume mounted at `/data`, then configure the variables shown
in `.env.example`. Never commit real tokens or API keys.

`CARD_BASELINE_MONTH` controls the first month included in the personal-card
dashboard. `CARD_MONTHLY_BUDGET` controls the monthly spending warning threshold.

After deployment, register the Telegram webhook once:

`https://YOUR_PUBLIC_URL/setup?key=YOUR_SETUP_SECRET`

The endpoint uses `WEBHOOK_SECRET` to authenticate Telegram webhook requests.
