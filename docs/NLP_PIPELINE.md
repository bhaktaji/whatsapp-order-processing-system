# OrderBridge 12-Stage NLP & Order Extraction Pipeline

Every inbound message processed by `backend/app/extractor.py` runs through 12 pure, deterministic stages. Each stage returns a `StageOutcome(result, confidence, evidence_span)` dataclass so operators and automated tests can inspect the exact substring and confidence at every step.

---

## Stage-by-Stage Specification

1. **Stage 1 (`normalise_message`):** Unicode `NFKC` normalisation, translation of 9 Indic numeral blocks (`०-९`, `০-৯`, `૦-૯`, `੦-੯`, `௦-௯`, `౦-౯`, `೦-೯`, `൦-൯`) to ASCII `0-9` while preserving `\n`, WhatsApp header stripping, and prompt-injection detection (`PROMPT_INJECTION_SUSPECTED`).
2. **Stage 2 (`detect_script_and_language`):** Identifies Devanagari, Gujarati, Gurmukhi, Bengali, Tamil, Telugu, Kannada, Malayalam, and Romanised Hinglish/regional trade markers.
3. **Stage 3 (`classify_intent`):** Classifies into `ORDER_CREATE`, `MODIFICATION_CANCEL`, `REPEAT_ORDER`, `RATE_ENQUIRY`, `PAYMENT_NOTE`, `DELIVERY_COMPLAINT`, `CHITCHAT_GREETING`, `OPT_OUT`, or `PROMPT_INJECTION`. Only `ORDER_CREATE` proceeds to line extraction; all other intents return `0` lines and route to `INBOX_REVIEW`.
4. **Stage 4 (`aggregate_debounce_window`):** Groups rapid multi-message sequences sent within a 90-second window and detects inline corrections (`"nahi"`, `"iske badle"`).
5. **Stage 5 (`segment_lines`):** Stashes promotional scheme notation (`2+1`, `10+1`) before splitting on newlines, commas, semicolons, and multilingual conjunctions (`aur`, `ane`, `ani`, `mattu`, `mariyu`, `um`), and strips numbered bullet prefixes (`1)`, `2.`).
6. **Stage 6 (`extract_quantity_and_unit`):** Parses exact `Fraction` quantities including compound Indian fractions (`sadhe N`, `paune N`, `sawa N`, `dhai`, `dedh`, `aadha`, `paav`), guards against Hindi imperative `"do"` (`"bhej do"`, `"aane do"`), separates pre-positioned pack weights (`"25kg Basmati 2 katta"`), and flags ambiguous ranges (`"5-6 peti"`).
7. **Stage 7 (`resolve_sku_candidates`):** Evaluates tenant `learned_aliases`, exact code/alias match, and transliteration-canonicalised token overlap + `rapidfuzz` + pack-size disambiguation. Never fabricates `CUSTOM-*` SKUs; returns `UNKNOWN` when below threshold or ambiguous across pack variants.
8. **Stage 8 (`validate_and_convert_unit`):** Validates extracted unit against per-SKU `allowed_units`, flags `INCOMPATIBLE_UNIT` (`"5 litre Parle-G"`), and computes exact integer-paise base and GST amounts (`backend/app/money.py`).
9. **Stage 9 (`llm_disambiguate_candidates`):** Candidate-locked Gemini (`gemini-3.8-flash`) disambiguation invoked only for `[0.50, 0.85)` ambiguity; validated by `validate_candidate_locked_payload`.
10. **Stage 10 (`route_by_confidence`):** Enforces Hard Rule 4: auto-confirms only if `tenant.auto_confirm_enabled == True` (default `False`), every line has `confidence >= 0.85`, zero `UNKNOWN` SKUs exist, and zero risk flags exist.
11. **Stage 11 (`build_clarification_reply`):** Generates a concise Hinglish + English WhatsApp clarification draft for uncertain lines.
12. **Stage 12 (`record_operator_correction` / `rollback_operator_correction`):** Persists tenant-scoped `LearnedAlias` mappings from operator edits with 1-click rollback (`POST /api/aliases/{id}/rollback`).
