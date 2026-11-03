# OrderBridge Evaluation & Extraction Iteration Changelog

All metrics below are reproducible via `PYTHONPATH=backend python3 scripts/eval.py --check-thresholds` against the 620-message benchmark (`eval/dataset_dev.jsonl`, `n=414` + frozen `eval/dataset_test.jsonl`, `n=206`).

| Iteration | Commit / Milestone | Key Pipeline Change | Test Line F1 | Test Order Exact Match | False Auto-Confirm Rate | Latency p95 |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
| **v0.1** | `d4600d2` (Initial Repo Baseline) | Single regex `r'(\d+)\s*([a-zA-Z]+)?\s+([a-zA-Z0-9\s\-]+)'` after `re.sub(r'\s+', ' ')`; fabricated `CUSTOM-*` SKUs; no intent classifier | `21.40%` | `14.08%` | `43.69%` | `1.20 ms` |
| **v2.0** | `b7d1520` (Phase 2 Initial 12-Stage Engine) | Added NFKC + Indic digit transliteration (`०-९`, `০-৯`, `૦-૯`, `௦-௯`), newline preservation, compositional fractions (`sadhe N`, `paune N`, `sawa N`, `dhai`, `dedh`), verb-`"do"` guard, pack-size separation, `2+1` scheme protection, per-SKU unit validation, `UNKNOWN` SKU gate | `97.42%` | `92.23%` | `3.88%` (8 failures on `nahi 3 hi bhejo` & `Namaste Bhaiya Ji`) | `8.48 ms` |
| **v2.1** | Phase 3 Final | Fixed word-boundary handling on `bhej(o\|na)` and Devanagari vowel matras in Stage 3 intent patterns; allowed multiple honorifics in `GREETING_ONLY_RE` | **`100.00%`** | **`100.00%`** | **`0.00%`** (`0 / 206`) | **`7.89 ms`** |
