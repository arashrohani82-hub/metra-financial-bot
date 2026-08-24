import base64
import json
import os
from datetime import datetime

import requests
from anthropic import Anthropic


def _available_model_ids(api_key: str):
    response = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return [item.get("id", "") for item in data.get("data", []) if item.get("id")]


def _choose_model(api_key: str):
    configured = os.getenv("ANTHROPIC_MODEL", "").strip()
    models = _available_model_ids(api_key)
    if configured and configured in models:
        return configured
    sonnets = [m for m in models if "sonnet" in m.lower()]
    if sonnets:
        return sonnets[0]
    if models:
        return models[0]
    raise RuntimeError("No Anthropic models are available for this API key")


def extract_receipt_resilient(image_bytes: bytes):
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = _choose_model(api_key)
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
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=700,
        messages=[{
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
        }],
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
        "model_used": model,
    }
