import hashlib
import hmac
import json
import logging
import os
import time


from flask import jsonify, request


import named_run as named_accounting
from receipt_ai import extract_receipt_resilient


# Railway starts this module directly. Import the complete runtime so the
# deployed bot includes chequing statement routing from run.py/named_run.py.
accounting = named_accounting.core
app = named_accounting.application
logger = logging.getLogger("metra_bookkeeping_router")
ROUTER_SHARED_SECRET = os.getenv("ROUTER_SHARED_SECRET", "").strip()


# Ensure the Telegram receipt workflow uses the same resilient Anthropic model
# selection as the router. This avoids failures when ANTHROPIC_MODEL points to
# a retired/unavailable model such as claude-3-5-sonnet-20241022.
accounting.extract_receipt = extract_receipt_resilient
logger.info("Receipt AI override active: resilient Anthropic model selection")




def _authorized_router_request():
    supplied = request.headers.get("X-Router-Secret", "")
    return bool(ROUTER_SHARED_SECRET) and hmac.compare_digest(supplied, ROUTER_SHARED_SECRET)




def _forbidden():
    return jsonify({"ok": False, "error": "forbidden"}), 403




def _extract_receipt_with_retry(image_bytes):
    last_error = None
    for attempt in range(2):
        try:
            return extract_receipt_resilient(image_bytes)
        except json.JSONDecodeError as exc:
            last_error = exc
            logger.warning("Malformed AI JSON while extracting receipt (attempt %s/2): %s", attempt + 1, exc)
            if attempt == 0:
                time.sleep(0.4)
                continue
        except Exception as exc:
            last_error = exc
            logger.exception("Receipt extraction failed")
            break
    raise RuntimeError(f"receipt_ai_error: {type(last_error).__name__}: {str(last_error)[:120]}")




@app.get("/status")
def router_status():
    return jsonify({"status": "ok", "service": "metra-bookkeeping"})




@app.post("/router/receipt")
def router_receipt():
    if not _authorized_router_request():
        return _forbidden()
    try:
        try:
