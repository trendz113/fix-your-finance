"""
Fix Your Finance — Debt Clarity Engine
----------------------------------------
Pure-python calculation core. No external deps. Import this into app.py.

v2 fixes (this pass):
1. debt_spiral_flag no longer fires off a single loan with zero income.
   The flag means "one loan is likely covering another" -- that requires
   at least two loans to be a coherent claim. A single loan with no
   income is still correctly force-classified CRITICAL, it just isn't
   labeled a "spiral" anymore, so the AI narration downstream doesn't
   describe a pattern that isn't actually there.
2. Added a weighted-average interest rate (`blended_rate_pct`) across all
   loans, weighted by outstanding balance. This is a number people
   commonly want ("what's my overall rate?") and previously had to be
   computed by hand or just wasn't given at all.
3. Underwater loans, when there's more than one, are now ranked by
   monthly rupee bleed (`monthly_interest`) rather than raw outstanding
   balance -- the loan actively costing the most per month is the more
   urgent one to renegotiate first, which isn't always the largest
   balance.
"""

import math
from dataclasses import dataclass, asdict
from typing import List, Optional


VALID_TYPES = {"emi", "gold_loan", "personal_loan", "credit_card", "other"}


@dataclass
class Debt:
    name: str
    debt_type: str          # one of VALID_TYPES
    outstanding_balance: float
    annual_rate_pct: float   # e.g. 14.5 for 14.5%
    emi_amount: float

    def __post_init__(self):
        if self.debt_type not in VALID_TYPES:
            self.debt_type = "other"
        self.outstanding_balance = max(0.0, float(self.outstanding_balance))
        self.annual_rate_pct = max(0.0, float(self.annual_rate_pct))
        self.emi_amount = max(0.0, float(self.emi_amount))


@dataclass
class DebtAnalysis:
    name: str
    debt_type: str
    outstanding_balance: float
    annual_rate_pct: float
    emi_amount: float
    monthly_interest: float
    is_underwater: bool
    months_to_payoff: Optional[int]   # None if underwater (never pays off at current EMI)
    total_interest_if_paid: Optional[float]
    payoff_priority: int = 0          # filled in after sorting


def months_to_payoff(principal: float, annual_rate_pct: float, emi: float) -> Optional[int]:
    """Standard amortization formula. Returns None if the loan is underwater
    (emi doesn't exceed the interest accruing each month) or if there's
    no balance/EMI to compute against."""
    if principal <= 0:
        return 0
    if emi <= 0:
        return None
    r = (annual_rate_pct / 12.0) / 100.0
    if r == 0:
        return math.ceil(principal / emi)
    monthly_interest = principal * r
    if emi <= monthly_interest:
        return None  # underwater — balance never shrinks
    n = -math.log(1 - (monthly_interest / emi)) / math.log(1 + r)
    return math.ceil(n)


def total_interest_paid(principal: float, annual_rate_pct: float, emi: float, n_months: int) -> float:
    if n_months is None or n_months <= 0:
        return 0.0
    total_paid = emi * n_months
    return max(0.0, total_paid - principal)


def analyze_debt(d: Debt) -> DebtAnalysis:
    r = (d.annual_rate_pct / 12.0) / 100.0
    monthly_interest = d.outstanding_balance * r
    n = months_to_payoff(d.outstanding_balance, d.annual_rate_pct, d.emi_amount)
    underwater = n is None and d.outstanding_balance > 0 and d.emi_amount >= 0
    interest_total = total_interest_paid(d.outstanding_balance, d.annual_rate_pct, d.emi_amount, n) if n else None
    return DebtAnalysis(
        name=d.name,
        debt_type=d.debt_type,
        outstanding_balance=d.outstanding_balance,
        annual_rate_pct=d.annual_rate_pct,
        emi_amount=d.emi_amount,
        monthly_interest=round(monthly_interest, 2),
        is_underwater=underwater,
        months_to_payoff=n,
        total_interest_if_paid=round(interest_total, 2) if interest_total is not None else None,
    )


def payoff_order(analyses: List[DebtAnalysis]) -> List[DebtAnalysis]:
    """Underwater loans first (they're actively bleeding — minimum payments
    will never clear them, so they need renegotiation/refinancing before
    anything else), ranked by monthly rupee bleed (highest cost-per-month
    first). Remaining loans ordered avalanche-style: highest interest rate
    first, since that's what's costing the most per rupee outstanding."""
    underwater = [a for a in analyses if a.is_underwater]
    safe = [a for a in analyses if not a.is_underwater]
    underwater.sort(key=lambda a: -a.monthly_interest)
    safe.sort(key=lambda a: -a.annual_rate_pct)
    ordered = underwater + safe
    for i, a in enumerate(ordered, start=1):
        a.payoff_priority = i
    return ordered


def blended_rate(analyses: List[DebtAnalysis]) -> Optional[float]:
    """Weighted-average interest rate across all loans, weighted by
    outstanding balance. Returns None if there's no outstanding balance
    at all (avoids a divide-by-zero)."""
    total_balance = sum(a.outstanding_balance for a in analyses)
    if total_balance <= 0:
        return None
    weighted = sum(a.outstanding_balance * a.annual_rate_pct for a in analyses)
    return round(weighted / total_balance, 2)


def classify_risk(analyses: List[DebtAnalysis], monthly_income: float) -> dict:
    total_outstanding = sum(a.outstanding_balance for a in analyses)
    total_emi = sum(a.emi_amount for a in analyses)
    underwater_count = sum(1 for a in analyses if a.is_underwater)
    loan_count = len(analyses)

    # No income but active EMI is the worst possible position -- there is
    # literally nothing to pay from. Don't let the DTI% math default to 0
    # and mask that as "healthy".
    no_income_with_debt = monthly_income <= 0 and total_emi > 0

    dti_pct = (total_emi / monthly_income * 100.0) if monthly_income > 0 else None

    # A debt spiral means one loan is likely covering another -- that's
    # only a coherent claim if there are at least two loans. A single loan
    # with no income is still CRITICAL (see below), it's just not a
    # "spiral" in the sense the narration describes.
    spiral_flag = (
        (no_income_with_debt and loan_count >= 2)
        or (loan_count >= 3 and (dti_pct or 0) > 50)
        or (underwater_count >= 1 and loan_count >= 3)
    )

    if no_income_with_debt or underwater_count > 0:
        level = "critical"
    elif dti_pct is not None and dti_pct > 60:
        level = "high"
    elif dti_pct is not None and dti_pct > 40:
        level = "caution"
    else:
        level = "healthy"

    return {
        "level": level,
        "dti_pct": round(dti_pct, 1) if dti_pct is not None else None,
        "blended_rate_pct": blended_rate(analyses),
        "no_income_with_debt": no_income_with_debt,
        "total_outstanding": round(total_outstanding, 2),
        "total_monthly_emi": round(total_emi, 2),
        "underwater_count": underwater_count,
        "debt_spiral_flag": spiral_flag,
        "loan_count": loan_count,
    }


def _build_debts(debts_raw: List[dict]) -> List[Debt]:
    return [Debt(
        name=d.get("name", "Unnamed"),
        debt_type=d.get("debt_type", "other"),
        outstanding_balance=d.get("outstanding_balance", 0),
        annual_rate_pct=d.get("annual_rate_pct", 0),
        emi_amount=d.get("emi_amount", 0),
    ) for d in debts_raw]


def run_full_analysis(debts_raw: List[dict], monthly_income: float) -> dict:
    debts = _build_debts(debts_raw)
    analyses = [analyze_debt(d) for d in debts]
    ordered = payoff_order(analyses)
    risk = classify_risk(analyses, monthly_income)

    return {
        "risk": risk,
        "loans_in_payoff_order": [asdict(a) for a in ordered],
    }


def free_preview(debts_raw: List[dict], monthly_income: float) -> dict:
    """Locked-down version for the unpaid tier — verdict + headline numbers
    only, no per-loan payoff order or projections."""
    debts = _build_debts(debts_raw)
    analyses = [analyze_debt(d) for d in debts]
    risk = classify_risk(analyses, monthly_income)
    return {"risk": risk}


if __name__ == "__main__":
    # quick smoke test with an underwater gold loan mixed in
    sample = [
        {"name": "Phone EMI", "debt_type": "emi", "outstanding_balance": 32000, "annual_rate_pct": 16, "emi_amount": 3200},
        {"name": "Gold Loan", "debt_type": "gold_loan", "outstanding_balance": 150000, "annual_rate_pct": 24, "emi_amount": 2900},  # underwater: 150000*0.02=3000 > 2900
        {"name": "Personal Loan", "debt_type": "personal_loan", "outstanding_balance": 80000, "annual_rate_pct": 18, "emi_amount": 4500},
    ]
    import json
    print(json.dumps(run_full_analysis(sample, monthly_income=35000), indent=2))

    print()
    print("--- edge case: single loan, zero income (should be CRITICAL, spiral_flag False) ---")
    print(json.dumps(free_preview(
        [{"name": "Personal Loan", "debt_type": "personal_loan", "outstanding_balance": 50000, "annual_rate_pct": 14, "emi_amount": 2000}],
        monthly_income=0,
    ), indent=2))
