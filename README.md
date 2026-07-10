# Fix Your Finance — Debt Clarity Passbook

Flask backend on Railway, static HTML frontend on GitHub Pages, Razorpay
payment gate, **Claude API** for the post-payment plain-language report.

## What changed in this pass

1. **Switched Groq to the Claude API.** New env var `ANTHROPIC_API_KEY`
   (replaces `GROQ_API_KEY`). Model name is configurable via `CLAUDE_MODEL`
   — check `docs.claude.com` for current model names/pricing before
   launch, these change over time.

2. **Fixed the debt-spiral bug on single loans.** `debt_spiral_flag` used
   to fire on a single loan with zero income — but "spiral" specifically
   means one loan is likely covering another, which needs at least two
   loans to be a coherent claim. A single loan with no income is still
   correctly force-classified CRITICAL, it's just not mislabeled a
   "spiral" anymore, so the AI report downstream doesn't describe a
   pattern that isn't actually there.

3. **Added a grounding guardrail to every AI prompt.** Nothing stopped
   the model from confidently stating specific lender/insurer policies
   it wasn't actually given (e.g. "your lender allows partial redemption
   in 25% steps"). Every prompt now explicitly forbids inventing
   institution-specific details and instructs the model to tell the
   person what document or authority to check instead of guessing.
   This matters more than it sounds — it's a paid product giving
   guidance to people already under financial stress.

4. **The tool now handles more than debt.** The original version could
   only ever solve loan-stacking problems — it had no way to help with
   insurance claims, tax notices, retirement/EPF questions, inherited
   property, loan-app fraud, savings, crop insurance, salary structure,
   or guarantor liability, even though those are common problems too.
   There's now a second path: a free-text box where someone describes
   their situation in their own words. A local keyword classifier
   (no AI call, so it's free and instant) tags the general topic area
   for the preview screen. After payment, Claude reads the actual
   situation and writes a grounded, guardrailed report with concrete
   next steps — still never calling the AI before payment is verified,
   same as the debt path.

5. **Added a blended (weighted-average) interest rate** to the debt
   report — a number people commonly ask for that wasn't computed
   before.

6. **Underwater loans, when there's more than one, are now ranked by
   monthly rupee bleed** (`monthly_interest`) rather than raw
   outstanding balance — the loan actively costing the most per month
   is the more urgent one to renegotiate, which isn't always the
   largest balance.

## Files

- `debt_engine.py` — pure-python calculation core for the structured
  loan case (no dependencies). Run directly (`python3 debt_engine.py`)
  to see worked examples, including the spiral-flag fix.
- `general_triage.py` — domain classifier (free, local, keyword-based)
  and the Claude prompt builder for everything that isn't a structured
  debt list (insurance, tax, retirement, property, fraud, savings,
  agriculture, salary, guarantor liability).
- `app.py` — Flask backend. Same three endpoints as before, now
  mode-aware (`debts` vs `situation_text`).
- `requirements.txt`, `Procfile` — Railway deploy files.
- `index.html` — the frontend, now with a toggle between "I can list my
  loans" (original structured form) and "It's something else"
  (free-text box). Deploy as a static file, same as your other
  frontends.

## Backend endpoints

- `GET /api/health` — sanity check.
- `POST /api/preview` — free tier, no AI call either way:
  - Body `{monthly_income, debts:[...]}` → structured verdict +
    headline numbers, no payoff order.
  - Body `{situation_text: "..."}` → local domain classification +
    a one-line preview of what the full report will cover.
- `POST /api/create-order` — creates a Razorpay order (default ₹199,
  configurable).
- `POST /api/verify-payment` — verifies the Razorpay HMAC signature
  server-side, and **only then** runs the full engine (debt mode) or
  calls Claude for a grounded report (general mode). Never calls the
  AI before payment is verified, in either mode.

## Deploy steps

### 1. Backend (Railway)

1. Push `app.py`, `debt_engine.py`, `general_triage.py`,
   `requirements.txt`, `Procfile` to a new GitHub repo (or a folder in
   an existing one).
2. In Railway, create a new project from that repo.
3. Set these environment variables in Railway:
   - `RAZORPAY_KEY_ID`
   - `RAZORPAY_KEY_SECRET`
   - `ANTHROPIC_API_KEY`
   - `CLAUDE_MODEL` — optional, defaults to `claude-sonnet-5`. Verify
     the current model string and per-token pricing before launch;
     `claude-haiku-4-5-20251001` is a cheaper option worth testing if
     per-report cost matters more than report quality at your price
     point.
   - `REPORT_PRICE_PAISE` — e.g. `19900` for ₹199 (Razorpay takes paise)
   - `ALLOWED_ORIGIN` — your frontend's exact origin, e.g.
     `https://yourusername.github.io`
4. Railway will auto-detect the `Procfile` and run gunicorn. Confirm
   with `curl https://your-app.up.railway.app/api/health`.

### 2. Frontend (GitHub Pages)

1. Open `index.html` and replace this line near the top of the
   `<script>` block:
   ```js
   const API_BASE = "https://YOUR-RAILWAY-APP.up.railway.app";
   ```
   with your actual Railway URL.
2. Push `index.html` to your GitHub Pages repo (same pattern as
   HoskoteConstruction / SalaryBit static frontends).
3. Test end-to-end with a real ₹1 test order first if Razorpay test
   mode is available on your account, before going live at ₹199.

## Things worth deciding before you launch

- **Price point**: ₹199 is a placeholder. Worth testing a lower price
  (₹49–₹99) alongside any LinkedIn validation posts — comments will
  tell you more about willingness to pay than guessing will.
- **General-mode report quality is only as good as the guardrail.**
  Read a handful of real general-mode reports yourself before opening
  this to paying customers — for domains like property/succession or
  tax notices, a wrong-but-confident answer is worse than no answer.
  The guardrail instructs Claude not to invent specifics, but you
  should still spot-check output, especially early on.
- **Refund path**: `verify-payment` returns an error message pointing
  the user to contact support with the payment ID if verification
  fails — you'll need an actual support contact (WhatsApp number or
  email) wired into that message before launch.
- **Data sensitivity**: this collects real debt figures and free-text
  descriptions of personal financial situations. Nothing is persisted
  server-side in the current code (each request is stateless), which
  is the safer default — if you later want to save reports for users
  to revisit, that needs explicit consent and secure storage, not an
  afterthought.
- **Domain classifier is a keyword heuristic, not a hard filter.**
  It only decides what preview copy to show and what label to hand
  Claude — Claude still reads the person's full text regardless, so a
  misclassified domain doesn't block a correct report. Worth reviewing
  `DOMAIN_KEYWORDS` in `general_triage.py` periodically as you see real
  submissions, since real phrasing will surface gaps the test cases
  didn't catch.
