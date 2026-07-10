"""
Fix Your Finance — Flask backend (Railway deploy target)
----------------------------------------------------------
v2: switched narration from Groq to the Claude API, fixed the debt-spiral
single-loan bug (see debt_engine.py), added a grounding guardrail so the
model can't invent lender-specific policy details, and added a second
path for people whose problem ISN'T a structured "list your loans" case
(insurance, tax, retirement, property, fraud, savings, agriculture,
salary, guarantor liability -- see general_triage.py).

ENV VARS REQUIRED (set these in Railway):
  RAZORPAY_KEY_ID
  RAZORPAY_KEY_SECRET
  ANTHROPIC_API_KEY
  REPORT_PRICE_PAISE      (e.g. "19900" for ₹199 — Razorpay uses paise)
  ALLOWED_ORIGIN          (your frontend origin, e.g. https://yourname.github.io)
  CLAUDE_MODEL            (optional, defaults to "claude-sonnet-5" below --
                           check docs.claude.com for current model names/
                           pricing before launch, these change over time.
                           "claude-haiku-4-5-20251001" is a cheaper option
                           if per-report cost matters more than quality at
                           your price point.)

Run locally:
  pip install flask flask-cors --break-system-packages
  export RAZORPAY_KEY_ID=... RAZORPAY_KEY_SECRET=... ANTHROPIC_API_KEY=...
  python app.py
"""

import os
import hmac
import hashlib
import json
import urllib.request
import urllib.error
import base64

from flask import Flask, request, jsonify
from flask_cors import CORS

from debt_engine import run_full_analysis, free_preview
from general_triage import classify_domain, free_preview_for_text, build_general_prompt, STANDARD_DISCLAIMER

app = Flask(__name__)
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGIN}})

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
REPORT_PRICE_PAISE = int(os.environ.get("REPORT_PRICE_PAISE", "19900"))  # default ₹199

RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

MAX_SITUATION_TEXT_CHARS = 4000  # guard against absurdly long / abusive input


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _razorpay_auth_header():
    token = base64.b64encode(f"{RAZORPAY_KEY_ID}:{RAZORPAY_KEY_SECRET}".encode()).decode()
    return f"Basic {token}"


def _post_json(url, payload, headers):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed


def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    if not RAZORPAY_KEY_SECRET:
        return False
    payload = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def call_claude(prompt: str, max_tokens: int = 600) -> str:
    """Single-purpose Claude call: one user turn, no system prompt needed
    since every prompt builder below already frames the task fully.
    Returns "" on any failure so callers can fall back to a safe default
    message instead of crashing the report."""
    if not ANTHROPIC_API_KEY:
        return ""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    status, resp = _post_json(ANTHROPIC_URL, payload, headers)
    if status != 200:
        return ""
    try:
        blocks = resp.get("content", [])
        text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
        return "".join(text_parts).strip()
    except (KeyError, IndexError, TypeError):
        return ""


DEBT_GUARDRAIL_BLOCK = """Grounding rules -- follow these exactly:
- Only use the numbers given below. Never invent a specific lender's name or
  policy (e.g. do not claim "your lender allows partial redemption in 25%
  steps" or similar) -- if something like that could genuinely help, tell
  them to confirm it with their specific lender directly instead of stating
  it as fact.
- No judgment, no lecturing about "avoid unnecessary spending."
- No generic encouragement line about hope or believing in themselves at the
  end -- keep it practical, like a knowledgeable relative giving it to them
  straight."""


def build_debt_narration_prompt(analysis: dict) -> str:
    risk = analysis["risk"]
    loans = analysis["loans_in_payoff_order"]
    loan_lines = []
    for l in loans:
        status = "UNDERWATER (balance is growing, not shrinking)" if l["is_underwater"] else f"clears in ~{l['months_to_payoff']} months at current EMI"
        loan_lines.append(
            f"- {l['name']} ({l['debt_type']}): outstanding ₹{l['outstanding_balance']:.0f}, "
            f"rate {l['annual_rate_pct']}%, EMI ₹{l['emi_amount']:.0f} — {status}"
        )
    loans_block = "\n".join(loan_lines)
    blended = f"{risk['blended_rate_pct']}%" if risk.get("blended_rate_pct") is not None else "n/a"

    return f"""You are explaining a household debt situation to someone in tier-2/rural India who is
stressed and possibly ashamed about their debt. Use plain, direct language. No jargon
("DTI", "amortization" etc are for internal use only — translate them into everyday terms).

Overall verdict: {risk['level'].upper()}
Total outstanding: ₹{risk['total_outstanding']:.0f}
Total monthly EMI: ₹{risk['total_monthly_emi']:.0f}
% of income going to EMI: {(str(risk['dti_pct']) + '%') if risk['dti_pct'] is not None else 'no income entered'}
Blended (weighted-average) interest rate across all loans: {blended}
Loans that are underwater (EMI doesn't cover interest, balance is growing): {risk['underwater_count']}
Debt-spiral pattern detected (one loan likely covering another): {"yes" if risk['debt_spiral_flag'] else "no"}

Loans in the order they should be tackled:
{loans_block}

{DEBT_GUARDRAIL_BLOCK}

Write a short report (150-220 words) with three parts, no headers, plain paragraphs:
1. One sentence stating plainly where they stand right now. If a debt-spiral pattern was
   detected, name it plainly (one loan is likely covering another) without shaming them.
2. What to do FIRST and why (call out any underwater loan by name explicitly if present —
   this needs to be renegotiated or refinanced, not just paid the minimum).
3. The order to clear the rest, in one or two sentences."""


def call_claude_debt_narration(analysis: dict) -> str:
    prompt = build_debt_narration_prompt(analysis)
    return call_claude(prompt, max_tokens=500)


def call_claude_general_report(situation_text: str, domain: str) -> str:
    prompt = build_general_prompt(situation_text, domain)
    return call_claude(prompt, max_tokens=550)


def _extract_debts_and_income():
    body = request.get_json(force=True, silent=True) or {}
    debts = body.get("debts", [])
    monthly_income = float(body.get("monthly_income", 0) or 0)
    if not isinstance(debts, list) or len(debts) == 0:
        return None, None, ("At least one debt entry is required", 400)
    if monthly_income <= 0:
        return None, None, ("Monthly income must be greater than zero", 400)
    return debts, monthly_income, None


def _extract_situation_text():
    body = request.get_json(force=True, silent=True) or {}
    text = (body.get("situation_text") or "").strip()
    if not text:
        return None, ("Please describe your situation in a few sentences.", 400)
    if len(text) > MAX_SITUATION_TEXT_CHARS:
        text = text[:MAX_SITUATION_TEXT_CHARS]
    return text, None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/preview", methods=["POST"])
def preview():
    """Free tier. Two modes, based on what's in the request body:
      - `debts` + `monthly_income` present -> structured debt verdict
        (deterministic, no AI call).
      - `situation_text` present instead -> local keyword-based domain
        classification only (no AI call -- the AI is never called before
        payment is verified, in either mode)."""
    body = request.get_json(force=True, silent=True) or {}

    if "situation_text" in body and not body.get("debts"):
        text, err = _extract_situation_text()
        if err:
            msg, code = err
            return jsonify({"error": msg}), code
        return jsonify(free_preview_for_text(text))

    debts, monthly_income, err = _extract_debts_and_income()
    if err:
        msg, code = err
        return jsonify({"error": msg}), code
    result = free_preview(debts, monthly_income)
    return jsonify(result)


@app.route("/api/create-order", methods=["POST"])
def create_order():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return jsonify({"error": "Payment gateway not configured"}), 500

    payload = {
        "amount": REPORT_PRICE_PAISE,
        "currency": "INR",
        "receipt": f"fyf_{os.urandom(6).hex()}",
        "payment_capture": 1,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": _razorpay_auth_header(),
    }
    status, resp = _post_json(RAZORPAY_ORDERS_URL, payload, headers)
    if status not in (200, 201):
        return jsonify({"error": "Could not create order", "details": resp}), 502

    return jsonify({
        "order_id": resp["id"],
        "amount": resp["amount"],
        "currency": resp["currency"],
        "key_id": RAZORPAY_KEY_ID,   # public key, safe to expose to frontend checkout.js
    })


@app.route("/api/verify-payment", methods=["POST"])
def verify_payment():
    """
    Expects razorpay_order_id, razorpay_payment_id, razorpay_signature,
    plus EITHER:
      - debts: [...], monthly_income: number   (structured debt case), OR
      - situation_text: string                  (anything else)

    Only after signature verification passes do we run the engine and
    call Claude for the report. Never calls the AI before payment is
    verified, in either mode.
    """
    body = request.get_json(force=True, silent=True) or {}
    order_id = body.get("razorpay_order_id", "")
    payment_id = body.get("razorpay_payment_id", "")
    signature = body.get("razorpay_signature", "")

    if not all([order_id, payment_id, signature]):
        return jsonify({"error": "Missing payment verification fields"}), 400

    if not verify_razorpay_signature(order_id, payment_id, signature):
        return jsonify({"error": "Payment verification failed"}), 400

    if "situation_text" in body and not body.get("debts"):
        text, err = _extract_situation_text()
        if err:
            msg, code = err
            return jsonify({"error": msg}), code
        domain = classify_domain(text)
        report = call_claude_general_report(text, domain)
        return jsonify({
            "mode": "general",
            "domain": domain,
            "report": report or (
                "We couldn't generate your report right now. Your payment is "
                "safe — contact support with your payment ID and we'll send "
                "it directly."
            ),
            # Fixed, non-AI-generated disclaimer -- always present, always
            # worded the same way, regardless of how the model responded on
            # this particular call. See general_triage.py for why this isn't
            # left to the model to write itself.
            "disclaimer": STANDARD_DISCLAIMER if report else None,
        })

    debts, monthly_income, err = _extract_debts_and_income()
    if err:
        msg, code = err
        return jsonify({"error": msg}), code

    analysis = run_full_analysis(debts, monthly_income)
    narration = call_claude_debt_narration(analysis)
    analysis["mode"] = "debt"
    analysis["narration"] = narration or (
        "Here is your full payoff order below. Start with anything marked "
        "UNDERWATER first — that one needs renegotiating, not just paying the minimum."
    )
    return jsonify(analysis)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
