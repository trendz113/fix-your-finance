"""
life_engine.py — "My full money picture" mode for Fix Your Finance
--------------------------------------------------------------------
Unlike debt_engine.py (which only looks at loans against income), this
engine looks at the whole monthly picture: fixed + lifestyle expenses,
credit-card behaviour, insurance gaps, an emergency fund, and investing.

Design mirrors debt_engine.py on purpose so app.py can treat all three
modes the same way:
  - free_preview_life()  -> deterministic, no AI, used pre-payment
  - analyze_life()       -> deterministic, full breakdown, used post-payment
  - build_life_narration_prompt() -> turns the analysis into a Claude prompt
  - LIFE_DISCLAIMER      -> fixed, non-AI-generated, always shown

IMPORTANT: this engine (and the prompt it builds) never recommends a
specific insurer, mutual fund, AMC, or policy, and never promises a
return figure. Those would cross from general education into
individualized advice, which requires a SEBI-registered investment
adviser (for investments) or an IRDAI-licensed advisor/POS agent (for
insurance) -- this product is neither. Keep it that way even if asked
to "just recommend one" in a future iteration.

EXPECTED `profile` SHAPE (all money values in ₹, monthly unless noted):
{
  "monthly_income": 35000,
  "expenses": {
    "rent": 8000, "home_loan_emi": 0, "grocery": 6000, "maid": 1500,
    "school_fees": 2000, "ott_subscriptions": 500, "petrol_travel": 2000,
    "outside_food": 1500, "other": 1000
  },
  "credit_card": {"outstanding": 20000, "pays_minimum_only": true},
  "other_loans_emi": 3000,          # any EMI/gold loan/personal loan not in Fix Your Finance's debt mode
  "dependents_count": 2,
  "has_health_insurance": false, "health_insurance_cover": 0,
  "has_term_insurance": false, "term_insurance_cover": 0,
  "monthly_investment": 0,          # SIP / mutual fund / RD etc.
  "emergency_fund": 5000,
  "age": 34
}
All fields are optional except monthly_income; missing fields default to
0 / false so a half-filled form still produces a sane (if incomplete)
result rather than crashing.
"""

EXPENSE_KEYS = [
    "rent", "home_loan_emi", "grocery", "maid", "school_fees",
    "ott_subscriptions", "petrol_travel", "outside_food", "other",
]

# Typical unsecured credit-card revolving APR in India runs ~36-45%/year
# when only the minimum due is paid. We use a conservative mid-point
# purely to size the drag for the user -- never state this as their
# card's actual rate, since we don't know it.
ASSUMED_CC_MONTHLY_RATE = 0.03  # ~36% p.a.

LIFE_DISCLAIMER = (
    "This report explains the numbers you entered in plain language. It is "
    "general educational information, not individualized investment or "
    "insurance advice under SEBI or IRDAI regulations, and it does not "
    "recommend any specific insurer, policy, mutual fund, or product. For "
    "advice tailored to your situation, consult a SEBI-registered "
    "investment adviser (for investing) or an IRDAI-licensed advisor/POS "
    "agent (for insurance)."
)


def _num(d: dict, key: str, default=0):
    try:
        return float(d.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _compute_core(profile: dict) -> dict:
    income = _num(profile, "monthly_income")
    expenses_in = profile.get("expenses") or {}
    expenses = {k: _num(expenses_in, k) for k in EXPENSE_KEYS}
    total_expenses = sum(expenses.values())

    other_loans_emi = _num(profile, "other_loans_emi")

    cc = profile.get("credit_card") or {}
    cc_outstanding = _num(cc, "outstanding")
    cc_min_only = bool(cc.get("pays_minimum_only", False))
    cc_est_monthly_interest = round(cc_outstanding * ASSUMED_CC_MONTHLY_RATE) if cc_min_only and cc_outstanding > 0 else 0

    total_outflow = total_expenses + other_loans_emi
    surplus = income - total_outflow
    expense_ratio_pct = round((total_outflow / income) * 100, 1) if income > 0 else None

    dependents_count = int(_num(profile, "dependents_count"))
    has_health = bool(profile.get("has_health_insurance", False))
    health_cover = _num(profile, "health_insurance_cover")
    has_term = bool(profile.get("has_term_insurance", False))
    term_cover = _num(profile, "term_insurance_cover")
    annual_income = income * 12

    monthly_investment = _num(profile, "monthly_investment")
    emergency_fund = _num(profile, "emergency_fund")
    months_covered = round(emergency_fund / total_outflow, 1) if total_outflow > 0 else None

    return dict(
        income=income, expenses=expenses, total_expenses=total_expenses,
        other_loans_emi=other_loans_emi, total_outflow=total_outflow,
        surplus=surplus, expense_ratio_pct=expense_ratio_pct,
        cc_outstanding=cc_outstanding, cc_min_only=cc_min_only,
        cc_est_monthly_interest=cc_est_monthly_interest,
        dependents_count=dependents_count,
        has_health=has_health, health_cover=health_cover,
        has_term=has_term, term_cover=term_cover, annual_income=annual_income,
        monthly_investment=monthly_investment, emergency_fund=emergency_fund,
        months_covered=months_covered,
    )


def _flags_and_priorities(c: dict) -> tuple:
    """Returns (flags: list[str], priorities: list[dict]) in the order the
    user should act, deterministically -- no AI involved. `flags` are short
    machine-readable tags; `priorities` are the human-facing action items,
    already in rank order, that both the preview and the Claude prompt
    build on top of."""
    flags = []
    priorities = []
    rank = 1

    if c["surplus"] < 0:
        flags.append("deficit")
        priorities.append({
            "rank": rank,
            "title": "Spending is more than income",
            "detail": f"Outflow is ₹{abs(c['surplus']):.0f} more than income every month — this is the first thing to close, before anything else on this list.",
        })
        rank += 1

    if c["cc_min_only"] and c["cc_outstanding"] > 0:
        flags.append("credit_card_trap")
        priorities.append({
            "rank": rank,
            "title": "Credit card balance is revolving",
            "detail": f"Paying only the minimum due on ₹{c['cc_outstanding']:.0f} outstanding costs roughly ₹{c['cc_est_monthly_interest']:.0f}/month in interest at typical card rates — this is usually the most expensive debt in the house.",
        })
        rank += 1

    if not c["has_health"]:
        flags.append("no_health_insurance")
        priorities.append({
            "rank": rank,
            "title": "No health insurance",
            "detail": "One hospital admission without cover can undo years of savings — this is a gap to close before investing anything new.",
        })
        rank += 1
    elif c["health_cover"] > 0 and c["health_cover"] < 500000:
        flags.append("health_cover_may_be_low")
        priorities.append({
            "rank": rank,
            "title": "Health cover may be on the lower side",
            "detail": f"Current cover is ₹{c['health_cover']:.0f}. A commonly used starting benchmark for a family floater is ₹5-10 lakh, though the right number depends on city and family size.",
        })
        rank += 1

    if c["dependents_count"] > 0 and not c["has_term"]:
        flags.append("no_term_insurance")
        priorities.append({
            "rank": rank,
            "title": "No term life insurance, with dependents in the house",
            "detail": "Term insurance is what protects the family's income if something happens to the earning member — with dependents relying on this income, this is a priority gap, not a someday item.",
        })
        rank += 1
    elif c["has_term"] and c["annual_income"] > 0 and c["term_cover"] < c["annual_income"] * 10:
        flags.append("term_cover_may_be_low")
        priorities.append({
            "rank": rank,
            "title": "Term cover may be on the lower side",
            "detail": f"Current cover is ₹{c['term_cover']:.0f} against annual income of about ₹{c['annual_income']:.0f}. A commonly used rule of thumb is 10-15x annual income.",
        })
        rank += 1

    if c["months_covered"] is not None and c["months_covered"] < 3:
        flags.append("emergency_fund_low")
        priorities.append({
            "rank": rank,
            "title": "Emergency fund covers under 3 months",
            "detail": f"Current savings set aside cover about {c['months_covered']} month(s) of expenses. A commonly used target is 3-6 months, kept somewhere accessible, not invested.",
        })
        rank += 1

    if c["surplus"] > 0 and c["monthly_investment"] <= 0:
        flags.append("not_investing_despite_surplus")
        priorities.append({
            "rank": rank,
            "title": "Monthly surplus isn't being invested",
            "detail": f"About ₹{c['surplus']:.0f}/month is left after expenses but isn't going anywhere specific yet.",
        })
        rank += 1

    if not flags:
        priorities.append({
            "rank": 1,
            "title": "No major gaps found in what was entered",
            "detail": "Income covers expenses with a surplus, insurance and emergency fund basics are in place, and the surplus is being invested. Focus now shifts to reviewing amounts periodically as income or family situation changes.",
        })

    return flags, priorities


def _verdict_level(c: dict, flags: list) -> str:
    if c["surplus"] < 0 or "credit_card_trap" in flags or "no_health_insurance" in flags or "no_term_insurance" in flags:
        return "critical"
    if (c["expense_ratio_pct"] or 0) > 80 or (c["months_covered"] is not None and c["months_covered"] < 1):
        return "high"
    if (c["expense_ratio_pct"] or 0) > 60 or "emergency_fund_low" in flags or "not_investing_despite_surplus" in flags:
        return "caution"
    return "healthy"


def free_preview_life(profile: dict) -> dict:
    """Deterministic, no AI call. Enough to show the person their shape of
    the month and how many gaps exist, without giving away the ranked plan
    (that's behind payment, same pattern as debt mode)."""
    c = _compute_core(profile)
    flags, priorities = _flags_and_priorities(c)
    level = _verdict_level(c, flags)
    return {
        "level": level,
        "income": c["income"],
        "total_outflow": round(c["total_outflow"], 0),
        "surplus": round(c["surplus"], 0),
        "expense_ratio_pct": c["expense_ratio_pct"],
        "gap_count": len(flags),
        "top_gap_title": priorities[0]["title"] if priorities else None,
    }


def analyze_life(profile: dict) -> dict:
    """Full deterministic analysis for the paid report. Returns everything
    needed both to render the ledger client-side and to build the Claude
    narration prompt -- no AI call happens in this function itself."""
    c = _compute_core(profile)
    flags, priorities = _flags_and_priorities(c)
    level = _verdict_level(c, flags)
    return {
        "level": level,
        "income": round(c["income"], 0),
        "expenses": {k: round(v, 0) for k, v in c["expenses"].items()},
        "other_loans_emi": round(c["other_loans_emi"], 0),
        "total_outflow": round(c["total_outflow"], 0),
        "surplus": round(c["surplus"], 0),
        "expense_ratio_pct": c["expense_ratio_pct"],
        "credit_card": {
            "outstanding": round(c["cc_outstanding"], 0),
            "pays_minimum_only": c["cc_min_only"],
            "est_monthly_interest": c["cc_est_monthly_interest"],
        },
        "insurance": {
            "has_health": c["has_health"], "health_cover": round(c["health_cover"], 0),
            "has_term": c["has_term"], "term_cover": round(c["term_cover"], 0),
            "dependents_count": c["dependents_count"],
        },
        "emergency_fund": round(c["emergency_fund"], 0),
        "months_covered": c["months_covered"],
        "monthly_investment": round(c["monthly_investment"], 0),
        "flags": flags,
        "priorities": priorities,
    }


LIFE_GUARDRAIL_BLOCK = """Grounding rules -- follow these exactly:
- Only use the numbers given below. Never name or recommend a specific
  insurer, mutual fund, AMC, policy, or product -- if a category of product
  would help (e.g. "a term plan" or "a liquid fund"), name the category
  only, never a brand or scheme.
- Never state or imply a specific investment return figure.
- No judgment about the person's spending choices, no lecturing.
- Do not tell them to stop any expense entirely (school fees, groceries,
  etc.) -- only point out where the money is going and what the tradeoffs
  are, and let them decide.
- End practically, like a knowledgeable relative giving it straight -- no
  generic "believe in yourself" encouragement line."""


def build_life_narration_prompt(analysis: dict) -> str:
    exp = analysis["expenses"]
    expense_lines = "\n".join(f"- {k.replace('_', ' ').title()}: ₹{v:.0f}" for k, v in exp.items() if v > 0)
    if analysis["other_loans_emi"] > 0:
        expense_lines += f"\n- Other loan EMIs: ₹{analysis['other_loans_emi']:.0f}"

    cc = analysis["credit_card"]
    cc_line = (
        f"Credit card outstanding ₹{cc['outstanding']:.0f}, paying minimum due only "
        f"(est. ₹{cc['est_monthly_interest']:.0f}/month in interest)"
        if cc["pays_minimum_only"] and cc["outstanding"] > 0
        else (f"Credit card outstanding ₹{cc['outstanding']:.0f}, paid off in full each month" if cc["outstanding"] > 0 else "No credit card balance carried")
    )

    ins = analysis["insurance"]
    health_line = f"Health insurance: {'yes, cover ₹' + str(int(ins['health_cover'])) if ins['has_health'] else 'NO health insurance'}"
    term_line = f"Term life insurance: {'yes, cover ₹' + str(int(ins['term_cover'])) if ins['has_term'] else 'NO term insurance'} (dependents: {ins['dependents_count']})"

    priorities_block = "\n".join(f"{p['rank']}. {p['title']} — {p['detail']}" for p in analysis["priorities"])

    return f"""You are explaining a household's full monthly money picture to someone in tier-2/rural
India, in plain, direct, everyday language (no "DTI", "asset allocation" or similar jargon).

Overall verdict: {analysis['level'].upper()}
Monthly income: ₹{analysis['income']:.0f}
Total monthly outflow (expenses + other EMIs): ₹{analysis['total_outflow']:.0f}
Surplus/deficit: ₹{analysis['surplus']:.0f}
Share of income going out each month: {(str(analysis['expense_ratio_pct']) + '%') if analysis['expense_ratio_pct'] is not None else 'n/a'}

Expense breakdown:
{expense_lines}

{cc_line}
{health_line}
{term_line}
Emergency fund: ₹{analysis['emergency_fund']:.0f} (~{analysis['months_covered']} months of expenses covered)
Current monthly investment (SIP/mutual fund/etc.): ₹{analysis['monthly_investment']:.0f}

Priorities already worked out, in order (do not re-rank these, just explain them):
{priorities_block}

{LIFE_GUARDRAIL_BLOCK}

Write a short report (180-260 words), no headers, plain paragraphs:
1. One or two sentences stating plainly where the month stands overall (surplus/deficit, and
   the single biggest thing pulling on the budget).
2. Walk through the priorities above in the given order, in flowing prose (not a bare list),
   explaining briefly *why* each one matters in that order.
3. Close with one practical, concrete next action for this week -- something small and doable,
   not another lecture."""
