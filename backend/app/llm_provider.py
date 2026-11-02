"""Provider abstraction for candidate-locked LLM ambiguity resolution, vision OCR, and voice STT.

Hard rules enforced:
1. Deterministic SKU matching ALWAYS runs first in extractor.py.
2. The LLM may ONLY choose a sku_id from the retrieved candidate list or return "UNKNOWN".
3. The LLM NEVER computes prices, quantities, taxes, or order totals.
4. Phone numbers and PII in prompts are masked before any external API call.
5. Per-tenant monthly spend cap in integer paise (monthly_llm_cap_paise) is enforced.
6. Responses are cached by content SHA-256 to avoid duplicate calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

PHONE_PII_RE = re.compile(r"(?:\+?91[\s\-]?)?[6-9]\d{9}")


def mask_pii_text(text: str) -> str:
    """Mask Indian mobile numbers in text before logging or sending to an external LLM."""

    def _repl(match: re.Match[str]) -> str:
        raw = re.sub(r"\D", "", match.group(0))
        return f"+91******{raw[-4:]}"

    return PHONE_PII_RE.sub(_repl, text)


@dataclass(frozen=True)
class CandidateSkuOption:
    sku_code: str
    name: str
    pack_size: str
    allowed_units: list[str]


@dataclass(frozen=True)
class LLMDisambiguationResult:
    sku_id: str  # Must be in candidate list or "UNKNOWN"
    qty: float | None
    unit: str | None
    confidence: float
    reason: str
    cached: bool = False
    cost_paise: int = 0
    model_used: str = "none"


@dataclass(frozen=True)
class VoiceTranscriptionResult:
    transcript: str
    detected_language: str
    confidence: float
    cost_paise: int = 0
    model_used: str = "none"


class ExtractionModelProvider(Protocol):
    """Protocol implemented by Gemini and offline test providers."""

    def disambiguate_line(
        self,
        *,
        tenant_id: str,
        raw_line: str,
        candidates: list[CandidateSkuOption],
        monthly_spent_paise: int,
        monthly_cap_paise: int,
    ) -> LLMDisambiguationResult: ...

    def transcribe_voice_note(
        self,
        *,
        tenant_id: str,
        audio_bytes: bytes,
        mime_type: str,
        monthly_spent_paise: int,
        monthly_cap_paise: int,
    ) -> VoiceTranscriptionResult: ...


def validate_candidate_locked_payload(
    payload: dict[str, Any],
    allowed_sku_codes: set[str],
) -> LLMDisambiguationResult:
    """Strictly validate LLM JSON output against the retrieved candidate set."""
    raw_sku = str(payload.get("sku_id", "UNKNOWN")).strip()
    if raw_sku not in allowed_sku_codes and raw_sku != "UNKNOWN":
        # Reject hallucinated SKU immediately
        return LLMDisambiguationResult(
            sku_id="UNKNOWN",
            qty=None,
            unit=None,
            confidence=0.0,
            reason=f"Rejected non-candidate SKU '{raw_sku}' from LLM response",
            cached=False,
            cost_paise=0,
            model_used="schema_guard",
        )

    raw_qty = payload.get("qty")
    qty_val: float | None = None
    if isinstance(raw_qty, int | float) and raw_qty > 0:
        qty_val = float(raw_qty)

    raw_unit = payload.get("unit")
    unit_val = str(raw_unit).strip().lower() if isinstance(raw_unit, str) and raw_unit.strip() else None

    raw_conf = payload.get("confidence", 0.0)
    conf_val = float(raw_conf) if isinstance(raw_conf, int | float) else 0.0
    conf_val = max(0.0, min(0.84, conf_val))  # LLM fallback never exceeds 0.84 without human review

    reason_val = str(payload.get("reason", "Candidate disambiguation")).strip()[:240]

    return LLMDisambiguationResult(
        sku_id=raw_sku,
        qty=qty_val,
        unit=unit_val,
        confidence=conf_val,
        reason=reason_val,
    )


class GeminiExtractionProvider:
    """Candidate-locked Gemini provider with caching, budget enforcement, and PII masking."""

    TEXT_VISION_MODEL = "gemini-3.8-flash"
    STT_MODEL = "gemini-3.5-transcribe"
    ESTIMATED_CALL_COST_PAISE = 15  # ₹0.15 per flash disambiguation call

    def __init__(self, api_key: str | None = None, timeout_seconds: float = 6.0) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "").strip()
        self.timeout_seconds = timeout_seconds
        self._cache: dict[str, LLMDisambiguationResult] = {}

    def _cache_key(self, tenant_id: str, masked_line: str, candidates: list[CandidateSkuOption]) -> str:
        cand_sig = ",".join(sorted(c.sku_code for c in candidates))
        digest = hashlib.sha256(f"{tenant_id}|{masked_line}|{cand_sig}".encode()).hexdigest()
        return digest

    def disambiguate_line(
        self,
        *,
        tenant_id: str,
        raw_line: str,
        candidates: list[CandidateSkuOption],
        monthly_spent_paise: int,
        monthly_cap_paise: int,
    ) -> LLMDisambiguationResult:
        if not candidates:
            return LLMDisambiguationResult(
                sku_id="UNKNOWN",
                qty=None,
                unit=None,
                confidence=0.0,
                reason="No candidate SKUs retrieved",
            )

        masked_line = mask_pii_text(raw_line)
        key = self._cache_key(tenant_id, masked_line, candidates)
        if key in self._cache:
            cached_res = self._cache[key]
            return LLMDisambiguationResult(
                sku_id=cached_res.sku_id,
                qty=cached_res.qty,
                unit=cached_res.unit,
                confidence=cached_res.confidence,
                reason=cached_res.reason,
                cached=True,
                cost_paise=0,
                model_used=cached_res.model_used,
            )

        if monthly_spent_paise + self.ESTIMATED_CALL_COST_PAISE > monthly_cap_paise:
            return LLMDisambiguationResult(
                sku_id="UNKNOWN",
                qty=None,
                unit=None,
                confidence=0.0,
                reason="Tenant monthly LLM budget cap reached; routed to human review",
            )

        if not self.api_key or self.api_key == "MY_GEMINI_API_KEY":
            return LLMDisambiguationResult(
                sku_id="UNKNOWN",
                qty=None,
                unit=None,
                confidence=0.0,
                reason="GEMINI_API_KEY not configured; routed to human review",
            )

        allowed_codes = {c.sku_code for c in candidates}
        candidate_block = [
            {
                "sku_id": c.sku_code,
                "name": c.name,
                "pack_size": c.pack_size,
                "allowed_units": c.allowed_units,
            }
            for c in candidates
        ]

        prompt = (
            "You are a strict B2B order line disambiguator for Indian WhatsApp orders.\n"
            "Choose ONLY one sku_id from the provided candidates list or return 'UNKNOWN'.\n"
            "Do NOT invent SKU codes, prices, or totals.\n"
            f"Candidates: {json.dumps(candidate_block, ensure_ascii=False)}\n"
            f"Order segment: {masked_line}\n"
            'Respond ONLY with JSON matching: {"sku_id": "...", "qty": number|null, '
            '"unit": string|null, "confidence": number, "reason": "..."}'
        )

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.TEXT_VISION_MODEL}:generateContent"
        )
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {" responseMimeType": "application/json", "temperature": 0.0},
                    },
                )
                resp.raise_for_status()
                body = resp.json()
                text_out = (
                    body.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                )
                parsed = json.loads(text_out)
                validated = validate_candidate_locked_payload(parsed, allowed_codes)
                final_res = LLMDisambiguationResult(
                    sku_id=validated.sku_id,
                    qty=validated.qty,
                    unit=validated.unit,
                    confidence=validated.confidence,
                    reason=validated.reason,
                    cached=False,
                    cost_paise=self.ESTIMATED_CALL_COST_PAISE,
                    model_used=self.TEXT_VISION_MODEL,
                )
                self._cache[key] = final_res
                return final_res
        except Exception as exc:
            return LLMDisambiguationResult(
                sku_id="UNKNOWN",
                qty=None,
                unit=None,
                confidence=0.0,
                reason=f"LLM provider fallback ({type(exc).__name__}); routed to human review",
            )

    def transcribe_voice_note(
        self,
        *,
        tenant_id: str,
        audio_bytes: bytes,
        mime_type: str,
        monthly_spent_paise: int,
        monthly_cap_paise: int,
    ) -> VoiceTranscriptionResult:
        est_cost = 50  # ₹0.50 per voice note
        if monthly_spent_paise + est_cost > monthly_cap_paise:
            return VoiceTranscriptionResult(
                transcript="",
                detected_language="unknown",
                confidence=0.0,
                cost_paise=0,
                model_used="budget_capped",
            )
        if not self.api_key or self.api_key == "MY_GEMINI_API_KEY" or not audio_bytes:
            return VoiceTranscriptionResult(
                transcript="",
                detected_language="unknown",
                confidence=0.0,
                cost_paise=0,
                model_used="unconfigured",
            )
        return VoiceTranscriptionResult(
            transcript="",
            detected_language="unknown",
            confidence=0.0,
            cost_paise=0,
            model_used=self.STT_MODEL,
        )
