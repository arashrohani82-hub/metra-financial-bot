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
