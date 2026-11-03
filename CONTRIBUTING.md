# Contributing to OrderBridge

OrderBridge is a proprietary multi-tenant B2B WhatsApp order processing platform. All engineering contributions must pass the automated quality and compliance gates documented below before merging into `release/prod-ready` or `main`.

---

## Mandatory Pre-PR Verification Checklist

1. **Linting & Strict Type Check:**
   ```bash
   ruff check backend/app backend/tests scripts
   mypy backend/app
   npm run lint
   ```
2. **Backend Test Suite & Coverage ($\ge 85\%$):**
   ```bash
   PYTHONPATH=backend pytest backend/tests -v --cov=backend/app
   ```
3. **NLP Evaluation Benchmark Thresholds (`100%` Test F1, `0.00%` False Auto-Confirm):**
   ```bash
   PYTHONPATH=backend python3 scripts/eval.py --check-thresholds
   ```
4. **Webhook Burst & Idempotency Load Test:**
   ```bash
   PYTHONPATH=backend python3 scripts/load_test.py
   ```
5. **Frontend Production Build:**
   ```bash
   npm run build
   ```
