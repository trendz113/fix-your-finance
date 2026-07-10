"""
Fix Your Finance — General Issue Triage
------------------------------------------
Handles the money problems that AREN'T a structured "list your loans"
case -- insurance claim disputes, tax notices, retirement/pension
questions, inherited property, EPF/UAN issues, loan-app harassment,
crop insurance, salary structure confusion, guarantor liability, etc.

Two-stage design, matching the existing debt-tool pattern of never
calling the paid AI before payment is verified:

  1. classify_domain() -- free, local, no API call. Keyword-based. Used
     for the free preview teaser ("this looks like an insurance
     question -- here's what the full report will cover").
  2. build_general_prompt() -- used ONLY after payment is verified, to
     ask Claude for real guidance.

IMPORTANT GUARDRAIL: this module explicitly instructs the model not to
invent specific lender/insurer/scheme policies, clause numbers, or legal
citations it wasn't given. For a paid product giving guidance to people
already under financial stress, a confident-sounding hallucinated detail
("your lender allows partial redemption in 25% steps") is a real harm,
not a cosmetic bug. Where a domain genuinely requires case-specific
verification (an insurance policy wording, a specific loan agreement
clause, a scheme's exact rules), the model is told to say so and point
the person to the document or authority that has the real answer,
instead of guessing.
"""

from typing import Optional


# ---------------------------------------------------------------------------
# Stage 1: free, local, keyword-based domain classification
# ---------------------------------------------------------------------------

DOMAIN_KEYWORDS = {
    "debt": ["emi", "loan", "gold loan", "personal loan", "credit card", "lender", "installment", "instalment"],
    "insurance": ["insurance", "policy", "claim", "hospital", "hospitalis", "hospitaliz", "sum insured", "premium", "mediclaim", "cashless"],
    "tax": ["tax", "notice", "income tax", "tds", "ais", "26as", "gst", "itr", "advance tax"],
    "retirement": ["pension", "retirement", "eps", "epf", "provident fund", "pf balance", "uan"],
    "property": ["property", "house", "flat", "land", "inherit", "khata", "registration", "stamp duty", "will", "succession"],
    "fraud": ["fraud", "scam", "otp", "kyc call", "loan app", "harass", "cyber", "phishing"],
    "savings": ["savings", "cash at home", "fixed deposit", "fd ", "invest", "inflation", "mutual fund"],
    "agriculture": ["crop", "farmer", "farming", "kisan", "pmfby", "mandi", "harvest", "msp"],
    "salary": ["ctc", "salary slip", "payslip", "in-hand", "offer letter", "gratuity"],
    "guarantor": ["guarant", "co-sign", "cosign", "surety"],  # "guarant" stems guarantor/guarantee/guaranteed
}

DOMAIN_LABELS = {
    "debt": "Debt & loan repayment",
    "insurance": "Insurance & claims",
    "tax": "Tax & notices",
    "retirement": "Retirement & pension (EPF/EPS)",
    "property": "Property & inheritance",
    "fraud": "Fraud, scams & digital lending safety",
    "savings": "Savings & investment",
    "agriculture": "Agriculture & crop insurance",
    "salary": "Salary structure & offer letters",
    "guarantor": "Guarantor / co-signed loan liability",
    "other": "General financial question",
}

DOMAIN_PREVIEW_COPY = {
    "debt": "This looks like a debt or repayment question. If you have specific loans, use the structured calculator above for an exact payoff order -- it's more precise than a text description for this kind of problem.",
    "insurance": "This looks like an insurance question. The full report will walk through how claims, sub-limits and multi-policy settlements typically work, and what to check in your specific policy documents.",
    "tax": "This looks like a tax question. The full report will break down what a notice or mismatch usually means in plain language, and the concrete next step to take with the department.",
    "retirement": "This looks like a retirement or pension question. The full report will explain how EPF/EPS generally works and what to check in your own UAN passbook.",
    "property": "This looks like a property or inheritance question. The full report will lay out the kinds of options usually available and what typically needs verifying with a lawyer or local authority.",
    "fraud": "This looks like it may involve fraud or an unsafe lending app. The full report will cover how to verify a lender and where to report it -- this one is time-sensitive, so don't wait to act on the basics.",
    "savings": "This looks like a savings or investment question. The full report will walk through the real cost of the current approach and a few lower-risk alternatives to weigh.",
    "agriculture": "This looks like a crop insurance or agricultural finance question. The full report will explain the usual grievance and reassessment process for these schemes.",
    "salary": "This looks like a salary structure question. The full report will break down CTC into what actually reaches your account, and what's worth renegotiating.",
    "guarantor": "This looks like a guarantor liability question. The full report will lay out the realistic options and their trade-offs.",
    "other": "The full report will give plain-language guidance on this and concrete next steps, written for your specific situation.",
}


# Used only to break ties when two domains score equally -- earlier entries
# win. More specific/narrower domains are listed ahead of "debt", since
# "debt"-adjacent words (loan, EMI) show up incidentally in guarantor,
# fraud, and agriculture situations that aren't really about the person's
# own repayment schedule.
TIE_BREAK_PRIORITY = [
    "guarantor", "fraud", "agriculture", "salary", "retirement",
    "property", "tax", "insurance", "savings", "debt",
]


def classify_domain(situation_text: str) -> str:
    text = (situation_text or "").lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        scores[domain] = sum(1 for kw in keywords if kw in text)
    top_score = max(scores.values())
    if top_score == 0:
        return "other"
    tied = [d for d, s in scores.items() if s == top_score]
    for domain in TIE_BREAK_PRIORITY:
        if domain in tied:
            return domain
    return tied[0]


def free_preview_for_text(situation_text: str) -> dict:
    domain = classify_domain(situation_text)
    return {
        "domain": domain,
        "domain_label": DOMAIN_LABELS[domain],
        "preview_note": DOMAIN_PREVIEW_COPY[domain],
    }


# ---------------------------------------------------------------------------
# Standard disclaimer -- appended in app.py AFTER the Claude call returns,
# not written by the model itself. Deliberately not left to the model to
# generate each time: a fixed, reviewed sentence is guaranteed to say
# exactly what it should, every single time, regardless of how well the
# model followed instructions on any given call. This matters most exactly
# when the report sounds most confident -- e.g. citing an actual legal
# principle by name -- since that's when a reader is most likely to treat
# the report as the final word instead of a starting point.
# ---------------------------------------------------------------------------

STANDARD_DISCLAIMER = (
    "This explains how things generally work in situations like yours, based only "
    "on what you described -- it is not a substitute for a lawyer, chartered "
    "accountant, or other licensed professional reviewing your specific documents "
    "before you act, especially before you pay any amount or sign anything."
)


# ---------------------------------------------------------------------------
# Stage 2: paid-tier prompt, only ever called after payment verification
# ---------------------------------------------------------------------------

GUARDRAIL_BLOCK = """Grounding rules -- follow these exactly. There are two different
categories of "specific detail" here, and they are handled differently:

CATEGORY 1 -- universal law and rules that apply to everyone in India, regardless
of which bank/insurer/scheme they're dealing with (e.g. the Indian Contract Act's
rules on guarantors, RBI's Digital Lending Guidelines, IRDAI's general claim-
settlement rules, EPFO's statutory formulas, Income Tax Act provisions on
TDS/advance tax). BE SPECIFIC AND CONFIDENT here -- naming the actual legal
principle (e.g. "a guarantor's liability is co-extensive with the borrower's
under the Indian Contract Act, so the lender is not required to exhaust
remedies against the borrower first") is exactly what makes this report worth
paying for instead of generic hedging. State the mechanism/principle in plain
language with full confidence. Only attach an exact section number or case
name if you are genuinely certain it's correct -- if you're not certain of the
precise citation, state the principle plainly without a citation rather than
risk giving a wrong one. A correct principle with no citation is fine. A
citation is a bonus, not a requirement.

CATEGORY 2 -- anything specific to THIS person's own paperwork or THIS specific
institution's discretionary practice (their exact loan/guarantee wording, this
particular bank's settlement policy, this insurer's internal claim process,
made-up interest rates or amounts they didn't give you). NEVER invent these.
Say plainly that it depends on their specific document, and tell them exactly
what to check or who to ask (e.g. "read the liability clause in your guarantee
deed" -- not "your bank's policy says X").

Also:
- Where the situation genuinely needs a professional for their specific facts
  (a lawyer to read their exact document, a CA for their specific filing),
  say that directly -- but only for verifying THEIR specifics, not as a way to
  avoid stating the general legal principle, which you should state plainly.
- Never recommend borrowing more, a specific investment product, or a specific
  company/app/service by name.
- No judgment, no lecturing, no generic encouragement lines about "believing
  in yourself." Calm, plain, respectful, specific -- like a knowledgeable
  relative giving it to them straight, not a customer-service script hedging
  every sentence."""


def build_general_prompt(situation_text: str, domain: str) -> str:
    domain_label = DOMAIN_LABELS.get(domain, "General financial question")
    return f"""You are helping someone in tier-2/rural India who is dealing with a financial
problem and may be stressed or unsure who to ask. Use plain, direct language,
no jargon left unexplained.

Detected topic area: {domain_label}

Here is what they told you, in their own words:
\"\"\"{situation_text.strip()}\"\"\"

{GUARDRAIL_BLOCK}

Write a short report (180-260 words), no headers, plain paragraphs, covering:
1. One or two sentences reflecting back what's actually going on, in plain
   terms -- so they know they've been understood correctly.
2. What generally applies here and why -- state the actual legal/regulatory
   principle specifically and confidently if it's Category 1 (see above);
   flag clearly anything that depends on their specific documents (Category 2).
3. A short, concrete list of 2-4 next actions, in the order they should be
   done, including who to contact or what to check if that's the right next
   step (e.g. "ask your insurer for X", "check clause Y in your policy",
   "consult a CA before filing", "raise this with your lender in writing").

End with the list of next actions as the very last thing you write -- do not
add your own closing disclaimer or caveat about this not being professional
advice. That line is appended automatically after your report, so don't
duplicate it."""
