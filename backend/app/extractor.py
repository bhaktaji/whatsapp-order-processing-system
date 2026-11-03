"""OrderBridge 12-Stage Deterministic-First Hybrid NLP Extraction Engine.

Architecture:
  Stage 1:  normalise_message()          -> Unicode NFKC, Indic digits -> ASCII, WhatsApp artefact stripping (preserving \\n)
  Stage 2:  detect_script_and_language() -> Per-segment script & language detection (11 Indian languages + Romanised Hinglish)
  Stage 3:  classify_intent()            -> 9 canonical intents; non-order intents NEVER create confirmed order lines
  Stage 4:  aggregate_debounce_window()  -> Multi-message grouping (90s default), quoted-reply & correction resolution
  Stage 5:  segment_lines()              -> Scheme-safe ("2+1") clause & newline segmentation, bullet-index stripping
  Stage 6:  extract_quantity_and_unit()  -> Rational Fraction qty, compositional Indic fractions, verb-"do" guard, pack-size separation
  Stage 7:  resolve_sku_candidates()     -> Transliteration-aware exact/alias/learned/RapidFuzz/pack-size/buyer-history retrieval
  Stage 8:  validate_and_convert_unit()  -> Per-SKU allowed_units check, rational base-unit conversion, exact integer-paise pricing
  Stage 9:  llm_disambiguate_line()      -> Candidate-locked Gemini fallback with strict schema & budget guard
  Stage 10: compute_confidence_routing() -> Calibrated [0,1] confidence, Green/Amber/Red bands, human-in-the-loop auto_confirm gate
  Stage 11: build_clarification_reply()  -> Polite bilingual/same-script WhatsApp clarification generator for uncertain lines
  Stage 12: record_learned_alias()       -> Tenant-scoped operator alias learning loop with rollback support
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, Generic, TypeVar

from rapidfuzz import fuzz

from app.llm_provider import (
    CandidateSkuOption,
    ExtractionModelProvider,
    GeminiExtractionProvider,
)
from app.money import (
    calculate_line_financials,
    paise_to_rupees_str,
    round_div_half_up,
)

T = TypeVar("T")


class MessageIntent(str, Enum):
    NEW_ORDER = "NEW_ORDER"
    MODIFICATION_CANCEL = "MODIFICATION_CANCEL"
    REPEAT_ORDER = "REPEAT_ORDER"
    RATE_ENQUIRY = "RATE_ENQUIRY"
    AVAILABILITY_ENQUIRY = "AVAILABILITY_ENQUIRY"
    PAYMENT_NOTE = "PAYMENT_NOTE"
    DELIVERY_ENQUIRY = "DELIVERY_ENQUIRY"
    COMPLAINT = "COMPLAINT"
    GREETING_OTHER = "GREETING_OTHER"


class OrderStatus(str, Enum):
    DRAFT = "DRAFT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INBOX_REVIEW = "INBOX_REVIEW"
    CONFIRMED = "CONFIRMED"
    SYNCED = "SYNCED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class StageOutcome(Generic[T]):
    """Standard return contract for every pipeline stage: (result, confidence, evidence_span)."""

    result: T
    confidence: float
    evidence_span: str

    def as_tuple(self) -> tuple[T, float, str]:
        return (self.result, self.confidence, self.evidence_span)


def normalise_sku_dict(raw_sku: dict[str, Any], price_tier: str = "WHOLESALE") -> dict[str, Any]:
    """Normalise SKU dictionary from either config/tenants.json / serialize_tenant() or test fixtures."""
    code = str(raw_sku.get("code") or raw_sku.get("sku_code") or "UNKNOWN")
    name = str(raw_sku.get("name") or code)
    base_unit = str(raw_sku.get("baseUnit") or raw_sku.get("base_unit") or "pcs").lower()
    pack_size = str(raw_sku.get("packSizeLabel") or raw_sku.get("pack_size") or "")
    gst_bps = int(raw_sku.get("taxRateBps") or raw_sku.get("gst_rate_bps") or 1800)

    tier_upper = price_tier.upper()
    if tier_upper == "DISTRIBUTOR" and "distributorPricePaise" in raw_sku:
        price_paise = int(raw_sku["distributorPricePaise"])
    elif tier_upper == "WHOLESALE" and "wholesalePricePaise" in raw_sku:
        price_paise = int(raw_sku["wholesalePricePaise"])
    elif "basePricePaise" in raw_sku:
        price_paise = int(raw_sku["basePricePaise"])
    else:
        price_paise = int(raw_sku.get("base_price_paise", 0))

    units_map: dict[str, dict[str, int]] = {}
    if isinstance(raw_sku.get("allowedUnits"), list):
        for u_entry in raw_sku["allowedUnits"]:
            u_name = str(u_entry.get("unit", "")).strip().lower()
            if u_name:
                units_map[u_name] = {
                    "multiplier_num": int(u_entry.get("multiplierNum", 1)),
                    "multiplier_den": int(u_entry.get("multiplierDen", 1)),
                }
    elif isinstance(raw_sku.get("units"), dict):
        for u_name, u_val in raw_sku["units"].items():
            units_map[str(u_name).strip().lower()] = {
                "multiplier_num": int(u_val.get("multiplier_num", 1)),
                "multiplier_den": int(u_val.get("multiplier_den", 1)),
            }

    if base_unit not in units_map:
        units_map[base_unit] = {"multiplier_num": 1, "multiplier_den": 1}

    return {
        "sku_code": code,
        "name": name,
        "base_unit": base_unit,
        "pack_size": pack_size,
        "base_price_paise": price_paise,
        "gst_rate_bps": gst_bps,
        "aliases": list(raw_sku.get("aliases", [])),
        "units": units_map,
        "hsn_code": str(raw_sku.get("hsnCode") or raw_sku.get("hsn_code") or ""),
    }


# ---------------------------------------------------------------------------
# Stage 1: Unicode & Indic Numeral Normalisation + Prompt Injection Detection
# ---------------------------------------------------------------------------

INDIC_DIGIT_RANGES: list[int] = [
    0x0966,  # Devanagari ०-९ (Hindi, Marathi)
    0x09E6,  # Bengali ০-৯
    0x0A66,  # Gurmukhi ੦-੯ (Punjabi)
    0x0AE6,  # Gujarati ૦-૯
    0x0B66,  # Odia ୦-୯
    0x0BE6,  # Tamil ௦-௯
    0x0C66,  # Telugu ౦-౯
    0x0CE6,  # Kannada ೦-೯
    0x0D66,  # Malayalam ൦-൯
]

INDIC_DIGIT_TRANS: dict[int, int] = {}
for _base in INDIC_DIGIT_RANGES:
    for _offset in range(10):
        INDIC_DIGIT_TRANS[_base + _offset] = ord("0") + _offset

WA_HEADER_RE = re.compile(
    r"^(?:\[\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\]\s*[^:]{1,40}:\s*|"
    r"\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)?\s*-\s*[^:]{1,40}:\s*)",
    re.MULTILINE,
)
WA_FORWARDED_RE = re.compile(
    r"^\s*(?:forwarded(?:\s+many\s+times)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions[^\n]*", re.IGNORECASE),
    re.compile(r"system\s+prompt[^\n]*", re.IGNORECASE),
    re.compile(r"(?:set|override|change)\s+(?:unit_)?price\s*=\s*\S+[^\n]*", re.IGNORECASE),
    re.compile(r"status\s*=\s*['\"]?confirmed[^\n]*", re.IGNORECASE),
    re.compile(r"discount\s*=\s*100\s*%[^\n]*", re.IGNORECASE),
    re.compile(r"auto_confirm\s*=\s*true[^\n]*", re.IGNORECASE),
]


@dataclass(frozen=True)
class NormalisedMessage:
    cleaned_text: str
    quoted_lines: list[str]
    prompt_injection_detected: bool
    injection_evidence: str


def normalise_message(raw_text: str) -> StageOutcome[NormalisedMessage]:
    """Stage 1: Normalise Unicode NFKC, convert Indic digits to ASCII, preserve newlines."""
    if not raw_text:
        return StageOutcome(
            result=NormalisedMessage("", [], False, ""),
            confidence=1.0,
            evidence_span="",
        )

    text = unicodedata.normalize("NFKC", raw_text)
    text = text.translate(INDIC_DIGIT_TRANS)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    injection_detected = False
    injection_span = ""
    for pat in PROMPT_INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            injection_detected = True
            if not injection_span:
                injection_span = m.group(0)
            text = pat.sub("", text)

    text = WA_HEADER_RE.sub("", text)
    text = WA_FORWARDED_RE.sub("", text)

    quoted_lines: list[str] = []
    kept_lines: list[str] = []
    for raw_line in text.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith(">"):
            quoted_lines.append(stripped.lstrip("> ").strip())
        else:
            line_clean = re.sub(r"(?<!\d)\*([^\*\n]+)\*(?!\d)", r"\1", raw_line)
            line_clean = re.sub(r"\b_([^_\n]+)_\b", r"\1", line_clean)
            line_clean = re.sub(r"~([^~\n]+)~", r"\1", line_clean)
            line_clean = re.sub(r"([a-zA-Z])\1{2,}", r"\1", line_clean)
            line_clean = re.sub(r"[ \t]+", " ", line_clean).strip(" .")
            if line_clean:
                kept_lines.append(line_clean)

    cleaned = "\n".join(kept_lines)
    return StageOutcome(
        result=NormalisedMessage(
            cleaned_text=cleaned,
            quoted_lines=quoted_lines,
            prompt_injection_detected=injection_detected,
            injection_evidence=injection_span,
        ),
        confidence=1.0,
        evidence_span=cleaned[:120],
    )


# ---------------------------------------------------------------------------
# Stage 2: Script & Language Detection
# ---------------------------------------------------------------------------

SCRIPT_RANGES: list[tuple[int, int, str, str]] = [
    (0x0900, 0x097F, "Devanagari", "hi"),
    (0x0980, 0x09FF, "Bengali", "bn"),
    (0x0A00, 0x0A7F, "Gurmukhi", "pa"),
    (0x0A80, 0x0AFF, "Gujarati", "gu"),
    (0x0B00, 0x0B7F, "Odia", "or"),
    (0x0B80, 0x0BFF, "Tamil", "ta"),
    (0x0C00, 0x0C7F, "Telugu", "te"),
    (0x0C80, 0x0CFF, "Kannada", "kn"),
    (0x0D00, 0x0D7F, "Malayalam", "ml"),
]

ROMANISED_LANG_MARKERS: dict[str, set[str]] = {
    "hinglish": {
        "bhaiya",
        "bhai",
        "peti",
        "katta",
        "bori",
        "bhej",
        "bhejo",
        "bhejna",
        "likho",
        "chahiye",
        "aur",
        "wala",
        "wali",
        "kitne",
        "kya",
        "paune",
        "sadhe",
        "sade",
        "dhai",
        "dedh",
        "sawa",
        "aadha",
        "paav",
        "pichli",
        "kal",
        "aane",
        "rehne",
        "kar",
        "dena",
        "kilo",
        "nag",
        "darjan",
    },
    "gu_roman": {"moklo", "moklajo", "dabba", "bhav", "su"},
    "mr_roman": {"pathva", "bhau", "pishvi", "kiti"},
    "bn_roman": {"pathan", "pathao", "bosta", "koto"},
    "ta_roman": {"anuppu", "anuppunga", "mootai", "petti"},
    "te_roman": {"pampu", "pampandi", "basta"},
}

MARATHI_DEVANAGARI_MARKERS = {"पाठवा", "भाऊ", "दादा", "पिशवी", "आहे"}


@dataclass(frozen=True)
class LanguageInfo:
    script: str
    language: str
    unknown_language: bool = False


def detect_script_and_language(text: str) -> StageOutcome[LanguageInfo]:
    """Stage 2: Detect Unicode script and Indian language (native or Romanised)."""
    if not text.strip():
        return StageOutcome(LanguageInfo("Latin", "en", False), 0.5, "")

    script_counts: dict[str, int] = {}
    script_lang_map: dict[str, str] = {}
    latin_count = 0
    other_non_ascii_count = 0

    for ch in text:
        cp = ord(ch)
        if ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
            latin_count += 1
            continue
        matched_indic = False
        for start, end, script_name, default_lang in SCRIPT_RANGES:
            if start <= cp <= end:
                script_counts[script_name] = script_counts.get(script_name, 0) + 1
                script_lang_map[script_name] = default_lang
                matched_indic = True
                break
        if not matched_indic and cp > 127 and ch.isalpha():
            other_non_ascii_count += 1

    if script_counts:
        top_script = max(script_counts, key=lambda k: script_counts[k])
        lang = script_lang_map[top_script]
        if top_script == "Devanagari" and any(m in text for m in MARATHI_DEVANAGARI_MARKERS):
            lang = "mr"
        if latin_count > 0:
            return StageOutcome(LanguageInfo(f"{top_script}+Latin", lang, False), 0.96, text[:60])
        return StageOutcome(LanguageInfo(top_script, lang, False), 0.98, text[:60])

    if other_non_ascii_count > latin_count and other_non_ascii_count > 3:
        return StageOutcome(LanguageInfo("UnknownScript", "unknown", True), 0.40, text[:60])

    tokens = set(re.findall(r"[a-zA-Z]+", text.lower()))
    if tokens & ROMANISED_LANG_MARKERS["hinglish"]:
        return StageOutcome(LanguageInfo("Latin", "hinglish", False), 0.95, text[:60])
    for rom_lang, markers in ROMANISED_LANG_MARKERS.items():
        if rom_lang != "hinglish" and (tokens & markers):
            return StageOutcome(LanguageInfo("Latin", rom_lang, False), 0.90, text[:60])

    return StageOutcome(LanguageInfo("Latin", "en", False), 0.90, text[:60])


# ---------------------------------------------------------------------------
# Stage 3: Intent Classification
# ---------------------------------------------------------------------------

RATE_ENQUIRY_PATTERNS = [
    re.compile(
        r"(?:kya\s+rate|rate\s+kya|kitne\s+ka|kitna\s+rate|bhav\s+kya|kya\s+bhav|"
        r"price\s+kya|what\s+is\s+the\s+(?:rate|price)|rate\s+batao|rate\s+bhej(?:o|na)?|"
        r"क्या\s+रेट|रेट\s+क्या|भाव\s+क्या|कितने\s+का|ભાવ\s+શું|दाम\s+किया)",
        re.IGNORECASE,
    ),
]

AVAILABILITY_PATTERNS = [
    re.compile(
        r"(?:stock\s+me\s+hai|available\s+hai|milega\s+kya|maal\s+hai\s+kya|"
        r"in\s+stock|स्टॉक\s+में\s+है|मिलेगा\s+क्या)",
        re.IGNORECASE,
    ),
]

MODIFICATION_CANCEL_PATTERNS = [
    re.compile(
        r"(?:cancel\s+kar|order\s+cancel|cancel\s+my\s+order|mat\s+bhej(?:o|na)?|"
        r"rehne\s+do|nahi\s+chahiye|ruk\s+jao|hold\s+kar|"
        r"nahi\s+\d+(?:\s+\w+)?\s+hi\s+bhej(?:o|na)?|kam\s+kar\s+do|badha\s+do|"
        r"कैंसिल|रद्द|मत\s+भेजो|मत\s+भेजना|रहने\s+दो)",
        re.IGNORECASE,
    ),
]

REPEAT_ORDER_PATTERNS = [
    re.compile(
        r"(?:pichli\s+baar\s+jaisa|pichla\s+order|wahi\s+jo\s+kal|same\s+order\s+(?:repeat|bhej(?:o|na)?)|"
        r"repeat\s+(?:last\s+)?order|last\s+time\s+wala|wahi\s+maal\s+bhej(?:o|na)?|"
        r"पिछली\s+बार\s+जैसा|वही\s+ऑर्डर)",
        re.IGNORECASE,
    ),
]

PAYMENT_NOTE_PATTERNS = [
    re.compile(
        r"(?:upi\s+kar\s+diya|payment\s+(?:kar\s+diya|bhej\s+diya|done)|"
        r"utr\s*[:#\-]?\s*\d{6,}|khata\s+me\s+jama|neft\s+kiya|rtgs\s+kiya|gpay\s+kiya|"
        r"पेमेंट\s+कर\s+दिया|खाते\s+में\s+जमा)",
        re.IGNORECASE,
    ),
]

DELIVERY_ENQUIRY_PATTERNS = [
    re.compile(
        r"(?:kab\s+aayega|maal\s+kab\s+aayega|tempo\s+kahan|dispatch\s+hua\s+kya|"
        r"delivery\s+status|डिलीवरी\s+कब|माल\s+कब\s+आएगा)",
        re.IGNORECASE,
    ),
]

COMPLAINT_PATTERNS = [
    re.compile(
        r"(?:maal\s+kharab|damaged\s+nikla|expired\s+maal|shortage\s+hai|tut\s+gaya|" r"शिकायत|माल\s+खराब)",
        re.IGNORECASE,
    ),
]

GREETING_ONLY_RE = re.compile(
    r"^\s*(?:namaste|namaskar|ram\s+ram|jai\s+shri\s+krishna|hello|hi|hey|ok|okay|"
    r"thik\s+hai|thank\s+you|shukriya|dhanyawad|नमस्ते|राम\s+राम|जय\s+श्री\s+कृष्ण)"
    r"(?:\s+(?:bhaiya|ji|sir|bhai|dada|anna|seth))*[\s!.,]*$",
    re.IGNORECASE,
)


def classify_intent(cleaned_text: str) -> StageOutcome[MessageIntent]:
    """Stage 3: Classify message intent. Only order intents produce draft order lines."""
    text = cleaned_text.strip()
    if not text:
        return StageOutcome(MessageIntent.GREETING_OTHER, 0.99, "")

    if GREETING_ONLY_RE.match(text):
        return StageOutcome(MessageIntent.GREETING_OTHER, 0.98, text)

    for pat in PAYMENT_NOTE_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.PAYMENT_NOTE, 0.96, m.group(0))

    for pat in MODIFICATION_CANCEL_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.MODIFICATION_CANCEL, 0.95, m.group(0))

    for pat in REPEAT_ORDER_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.REPEAT_ORDER, 0.95, m.group(0))

    for pat in RATE_ENQUIRY_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.RATE_ENQUIRY, 0.95, m.group(0))

    for pat in AVAILABILITY_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.AVAILABILITY_ENQUIRY, 0.93, m.group(0))

    for pat in DELIVERY_ENQUIRY_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.DELIVERY_ENQUIRY, 0.94, m.group(0))

    for pat in COMPLAINT_PATTERNS:
        m = pat.search(text)
        if m:
            return StageOutcome(MessageIntent.COMPLAINT, 0.94, m.group(0))

    return StageOutcome(MessageIntent.NEW_ORDER, 0.92, text[:100])


# ---------------------------------------------------------------------------
# Stage 4: Multi-Message Aggregation & Debounce Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundChatBurstItem:
    wamid: str
    sender_phone: str
    text: str
    timestamp_epoch_sec: float
    reply_to_wamid: str | None = None


@dataclass(frozen=True)
class AggregatedMessageWindow:
    combined_text: str
    wamids: list[str]
    has_inline_correction: bool
    correction_notes: list[str]


CORRECTION_INLINE_RE = re.compile(
    r"^\s*(?:sorry|arre|nahi|correction|\*)\s*(.+)$",
    re.IGNORECASE,
)


def aggregate_debounce_window(
    messages: list[InboundChatBurstItem],
    debounce_seconds: float = 90.0,
) -> StageOutcome[AggregatedMessageWindow]:
    """Stage 4: Aggregate consecutive messages from the same buyer within the debounce window."""
    if not messages:
        return StageOutcome(AggregatedMessageWindow("", [], False, []), 1.0, "")

    ordered = sorted(messages, key=lambda m: m.timestamp_epoch_sec)
    grouped: list[InboundChatBurstItem] = [ordered[0]]
    for msg in ordered[1:]:
        if (
            msg.sender_phone == grouped[-1].sender_phone
            and (msg.timestamp_epoch_sec - grouped[-1].timestamp_epoch_sec) <= debounce_seconds
        ):
            grouped.append(msg)

    lines: list[str] = []
    wamids: list[str] = []
    correction_notes: list[str] = []
    has_correction = False

    for msg in grouped:
        wamids.append(msg.wamid)
        txt = msg.text.strip()
        if not txt:
            continue
        if CORRECTION_INLINE_RE.match(txt) or msg.reply_to_wamid:
            has_correction = True
            correction_notes.append(f"Correction in {msg.wamid}: {txt}")
        lines.append(txt)

    combined = "\n".join(lines)
    return StageOutcome(
        result=AggregatedMessageWindow(
            combined_text=combined,
            wamids=wamids,
            has_inline_correction=has_correction,
            correction_notes=correction_notes,
        ),
        confidence=0.95 if not has_correction else 0.78,
        evidence_span=combined[:120],
    )


# ---------------------------------------------------------------------------
# Stage 5: Line Segmentation (Scheme-Safe & Bullet-Index-Safe)
# ---------------------------------------------------------------------------

SCHEME_TOKEN_RE = re.compile(r"\b(\d+)\s*\+\s*(\d+)(?:\s*(?:scheme|free|offer|स्कीम))?\b", re.IGNORECASE)
LEADING_LIST_INDEX_RE = re.compile(r"^\s*(?:\(\d{1,2}\)|\d{1,2}[\.\)\]]|[-•▪▸])\s+")
ORDER_INTRO_RE = re.compile(
    r"^\s*(?:bhaiya|bhai|dada|anna|sir|ji|namaste)?\s*"
    r"(?:mera\s+|aaj\s+ka\s+)?(?:order\s+likh(?:o|na|lo)|order\s+note\s+karo|order)\s*[:\-]?\s*",
    re.IGNORECASE,
)
CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:,|;|\n|\s+aur\s+|\s+and\s+|\s+&\s+|\s+tatha\s+|\s+और\s+|\s+अने\s+|\s+અને\s+|\s+এবং\s+|\s+மற்றும்\s+)\s*",
    re.IGNORECASE,
)


def segment_lines(cleaned_text: str) -> StageOutcome[list[str]]:
    """Stage 5: Split message into atomic order line clauses without breaking '2+1' schemes."""
    if not cleaned_text.strip():
        return StageOutcome([], 1.0, "")

    scheme_map: dict[str, str] = {}

    def _stash_scheme(match: re.Match[str]) -> str:
        key = f"__SCHEME_{len(scheme_map)}__"
        scheme_map[key] = match.group(0)
        return key

    protected = SCHEME_TOKEN_RE.sub(_stash_scheme, cleaned_text)

    raw_lines: list[str] = []
    for physical_line in protected.split("\n"):
        line_str = physical_line.strip()
        if not line_str:
            continue
        line_str = LEADING_LIST_INDEX_RE.sub("", line_str).strip()
        line_str = ORDER_INTRO_RE.sub("", line_str).strip()
        if not line_str:
            continue

        clauses = CLAUSE_SPLIT_RE.split(line_str)
        for clause in clauses:
            c_clean = clause.strip()
            if not c_clean:
                continue
            c_clean = LEADING_LIST_INDEX_RE.sub("", c_clean).strip()
            for placeholder, orig_scheme in scheme_map.items():
                c_clean = c_clean.replace(placeholder, orig_scheme)
            if c_clean:
                raw_lines.append(c_clean)

    return StageOutcome(raw_lines, 0.96, " | ".join(raw_lines[:5]))


# ---------------------------------------------------------------------------
# Stage 6: Quantity & Unit Extraction (Pure Rules, Rational Fraction, Verb Guard)
# ---------------------------------------------------------------------------

UNIT_SYNONYM_MAP: dict[str, str] = {
    "peti": "peti",
    "petis": "peti",
    "petti": "peti",
    "पेटी": "peti",
    "પેટી": "peti",
    "পেটি": "peti",
    "பெட்டி": "peti",
    "carton": "carton",
    "cartons": "carton",
    "ctn": "carton",
    "कार्टन": "carton",
    "box": "box",
    "boxes": "box",
    "bx": "box",
    "बॉक्स": "box",
    "dabba": "dabba",
    "डब्बा": "dabba",
    "katta": "katta",
    "kattas": "katta",
    "katte": "katta",
    "कट्टा": "katta",
    "कट्टे": "katta",
    "bori": "bori",
    "boris": "bori",
    "बोरी": "bori",
    "bosta": "bori",
    "basta": "bori",
    "mootai": "bori",
    "bag": "bag",
    "bags": "bag",
    "बैग": "bag",
    "pishvi": "bag",
    "पिशवी": "bag",
    "quintal": "quintal",
    "क्विंटल": "quintal",
    "ladi": "ladi",
    "ladis": "ladi",
    "लड़ी": "ladi",
    "tin": "tin",
    "tins": "tin",
    "टिन": "tin",
    "पीपा": "tin",
    "pipa": "tin",
    "jar": "jar",
    "jars": "jar",
    "जार": "jar",
    "berni": "jar",
    "crate": "crate",
    "crates": "crate",
    "क्रेट": "crate",
    "carat": "crate",
    "tray": "tray",
    "trays": "tray",
    "ट्रे": "tray",
    "kg": "kg",
    "kgs": "kg",
    "kilo": "kg",
    "kilogram": "kg",
    "किलो": "kg",
    "किग्रा": "kg",
    "कि.ग्रा.": "kg",
    "કિલો": "kg",
    "কেজি": "kg",
    "gm": "gm",
    "gms": "gm",
    "gram": "gm",
    "grams": "gm",
    "g": "gm",
    "ग्राम": "gm",
    "ltr": "ltr",
    "ltrs": "ltr",
    "liter": "ltr",
    "liters": "ltr",
    "litre": "ltr",
    "litres": "ltr",
    "लीटर": "ltr",
    "लिटर": "ltr",
    "ml": "ml",
    "मिली": "ml",
    "pcs": "pcs",
    "pc": "pcs",
    "piece": "pcs",
    "pieces": "pcs",
    "nos": "pcs",
    "nag": "pcs",
    "नग": "pcs",
    "पीस": "pcs",
    "pkt": "pkt",
    "pkts": "pkt",
    "packet": "pkt",
    "packets": "pkt",
    "pack": "pkt",
    "पैकेट": "pkt",
    "pouch": "pouch",
    "pouches": "pouch",
    "पाउच": "pouch",
    "btl": "bottle",
    "bottle": "bottle",
    "bottles": "bottle",
    "बोतल": "bottle",
    "tube": "tube",
    "tubes": "tube",
    "ट्यूब": "tube",
    "roll": "roll",
    "rolls": "roll",
    "रोल": "roll",
    "pair": "pair",
    "pairs": "pair",
    "jodi": "pair",
    "जोड़ी": "pair",
    "bundle": "bundle",
    "bundles": "bundle",
    "gaddi": "bundle",
    "गड्डी": "bundle",
    "बंडल": "bundle",
    "dozen": "dozen",
    "dozens": "dozen",
    "dz": "dozen",
    "darjan": "dozen",
    "दर्जन": "dozen",
}

ALL_UNIT_WORDS_SORTED = sorted(UNIT_SYNONYM_MAP.keys(), key=len, reverse=True)
UNIT_REGEX_GROUP = "|".join(re.escape(u) for u in ALL_UNIT_WORDS_SORTED)

NUMBER_WORDS: dict[str, int] = {
    "one": 1,
    "ek": 1,
    "एक": 1,
    "એક": 1,
    "এক": 1,
    "two": 2,
    "do": 2,
    "दो": 2,
    "બે": 2,
    "দুই": 2,
    "three": 3,
    "teen": 3,
    "तीन": 3,
    "ત્રણ": 3,
    "তিন": 3,
    "four": 4,
    "char": 4,
    "chaar": 4,
    "चार": 4,
    "ચાર": 4,
    "চার": 4,
    "five": 5,
    "panch": 5,
    "paanch": 5,
    "पांच": 5,
    "पाँच": 5,
    "પાંચ": 5,
    "পাঁচ": 5,
    "six": 6,
    "chhe": 6,
    "che": 6,
    "chhah": 6,
    "छह": 6,
    "છ": 6,
    "ছয়": 6,
    "seven": 7,
    "saat": 7,
    "sat": 7,
    "सात": 7,
    "સાત": 7,
    "সাত": 7,
    "eight": 8,
    "aath": 8,
    "ath": 8,
    "आठ": 8,
    "આઠ": 8,
    "আট": 8,
    "nine": 9,
    "nau": 9,
    "नौ": 9,
    "નવ": 9,
    "নয়": 9,
    "ten": 10,
    "das": 10,
    "dus": 10,
    "दस": 10,
    "દસ": 10,
    "দশ": 10,
    "eleven": 11,
    "gyarah": 11,
    "ग्यारह": 11,
    "twelve": 12,
    "barah": 12,
    "बारह": 12,
    "fifteen": 15,
    "pandrah": 15,
    "पंद्रह": 15,
    "twenty": 20,
    "bees": 20,
    "bis": 20,
    "बीस": 20,
    "twentyfive": 25,
    "pachees": 25,
    "pachis": 25,
    "पच्चीस": 25,
    "thirty": 30,
    "tees": 30,
    "तीस": 30,
    "fifty": 50,
    "pachas": 50,
    "पचास": 50,
    "hundred": 100,
    "sau": 100,
    "सौ": 100,
}

HINDI_IMPERATIVE_TAIL_RE = re.compile(
    r"\s*(?:bhej\s+do|bhej\s+dena|bhej\s+dijiye|bhejo|bhejna|aane\s+do|laga\s+do|"
    r"kar\s+do|daal\s+do|de\s+do|nikal\s+do|rakh\s+do|pack\s+kar\s+do|likh\s+do|"
    r"moklo|moklajo|pathan|pathao|pathva|anuppu|"
    r"भेज\s+दो|भेज\s+देना|भेजो|आने\s+दो|लगा\s+दो|कर\s+दो|दे\s+दो|मोक्लो|મોકલો|পাঠান)\s*[\.!]*$",
    re.IGNORECASE,
)

POLITE_PREFIX_RE = re.compile(
    r"^(?:bhaiya|bhai|dada|anna|sir|ji|seth\s*ji|भैया|भाई|दादा|ભાઈ|দাদা)\s+",
    re.IGNORECASE,
)

PACK_SIZE_CUE_RE = re.compile(
    r"\b(?:"
    r"\d+(?:\.\d+)?\s*(?:kg|kilo|किलो|gm|g|ग्राम|ltr|l|लीटर|ml|मिली|mm|inch)\b(?:\s*(?:wala|wali|वाला|वाली))?|"
    r"(?:rs\.?\s*|₹\s*)?\d+\s*(?:rs\.?\s*)?(?:wala|wali|वाला|वाली)\b"
    r")",
    re.IGNORECASE,
)

AMBIGUOUS_RANGE_RE = re.compile(
    r"\b(\d+)\s*(?:\-|~|to|ya|or|se|से|या)\s*(\d+)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ExtractedQuantityUnit:
    qty_fraction: Fraction
    raw_unit: str | None
    canonical_unit: str | None
    remaining_item_text: str
    pack_size_cues: list[str]
    scheme_tag: str | None
    ambiguous_range: str | None
    missing_quantity: bool
    review_flags: list[str]


def _parse_numeric_or_word(token: str) -> Fraction | None:
    t = token.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", t):
        return Fraction(t)
    if t in NUMBER_WORDS:
        return Fraction(NUMBER_WORDS[t], 1)
    return None


def extract_quantity_and_unit(clause: str) -> StageOutcome[ExtractedQuantityUnit]:
    """Stage 6: Deterministic rational quantity and unit parser with Indic fractions & verb-do protection."""
    work = clause.strip()
    review_flags: list[str] = []

    work = POLITE_PREFIX_RE.sub("", work).strip()
    work = HINDI_IMPERATIVE_TAIL_RE.sub("", work).strip()

    scheme_tag: str | None = None
    scheme_match = re.search(
        r"\b(\d+\s*\+\s*\d+)(?:\s*(?:scheme|free|offer|स्कीम))?(?:\s*(?:wala|wali|वाला|वाली))?\b",
        work,
        re.IGNORECASE,
    )
    if scheme_match:
        scheme_tag = re.sub(r"\s+", "", scheme_match.group(1))
        review_flags.append("SCHEME_LINE_REVIEW")
        work = (work[: scheme_match.start()] + " " + work[scheme_match.end() :]).strip()

    ambiguous_range: str | None = None
    range_match = AMBIGUOUS_RANGE_RE.search(work)
    if range_match:
        low_n = int(range_match.group(1))
        high_n = int(range_match.group(2))
        if 0 < low_n < high_n and (high_n - low_n) <= 10:
            ambiguous_range = range_match.group(0)
            review_flags.append("AMBIGUOUS_QUANTITY")
            work = work[: range_match.start()] + str(low_n) + work[range_match.end() :]

    has_packaging_unit = bool(
        re.search(
            r"\b(?:peti|petis|पेटी|પેટી|পেটি|katta|kattas|कट्टा|bori|बोरी|bag|bags|बैग|"
            r"tin|tins|टिन|box|boxes|बॉक्स|crate|क्रेट|tray|ट्रे|carton|jar|bundle|gaddi|"
            r"dozen|darjan|दर्जन|ladi|लड़ी)\b",
            work,
            re.IGNORECASE,
        )
    )

    pack_cues: list[str] = []
    if has_packaging_unit:
        for m in PACK_SIZE_CUE_RE.finditer(work):
            pack_cues.append(m.group(0).strip())
        work_for_qty = PACK_SIZE_CUE_RE.sub(" ", work)
    else:
        wala_re = re.compile(
            r"\b(?:rs\.?\s*|₹\s*)?\d+\s*(?:rs\.?\s*)?(?:wala|wali|वाला|वाली)\b",
            re.IGNORECASE,
        )
        for m in wala_re.finditer(work):
            pack_cues.append(m.group(0).strip())
        work_for_qty = wala_re.sub(" ", work)

    work_for_qty = re.sub(r"\s+", " ", work_for_qty).strip()

    num_token_pat = r"(?:\d+|" + "|".join(re.escape(k) for k in NUMBER_WORDS) + r")"
    colloquial_patterns: list[tuple[re.Pattern[str], str]] = [
        (
            re.compile(
                rf"(?<!\w)(?:sadhe|sade|saadhe|साढ़े|साढे)\s+({num_token_pat})(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)",
                re.IGNORECASE,
            ),
            "sadhe_n",
        ),
        (
            re.compile(
                rf"(?<!\w)(?:paune|pune|पौने|पाउने)\s+({num_token_pat})(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)",
                re.IGNORECASE,
            ),
            "paune_n",
        ),
        (
            re.compile(
                rf"(?<!\w)(?:sawa|sava|सवा)\s+({num_token_pat})(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)",
                re.IGNORECASE,
            ),
            "sawa_n",
        ),
        (
            re.compile(rf"(?<!\w)(?:dhai|adhai|ढाई|अढ़ाई)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "dhai",
        ),
        (
            re.compile(rf"(?<!\w)(?:dedh|डेढ़|डेढ)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "dedh",
        ),
        (
            re.compile(rf"(?<!\w)(?:sawa|sava|सवा)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "sawa_1",
        ),
        (
            re.compile(rf"(?<!\w)(?:paune|pune|पौने)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "paune_1",
        ),
        (
            re.compile(rf"(?<!\w)(?:aadha|adha|आधा|अर्धा)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "aadha",
        ),
        (
            re.compile(rf"(?<!\w)(?:paav|pav|pao|पाव)(?:\s+({UNIT_REGEX_GROUP}))?(?!\w)", re.IGNORECASE),
            "paav",
        ),
    ]

    qty_frac: Fraction | None = None
    raw_unit: str | None = None
    matched_span = ""

    for pat, kind in colloquial_patterns:
        m_col = pat.search(work_for_qty)
        if not m_col:
            continue
        matched_span = m_col.group(0)
        if kind in {"sadhe_n", "paune_n", "sawa_n"}:
            base_n = _parse_numeric_or_word(m_col.group(1))
            if base_n is None:
                continue
            if kind == "sadhe_n":
                qty_frac = base_n + Fraction(1, 2)
            elif kind == "paune_n":
                qty_frac = max(Fraction(1, 4), base_n - Fraction(1, 4))
            else:
                qty_frac = base_n + Fraction(1, 4)
            raw_unit = m_col.group(2)
        elif kind == "dhai":
            qty_frac = Fraction(5, 2)
            raw_unit = m_col.group(1)
        elif kind == "dedh":
            qty_frac = Fraction(3, 2)
            raw_unit = m_col.group(1)
        elif kind == "sawa_1":
            qty_frac = Fraction(5, 4)
            raw_unit = m_col.group(1)
        elif kind == "paune_1":
            qty_frac = Fraction(3, 4)
            raw_unit = m_col.group(1)
        elif kind == "aadha":
            qty_frac = Fraction(1, 2)
            raw_unit = m_col.group(1)
        elif kind == "paav":
            qty_frac = Fraction(1, 4)
            raw_unit = m_col.group(1)

        work_for_qty = (work_for_qty[: m_col.start()] + " " + work_for_qty[m_col.end() :]).strip()
        break

    if qty_frac is None:
        pat_qty_unit = re.compile(
            rf"(?<!\w)(\d+(?:\.\d+)?|{num_token_pat})\s*({UNIT_REGEX_GROUP})(?!\w)",
            re.IGNORECASE,
        )
        m_qu = pat_qty_unit.search(work_for_qty)
        if m_qu:
            parsed_q = _parse_numeric_or_word(m_qu.group(1))
            if parsed_q is not None:
                qty_frac = parsed_q
                raw_unit = m_qu.group(2)
                matched_span = m_qu.group(0)
                work_for_qty = (work_for_qty[: m_qu.start()] + " " + work_for_qty[m_qu.end() :]).strip()

    if qty_frac is None:
        pat_mult = re.compile(r"(?:[x×\*]\s*(\d+(?:\.\d+)?)|-\s*(\d+(?:\.\d+)?)\s*$)", re.IGNORECASE)
        m_mult = pat_mult.search(work_for_qty)
        if m_mult:
            num_str = m_mult.group(1) or m_mult.group(2)
            qty_frac = Fraction(num_str)
            matched_span = m_mult.group(0)
            work_for_qty = (work_for_qty[: m_mult.start()] + " " + work_for_qty[m_mult.end() :]).strip()

    if qty_frac is None:
        pat_num = re.compile(rf"(?<!\w)(\d+(?:\.\d+)?|{num_token_pat})(?!\w)", re.IGNORECASE)
        m_num = pat_num.search(work_for_qty)
        if m_num:
            parsed_q = _parse_numeric_or_word(m_num.group(1))
            if parsed_q is not None:
                qty_frac = parsed_q
                matched_span = m_num.group(0)
                work_for_qty = (work_for_qty[: m_num.start()] + " " + work_for_qty[m_num.end() :]).strip()

    if raw_unit is None:
        pat_unit_only = re.compile(rf"(?<!\w)({UNIT_REGEX_GROUP})(?!\w)", re.IGNORECASE)
        m_u = pat_unit_only.search(work_for_qty)
        if m_u:
            raw_unit = m_u.group(1)
            work_for_qty = (work_for_qty[: m_u.start()] + " " + work_for_qty[m_u.end() :]).strip()

    missing_qty = False
    if qty_frac is None:
        qty_frac = Fraction(1, 1)
        missing_qty = True
        review_flags.append("MISSING_QUANTITY")

    canonical_u = UNIT_SYNONYM_MAP.get(raw_unit.lower()) if raw_unit else None

    item_text = re.sub(r"\s+", " ", f"{work_for_qty} {' '.join(pack_cues)}").strip()
    if not item_text:
        item_text = work

    conf = 0.95
    if ambiguous_range:
        conf = 0.38
    elif missing_qty:
        conf = 0.45
    elif scheme_tag:
        conf = 0.75

    return StageOutcome(
        result=ExtractedQuantityUnit(
            qty_fraction=qty_frac,
            raw_unit=raw_unit,
            canonical_unit=canonical_u,
            remaining_item_text=item_text,
            pack_size_cues=pack_cues,
            scheme_tag=scheme_tag,
            ambiguous_range=ambiguous_range,
            missing_quantity=missing_qty,
            review_flags=review_flags,
        ),
        confidence=conf,
        evidence_span=matched_span or clause[:60],
    )


# ---------------------------------------------------------------------------
# Stage 7: Transliteration-Aware SKU Retrieval
# ---------------------------------------------------------------------------

INDIC_LEXICON_ROMAN_MAP: dict[str, str] = {
    "पारले-जी": "parle g",
    "पारलेजी": "parle g",
    "पारले": "parle",
    "पार्ले": "parle",
    "जी": "g",
    "પાર્લે-જી": "parle g",
    "પારલે જી": "parle g",
    "પાર્લે": "parle",
    "પારલે": "parle",
    "જી": "g",
    "পার্লে-জি": "parle g",
    "পারলে জি": "parle g",
    "পার্লে": "parle",
    "পারলে": "parle",
    "জি": "g",
    "பார்லே ஜி": "parle g",
    "பார்லே": "parle",
    "పార్లే జి": "parle g",
    "मारी": "marie",
    "गोल्ड": "gold",
    "गुड": "good",
    "डे": "day",
    "बिस्किट": "biscuit",
    "बासमती चावल": "basmati rice",
    "बासमती": "basmati",
    "चावल": "rice",
    "બાસમતી ચોખા": "basmati rice",
    "બાસમતી": "basmati",
    "বাসমতী চাল": "basmati rice",
    "বাসমতি": "basmati",
    "आशीर्वाद आटा": "aashirvaad atta",
    "आशीर्वाद": "aashirvaad",
    "आटा": "atta",
    "चक्की": "chakki",
    "फॉर्च्यून तेल": "fortune oil",
    "फॉर्च्यून": "fortune",
    "तेल": "oil",
    "सोया": "soya",
    "टाटा": "tata",
    "नमक": "salt",
    "संपन्न": "sampann",
    "दाल": "dal",
    "तूर": "toor",
    "चीनी": "sugar",
    "शक्कर": "sugar",
    "साखर": "sugar",
    "চিনি": "sugar",
    "ખાંડ": "sugar",
    "मैगी मसाला": "maggi masala",
    "मैगी": "maggi",
    "सर्फ एक्सेल": "surf excel",
    "सर्फ": "surf",
    "अमूल ताज़ा": "amul taaza",
    "अमूल दूध": "amul milk",
    "अमूल": "amul",
    "दूध": "milk",
    "हल्दीराम": "haldiram",
    "भुजिया": "bhujia",
    "जीरा": "jeera",
    "જીરું": "jeera",
    "जीरे": "jeera",
    "इलायची": "elaichi",
    "ಏಲಕ್ಕಿ": "elaichi",
    "ஏலக்காய்": "elaichi",
    "काजू": "kaju",
    "কাজু": "kaju",
    "કાજુ": "kaju",
}


def canonicalise_indian_text(text: str) -> str:
    """Normalise Indic script words and Hinglish phonetic variants into canonical Latin tokens."""
    s = text.lower().strip()
    for indic_word, roman_eq in sorted(
        INDIC_LEXICON_ROMAN_MAP.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        s = s.replace(indic_word.lower(), f" {roman_eq} ")

    s = re.sub(r"[\-_/\.,\(\)\[\]]+", " ", s)
    s = s.replace("aa", "a").replace("ee", "i").replace("oo", "u")
    s = s.replace("ph", "f").replace("w", "v").replace("sh", "s")
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass(frozen=True)
class SkuCandidateMatch:
    sku_code: str
    name: str
    score: float
    match_reason: str
    sku_record: dict[str, Any]


@dataclass(frozen=True)
class SkuRetrievalOutcome:
    best_candidate: SkuCandidateMatch | None
    top_candidates: list[SkuCandidateMatch]
    ambiguous_tie: bool


def resolve_sku_candidates(
    item_text: str,
    pack_size_cues: list[str],
    catalogue: list[dict[str, Any]],
    learned_aliases: dict[str, str] | None = None,
    buyer_history_sku_codes: set[str] | None = None,
    price_tier: str = "WHOLESALE",
    top_k: int = 3,
) -> StageOutcome[SkuRetrievalOutcome]:
    """Stage 7: Retrieve candidate SKUs from the tenant catalogue. NEVER invents SKU codes."""
    query_raw = item_text.strip().lower()
    query_canon = canonicalise_indian_text(item_text)
    query_nospace = query_canon.replace(" ", "")

    if not query_raw or not catalogue:
        return StageOutcome(SkuRetrievalOutcome(None, [], False), 0.0, item_text)

    learned = learned_aliases or {}
    buyer_prior = buyer_history_sku_codes or set()
    learned_sku_code = learned.get(query_raw) or learned.get(query_canon)

    scored: list[SkuCandidateMatch] = []
    for raw_sku in catalogue:
        sku = normalise_sku_dict(raw_sku, price_tier=price_tier)
        code = sku["sku_code"]
        name = sku["name"]
        pack_size = sku["pack_size"].lower()
        aliases: list[str] = sku["aliases"]

        best_score = 0.0
        reason = "fuzzy"

        if learned_sku_code == code:
            best_score = 0.98
            reason = "learned_alias"

        all_targets = [code.lower(), name.lower(), *[a.lower() for a in aliases]]
        for tgt in all_targets:
            tgt_canon = canonicalise_indian_text(tgt)
            tgt_nospace = tgt_canon.replace(" ", "")
            if (
                query_raw == tgt
                or query_canon == tgt_canon
                or (len(query_nospace) >= 4 and query_nospace == tgt_nospace)
            ):
                if 0.97 > best_score:
                    best_score = 0.97
                    reason = f"exact_alias:{tgt}"
            elif len(tgt_canon) >= 4 and (tgt_canon in query_canon or tgt_nospace in query_nospace):
                if 0.91 > best_score:
                    best_score = 0.91
                    reason = f"substring_alias:{tgt}"
            else:
                r1 = fuzz.token_set_ratio(query_canon, tgt_canon) / 100.0
                r2 = fuzz.ratio(query_nospace, tgt_nospace) / 100.0
                fuzzy_val = max(r1 * 0.92, r2 * 0.92)
                if fuzzy_val > best_score:
                    best_score = fuzzy_val
                    reason = f"rapidfuzz:{tgt}"

        if pack_size_cues and best_score >= 0.55:
            cue_joined = " ".join(pack_size_cues).lower().replace(" ", "")
            sku_blob = f"{code} {name} {pack_size} {' '.join(aliases)}".lower().replace(" ", "")
            if any(c.lower().replace(" ", "") in sku_blob for c in pack_size_cues) or cue_joined in sku_blob:
                best_score = min(0.99, best_score + 0.07)
                reason += "+pack_match"
            elif pack_size and not any(tok in sku_blob for tok in cue_joined.split()):
                best_score = max(0.0, best_score - 0.12)
                reason += "-pack_mismatch"

        if code in buyer_prior and best_score >= 0.65:
            best_score = min(0.99, best_score + 0.04)
            reason += "+buyer_prior"

        if best_score >= 0.45:
            scored.append(
                SkuCandidateMatch(
                    sku_code=code,
                    name=name,
                    score=round(best_score, 4),
                    match_reason=reason,
                    sku_record=sku,
                )
            )

    scored.sort(key=lambda c: c.score, reverse=True)
    top_candidates = scored[:top_k]

    if not top_candidates or top_candidates[0].score < 0.65:
        return StageOutcome(
            result=SkuRetrievalOutcome(
                best_candidate=None, top_candidates=top_candidates, ambiguous_tie=False
            ),
            confidence=0.0,
            evidence_span=item_text,
        )

    best = top_candidates[0]
    ambiguous_tie = (
        len(top_candidates) >= 2 and (best.score - top_candidates[1].score) < 0.05 and best.score < 0.95
    )

    return StageOutcome(
        result=SkuRetrievalOutcome(
            best_candidate=best,
            top_candidates=top_candidates,
            ambiguous_tie=ambiguous_tie,
        ),
        confidence=best.score if not ambiguous_tie else min(0.72, best.score),
        evidence_span=f"{item_text} -> {best.sku_code} ({best.score:.2f})",
    )


# ---------------------------------------------------------------------------
# Stage 8: Per-SKU Unit Validation & Integer-Paise Pricing Conversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedUnitPricing:
    resolved_unit: str
    unit_compatible: bool
    base_unit_qty: Fraction
    unit_price_paise: int
    line_subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_total_paise: int
    review_flags: list[str]
    candidate_units: list[str]


def validate_and_convert_unit(
    *,
    qty_fraction: Fraction,
    canonical_unit: str | None,
    sku_record: dict[str, Any] | None,
    is_inter_state: bool = False,
) -> StageOutcome[ValidatedUnitPricing]:
    """Stage 8: Validate unit against SKU's allowed_units and calculate integer-paise totals."""
    if sku_record is None:
        return StageOutcome(
            result=ValidatedUnitPricing(
                resolved_unit=canonical_unit or "pcs",
                unit_compatible=False,
                base_unit_qty=qty_fraction,
                unit_price_paise=0,
                line_subtotal_paise=0,
                cgst_paise=0,
                sgst_paise=0,
                igst_paise=0,
                line_total_paise=0,
                review_flags=["UNKNOWN_SKU"],
                candidate_units=[],
            ),
            confidence=0.0,
            evidence_span="UNKNOWN_SKU",
        )

    norm_sku = normalise_sku_dict(sku_record)
    base_unit = norm_sku["base_unit"]
    base_price_paise = norm_sku["base_price_paise"]
    gst_rate_bps = norm_sku["gst_rate_bps"]
    allowed_units: dict[str, dict[str, int]] = norm_sku["units"]

    candidate_unit_list = list(allowed_units.keys())
    flags: list[str] = []

    if canonical_unit is None:
        resolved_unit = base_unit
        compatible = True
        mult_info = allowed_units[base_unit]
    elif canonical_unit in allowed_units:
        resolved_unit = canonical_unit
        compatible = True
        mult_info = allowed_units[canonical_unit]
    else:
        resolved_unit = canonical_unit
        compatible = False
        flags.append("INCOMPATIBLE_UNIT")
        mult_info = {"multiplier_num": 1, "multiplier_den": 1}

    mult_num = int(mult_info.get("multiplier_num", 1))
    mult_den = int(mult_info.get("multiplier_den", 1))
    base_qty = qty_fraction * Fraction(mult_num, mult_den)

    if compatible:
        unit_p = round_div_half_up(base_price_paise * mult_num, mult_den)
        tax_breakdown = calculate_line_financials(
            qty_num=qty_fraction.numerator,
            qty_den=qty_fraction.denominator,
            unit_price_paise=unit_p,
            tax_rate_bps=gst_rate_bps,
            seller_state_code="27",
            buyer_state_code="19" if is_inter_state else "27",
            tax_regime="GST_INDIA",
        )
        subtotal_p = tax_breakdown.line_subtotal_paise
        cgst_p = tax_breakdown.cgst_paise
        sgst_p = tax_breakdown.sgst_paise
        igst_p = tax_breakdown.igst_paise
        total_p = tax_breakdown.line_total_paise
    else:
        unit_p = 0
        subtotal_p = 0
        cgst_p = 0
        sgst_p = 0
        igst_p = 0
        total_p = 0

    return StageOutcome(
        result=ValidatedUnitPricing(
            resolved_unit=resolved_unit,
            unit_compatible=compatible,
            base_unit_qty=base_qty,
            unit_price_paise=unit_p,
            line_subtotal_paise=subtotal_p,
            cgst_paise=cgst_p,
            sgst_paise=sgst_p,
            igst_paise=igst_p,
            line_total_paise=total_p,
            review_flags=flags,
            candidate_units=candidate_unit_list,
        ),
        confidence=0.98 if compatible else 0.25,
        evidence_span=f"unit={resolved_unit} compatible={compatible} allowed={candidate_unit_list}",
    )


# ---------------------------------------------------------------------------
# Stage 9, 10, 11, 12 & End-to-End Pipeline Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class ExtractedLineItem:
    line_index: int
    raw_segment: str
    sku_code: str
    sku_name: str
    qty: float
    qty_num: int
    qty_den: int
    unit: str
    base_unit_qty: float
    unit_price_paise: int
    line_subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    line_total_paise: int
    confidence: float
    confidence_band: str  # "GREEN" | "AMBER" | "RED"
    requires_review: bool
    review_reasons: list[str]
    candidates: list[dict[str, Any]]
    evidence_span: str


@dataclass
class ExtractionPipelineResult:
    tenant_id: str
    raw_message: str
    normalised_text: str
    detected_script: str
    detected_language: str
    intent: str
    order_status: str
    auto_confirmed: bool
    overall_confidence: float
    requires_human_review: bool
    review_reasons: list[str]
    lines: list[ExtractedLineItem]
    subtotal_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    total_paise: int
    clarification_reply: str | None
    stage_trace: dict[str, Any] = field(default_factory=dict)


def build_clarification_reply(
    lines: list[ExtractedLineItem],
    language: str,
    script: str,
) -> StageOutcome[str | None]:
    """Stage 11: Generate a concise, polite WhatsApp clarification message for uncertain lines only."""
    uncertain = [ln for ln in lines if ln.requires_review]
    if not uncertain:
        return StageOutcome(None, 1.0, "")

    prompts: list[str] = []
    for ln in uncertain:
        if "UNKNOWN_SKU" in ln.review_reasons:
            prompts.append(f"'{ln.raw_segment}' (item confirm karein)")
        elif "AMBIGUOUS_QUANTITY" in ln.review_reasons:
            prompts.append(f"'{ln.sku_name}' ki exact quantity ({ln.raw_segment})")
        elif "INCOMPATIBLE_UNIT" in ln.review_reasons:
            allowed = ", ".join(ln.candidates[0].get("allowed_units", [])) if ln.candidates else "peti/pcs"
            prompts.append(f"'{ln.sku_name}' ka unit ({allowed} me se confirm karein)")
        elif "AMBIGUOUS_SKU_TIE" in ln.review_reasons and len(ln.candidates) >= 2:
            c1 = ln.candidates[0]["name"]
            c2 = ln.candidates[1]["name"]
            prompts.append(f"'{ln.raw_segment}' ({c1} ya {c2}?)")
        else:
            prompts.append(f"'{ln.raw_segment}'")

    joined = "; ".join(prompts)
    if "Devanagari" in script or language in {"hi", "mr"}:
        reply = f"नमस्ते! कृपया अपने ऑर्डर में इन आइटमों की पुष्टि करें: {joined}।"
    else:
        reply = f"Namaste! Kripya apne order me confirm karein: {joined}."

    return StageOutcome(reply, 0.95, reply)


class HybridExtractor:
    """12-Stage Deterministic-First Hybrid Order Extractor."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        llm_provider: ExtractionModelProvider | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path)
            if config_path
            else Path(__file__).resolve().parents[2] / "config" / "tenants.json"
        )
        self.llm_provider: ExtractionModelProvider = llm_provider or GeminiExtractionProvider()
        self._tenants_cache: dict[str, dict[str, Any]] = {}
        self._learned_aliases: dict[str, dict[str, str]] = {}
        self._load_catalogue_from_json()

    def _load_catalogue_from_json(self) -> None:
        if not self.config_path.exists():
            return
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "tenants" in data and isinstance(data["tenants"], list):
            for t in data["tenants"]:
                tid = str(t.get("tenant_id") or t.get("slug") or t.get("id"))
                self._tenants_cache[tid] = t
                self._learned_aliases.setdefault(tid, {})
        elif isinstance(data, dict):
            for tid, t in data.items():
                if isinstance(t, dict):
                    self._tenants_cache[tid] = t
                    self._learned_aliases.setdefault(tid, {})

    def get_tenant_catalogue(self, tenant_id: str) -> list[dict[str, Any]]:
        t = self._tenants_cache.get(tenant_id, {})
        return list(t.get("skus") or t.get("catalogue") or [])

    def record_operator_correction(
        self,
        *,
        tenant_id: str,
        alias_phrase: str,
        sku_code: str,
    ) -> StageOutcome[dict[str, str]]:
        """Stage 12: Record an operator's SKU resolution into the tenant's learned aliases."""
        norm_phrase = canonicalise_indian_text(alias_phrase)
        self._learned_aliases.setdefault(tenant_id, {})[norm_phrase] = sku_code
        self._learned_aliases[tenant_id][alias_phrase.strip().lower()] = sku_code
        return StageOutcome(
            result={"tenant_id": tenant_id, "alias_phrase": norm_phrase, "sku_code": sku_code},
            confidence=1.0,
            evidence_span=f"{norm_phrase} -> {sku_code}",
        )

    def rollback_operator_correction(self, *, tenant_id: str, alias_phrase: str) -> bool:
        """Remove a learned alias if an operator rolls back an accidental mapping."""
        norm_phrase = canonicalise_indian_text(alias_phrase)
        tenant_map = self._learned_aliases.get(tenant_id, {})
        removed = False
        for key in (norm_phrase, alias_phrase.strip().lower()):
            if key in tenant_map:
                del tenant_map[key]
                removed = True
        return removed

    def extract_order(
        self,
        raw_message: str,
        tenant_id: str = "patel_fmcg",
        *,
        catalogue_override: list[dict[str, Any]] | None = None,
        price_tier: str = "WHOLESALE",
        auto_confirm_enabled: bool = False,
        is_inter_state: bool = False,
        buyer_history_sku_codes: set[str] | None = None,
        monthly_llm_spent_paise: int = 0,
        monthly_llm_cap_paise: int = 500_00,
    ) -> ExtractionPipelineResult:
        """Run all 12 extraction stages on an inbound WhatsApp message."""
        catalogue = (
            catalogue_override if catalogue_override is not None else self.get_tenant_catalogue(tenant_id)
        )
        learned_map = self._learned_aliases.get(tenant_id, {})

        s1 = normalise_message(raw_message)
        norm_msg = s1.result

        s2 = detect_script_and_language(norm_msg.cleaned_text or raw_message)
        lang_info = s2.result

        s3 = classify_intent(norm_msg.cleaned_text)
        intent = s3.result

        order_review_reasons: list[str] = []
        if norm_msg.prompt_injection_detected:
            order_review_reasons.append("PROMPT_INJECTION_SUSPECTED")
        if lang_info.unknown_language:
            order_review_reasons.append("UNKNOWN_LANGUAGE_REVIEW")

        if intent != MessageIntent.NEW_ORDER:
            order_review_reasons.append(f"NON_ORDER_INTENT:{intent.value}")
            return ExtractionPipelineResult(
                tenant_id=tenant_id,
                raw_message=raw_message,
                normalised_text=norm_msg.cleaned_text,
                detected_script=lang_info.script,
                detected_language=lang_info.language,
                intent=intent.value,
                order_status=OrderStatus.INBOX_REVIEW.value,
                auto_confirmed=False,
                overall_confidence=s3.confidence,
                requires_human_review=True,
                review_reasons=order_review_reasons,
                lines=[],
                subtotal_paise=0,
                cgst_paise=0,
                sgst_paise=0,
                igst_paise=0,
                total_paise=0,
                clarification_reply=None,
                stage_trace={
                    "stage1_normalise": s1.evidence_span,
                    "stage2_language": f"{lang_info.script}/{lang_info.language}",
                    "stage3_intent": f"{intent.value} ({s3.evidence_span})",
                },
            )

        s5 = segment_lines(norm_msg.cleaned_text)
        segments = s5.result

        extracted_lines: list[ExtractedLineItem] = []
        for idx, seg in enumerate(segments):
            s6 = extract_quantity_and_unit(seg)
            qu = s6.result

            s7 = resolve_sku_candidates(
                item_text=qu.remaining_item_text,
                pack_size_cues=qu.pack_size_cues,
                catalogue=catalogue,
                learned_aliases=learned_map,
                buyer_history_sku_codes=buyer_history_sku_codes,
                price_tier=price_tier,
            )
            ret = s7.result
            best_cand = ret.best_candidate

            if (best_cand is None or ret.ambiguous_tie or best_cand.score < 0.85) and ret.top_candidates:
                llm_options = [
                    CandidateSkuOption(
                        sku_code=c.sku_code,
                        name=c.name,
                        pack_size=str(c.sku_record.get("pack_size", "")),
                        allowed_units=list(c.sku_record.get("units", {}).keys()),
                    )
                    for c in ret.top_candidates
                ]
                llm_res = self.llm_provider.disambiguate_line(
                    tenant_id=tenant_id,
                    raw_line=seg,
                    candidates=llm_options,
                    monthly_spent_paise=monthly_llm_spent_paise,
                    monthly_cap_paise=monthly_llm_cap_paise,
                )
                if llm_res.sku_id != "UNKNOWN":
                    for cand in ret.top_candidates:
                        if cand.sku_code == llm_res.sku_id:
                            best_cand = cand
                            break

            line_reasons = list(qu.review_flags)
            if norm_msg.prompt_injection_detected:
                line_reasons.append("PROMPT_INJECTION_SUSPECTED")
            if ret.ambiguous_tie:
                line_reasons.append("AMBIGUOUS_SKU_TIE")

            sku_rec = best_cand.sku_record if best_cand is not None else None

            s8 = validate_and_convert_unit(
                qty_fraction=qu.qty_fraction,
                canonical_unit=qu.canonical_unit,
                sku_record=sku_rec,
                is_inter_state=is_inter_state,
            )
            val_unit = s8.result
            for flg in val_unit.review_flags:
                if flg not in line_reasons:
                    line_reasons.append(flg)

            if best_cand is None:
                line_conf = 0.0
                sku_code_out = "UNKNOWN"
                sku_name_out = "Unmatched Item (Requires Review)"
            else:
                sku_code_out = best_cand.sku_code
                sku_name_out = best_cand.name
                line_conf = min(s6.confidence, s7.confidence, s8.confidence)
                if line_reasons:
                    line_conf = min(line_conf, 0.72)
                if "INCOMPATIBLE_UNIT" in line_reasons or "AMBIGUOUS_QUANTITY" in line_reasons:
                    line_conf = min(line_conf, 0.35)
                if "PROMPT_INJECTION_SUSPECTED" in line_reasons:
                    line_conf = min(line_conf, 0.30)

            line_conf = round(max(0.0, min(1.0, line_conf)), 4)
            if line_conf >= 0.85 and not line_reasons:
                band = "GREEN"
                req_review = False
            elif (
                line_conf >= 0.55
                and "UNKNOWN_SKU" not in line_reasons
                and "INCOMPATIBLE_UNIT" not in line_reasons
            ):
                band = "AMBER"
                req_review = True
            else:
                band = "RED"
                req_review = True

            cand_dicts = [
                {
                    "sku_code": c.sku_code,
                    "name": c.name,
                    "score": c.score,
                    "match_reason": c.match_reason,
                    "allowed_units": list(c.sku_record.get("units", {}).keys()),
                }
                for c in ret.top_candidates
            ]

            extracted_lines.append(
                ExtractedLineItem(
                    line_index=idx,
                    raw_segment=seg,
                    sku_code=sku_code_out,
                    sku_name=sku_name_out,
                    qty=float(qu.qty_fraction),
                    qty_num=qu.qty_fraction.numerator,
                    qty_den=qu.qty_fraction.denominator,
                    unit=val_unit.resolved_unit,
                    base_unit_qty=float(val_unit.base_unit_qty),
                    unit_price_paise=val_unit.unit_price_paise,
                    line_subtotal_paise=val_unit.line_subtotal_paise,
                    cgst_paise=val_unit.cgst_paise,
                    sgst_paise=val_unit.sgst_paise,
                    igst_paise=val_unit.igst_paise,
                    line_total_paise=val_unit.line_total_paise,
                    confidence=line_conf,
                    confidence_band=band,
                    requires_review=req_review,
                    review_reasons=line_reasons,
                    candidates=cand_dicts,
                    evidence_span=s7.evidence_span,
                )
            )

        if not extracted_lines:
            order_review_reasons.append("NO_LINES_EXTRACTED")

        any_line_review = (
            any(ln.requires_review for ln in extracted_lines)
            or bool(order_review_reasons)
            or not extracted_lines
        )
        for ln in extracted_lines:
            for r in ln.review_reasons:
                if r not in order_review_reasons:
                    order_review_reasons.append(r)

        overall_conf = round(min(ln.confidence for ln in extracted_lines), 4) if extracted_lines else 0.0

        can_auto_confirm = (
            bool(auto_confirm_enabled)
            and bool(extracted_lines)
            and not any_line_review
            and overall_conf >= 0.85
        )

        if can_auto_confirm:
            status_out = OrderStatus.CONFIRMED.value
        elif any_line_review:
            status_out = OrderStatus.NEEDS_REVIEW.value
        else:
            status_out = OrderStatus.DRAFT.value

        s11 = build_clarification_reply(extracted_lines, lang_info.language, lang_info.script)

        return ExtractionPipelineResult(
            tenant_id=tenant_id,
            raw_message=raw_message,
            normalised_text=norm_msg.cleaned_text,
            detected_script=lang_info.script,
            detected_language=lang_info.language,
            intent=intent.value,
            order_status=status_out,
            auto_confirmed=can_auto_confirm,
            overall_confidence=overall_conf,
            requires_human_review=any_line_review,
            review_reasons=order_review_reasons,
            lines=extracted_lines,
            subtotal_paise=sum(ln.line_subtotal_paise for ln in extracted_lines),
            cgst_paise=sum(ln.cgst_paise for ln in extracted_lines),
            sgst_paise=sum(ln.sgst_paise for ln in extracted_lines),
            igst_paise=sum(ln.igst_paise for ln in extracted_lines),
            total_paise=sum(ln.line_total_paise for ln in extracted_lines),
            clarification_reply=s11.result,
            stage_trace={
                "stage1_normalise": s1.evidence_span,
                "stage2_language": f"{lang_info.script}/{lang_info.language}",
                "stage3_intent": intent.value,
                "stage5_segments": len(segments),
            },
        )


class OrderExtractionEngine:
    """Adapter used by FastAPI endpoints in main.py wrapping HybridExtractor."""

    def __init__(
        self,
        tenant_config: dict[str, Any],
        sku_catalog: list[dict[str, Any]],
        llm_provider: ExtractionModelProvider | None = None,
    ) -> None:
        self.tenant_config = tenant_config
        self.sku_catalog = sku_catalog
        self.extractor = HybridExtractor(llm_provider=llm_provider)

    def parse_message(
        self,
        message_text: str,
        price_tier: str = "WHOLESALE",
        is_inter_state: bool = False,
    ) -> dict[str, Any]:
        tenant_id = str(self.tenant_config.get("slug") or self.tenant_config.get("id") or "patel_fmcg")
        auto_confirm = bool(self.tenant_config.get("autoConfirmEnabled", False))
        res = self.extractor.extract_order(
            raw_message=message_text,
            tenant_id=tenant_id,
            catalogue_override=self.sku_catalog,
            price_tier=price_tier,
            auto_confirm_enabled=auto_confirm,
            is_inter_state=is_inter_state,
        )
        return {
            "tenant_id": res.tenant_id,
            "raw_message": res.raw_message,
            "normalised_text": res.normalised_text,
            "detected_script": res.detected_script,
            "detected_language": res.detected_language,
            "intent": res.intent,
            "status": res.order_status,
            "auto_confirmed": res.auto_confirmed,
            "overall_confidence": res.overall_confidence,
            "requires_human_review": res.requires_human_review,
            "review_reasons": res.review_reasons,
            "clarification_reply": res.clarification_reply,
            "subtotal_paise": res.subtotal_paise,
            "cgst_paise": res.cgst_paise,
            "sgst_paise": res.sgst_paise,
            "igst_paise": res.igst_paise,
            "total_paise": res.total_paise,
            "subtotal": float(paise_to_rupees_str(res.subtotal_paise)),
            "tax_total": float(paise_to_rupees_str(res.cgst_paise + res.sgst_paise + res.igst_paise)),
            "grand_total": float(paise_to_rupees_str(res.total_paise)),
            "lines": [asdict(ln) for ln in res.lines],
            "stage_trace": res.stage_trace,
        }
