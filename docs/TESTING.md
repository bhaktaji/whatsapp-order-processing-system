# Testing, Evaluation Harness & Quality Gates

OrderBridge enforces automated quality gates across unit tests, property-based tests, multi-tenant API integration tests, a 620-message multilingual NLP evaluation benchmark, and a 200-request webhook burst load test.

---

## Running the Automated Test Suite

```bash
# 1. Run all 39 backend unit, property, security, and integration tests with coverage
PYTHONPATH=backend pytest backend/tests -v --cov=backend/app --cov-report=term

# 2. Run Ruff linter and Mypy strict type-checking
ruff check backend/app backend/tests scripts
mypy backend/app

# 3. Run the 620-message NLP evaluation benchmark and enforce release thresholds
PYTHONPATH=backend python3 scripts/eval.py --check-thresholds

# 4. Run the 200-request signed WhatsApp webhook burst & idempotency benchmark
PYTHONPATH=backend python3 scripts/load_test.py

# 5. Run frontend TypeScript check and Vite production bundle build
npm run lint
npm run build
```

---

## Verified Benchmark Results

- **Pytest Suite:** `39 passed in 8.17s`, **`87%` total line coverage** (`extractor.py` `90%`, `models.py` `100%`, `seed.py` `96%`, `failover.py` `90%`, `money.py` `89%`, `accounting_sync.py` `87%`).
- **Evaluation Harness (`n=206` frozen test split):** Line F1 = **`100.00%`**, Order Exact Match = **`100.00%`**, False Auto-Confirm Rate = **`0.00%` (`0 / 206`)**, p95 Latency = **`7.89 ms`**.
- **Webhook Burst Load Test (`200` signed deliveries):** `160` unique orders created, `40` duplicate `wamid` replays suppressed, `0` HTTP errors, `p50 = 30.82 ms`, `p95 = 49.25 ms`.
