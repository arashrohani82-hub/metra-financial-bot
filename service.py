import hashlib
import hmac
import json
import logging
import os
import time

from flask import jsonify, request

import app as accounting
from receipt_ai import extract_receipt_resilient

app = accounting.app
logger = logging.getLogger("metra_bookkeeping_router")
ROUTER_SHARED_SECRET = os.getenv("ROUTER_SHARED_SECRET", "").strip()


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
            user_id = int(request.form.get("user_id", "0"))
        except ValueError:
            user_id = 0
        if not user_id or not accounting.is_allowed(user_id):
            return jsonify({"ok": False, "error": "user_not_allowed"}), 403
        upload = request.files.get("image")
        if not upload:
            return jsonify({"ok": False, "error": "image_required"}), 400

        image_bytes = upload.read()
        if not image_bytes:
            return jsonify({"ok": False, "error": "empty_image"}), 400
        digest = hashlib.sha256(image_bytes).hexdigest()

        with accounting.db() as connection:
            duplicate = connection.execute(
                "SELECT id, merchant, total FROM expenses WHERE user_id=? AND receipt_hash=?",
                (user_id, digest),
            ).fetchone()
        if duplicate:
            return jsonify({
                "ok": True,
                "duplicate": True,
                "merchant": duplicate["merchant"],
                "total": duplicate["total"],
            })

        receipt_path = accounting.RECEIPT_DIR / f"{user_id}_{digest[:20]}.jpg"
        receipt_path.write_bytes(image_bytes)
        data = _extract_receipt_with_retry(image_bytes)
        data["receipt_hash"] = digest
        data["receipt_path"] = str(receipt_path)
        accounting.set_session(user_id, "choose_type", data)
        return jsonify({"ok": True, "duplicate": False, "receipt": data})
    except Exception as exc:
        logger.exception("Router receipt processing failed")
        return jsonify({
            "ok": False,
            "error": "receipt_processing_failed",
            "detail": str(exc)[:220],
            "error_type": type(exc).__name__,
        }), 500


@app.post("/router/action")
def router_action():
    if not _authorized_router_request():
        return _forbidden()
    body = request.get_json(silent=True) or {}
    try:
        user_id = int(body.get("user_id", 0))
    except (TypeError, ValueError):
        user_id = 0
    action = str(body.get("action") or "")
    if not user_id or not accounting.is_allowed(user_id):
        return jsonify({"ok": False, "error": "user_not_allowed"}), 403

    step, payload = accounting.get_session(user_id)

    if action.startswith("type:") and step == "choose_type":
        expense_type = action.split(":", 1)[1]
        if expense_type not in {"company", "personal"}:
            return jsonify({"ok": False, "error": "invalid_type"}), 400
        payload["expense_type"] = expense_type
        categories = accounting.COMPANY_CATEGORIES if expense_type == "company" else accounting.PERSONAL_CATEGORIES
        payload["categories"] = categories
        accounting.set_session(user_id, "choose_category", payload)
        return jsonify({"ok": True, "state": "choose_category", "categories": categories})

    if action.startswith("category:") and step == "choose_category":
        try:
            index = int(action.split(":", 1)[1])
        except ValueError:
            return jsonify({"ok": False, "error": "invalid_category"}), 400
        categories = payload.get("categories", [])
        if index < 0 or index >= len(categories):
            return jsonify({"ok": False, "error": "invalid_category"}), 400
        payload["category"] = categories[index]
        payload.pop("categories", None)
        if payload.get("expense_type") == "personal":
            payload["project_code"] = None
            accounting.save_expense(user_id, payload)
            accounting.set_session(user_id)
            return jsonify({"ok": True, "state": "saved", "message": "Personal expense saved"})
        accounting.set_session(user_id, "enter_project", payload)
        return jsonify({"ok": True, "state": "enter_project"})

    if action == "project:none" and step == "enter_project":
        payload["project_code"] = None
        accounting.save_expense(user_id, payload)
        accounting.set_session(user_id)
        return jsonify({"ok": True, "state": "saved", "message": "Company expense saved"})

    if action == "cancel":
        accounting.set_session(user_id)
        return jsonify({"ok": True, "state": "cancelled"})

    return jsonify({"ok": False, "error": "invalid_state", "state": step}), 409


@app.post("/router/project")
def router_project():
    if not _authorized_router_request():
        return _forbidden()
    body = request.get_json(silent=True) or {}
    try:
        user_id = int(body.get("user_id", 0))
    except (TypeError, ValueError):
        user_id = 0
    project_code = str(body.get("project_code") or "").strip().upper()[:50]
    if not user_id or not project_code or not accounting.is_allowed(user_id):
        return jsonify({"ok": False, "error": "invalid_request"}), 400
    step, payload = accounting.get_session(user_id)
    if step != "enter_project":
        return jsonify({"ok": False, "error": "invalid_state", "state": step}), 409
    payload["project_code"] = project_code
    accounting.save_expense(user_id, payload)
    accounting.set_session(user_id)
    return jsonify({"ok": True, "state": "saved", "project_code": project_code})
