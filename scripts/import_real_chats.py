"""DPDP-Compliant Real Distributor WhatsApp Chat Importer for OrderBridge Evaluation.

Usage:
  python3 scripts/import_real_chats.py --input path/to/whatsapp_export.txt --tenant patel_fmcg

Features:
- Strips WhatsApp timestamp/sender headers ([dd/mm/yy, hh:mm:ss] Name:)
- Masks all Indian mobile numbers (+91******1234) before saving to disk
- Tags every imported record with `"source": "real_pilot"` so `scripts/eval.py`
  reports Synthetic vs. Real Pilot metrics separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

WA_LINE_RE = re.compile(
    r"^(?:\[?\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\]?\s*(?:-\s*)?)"
    r"([^:]{1,50}):\s*(.+)$"
)
PHONE_RE = re.compile(r"(?:\+?91[\s\-]?)?[6-9]\d{9}")


def mask_phone_numbers(text: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return f"+91******{digits[-4:]}"

    return PHONE_RE.sub(_repl, text)


def import_whatsapp_export(input_path: Path, output_path: Path, tenant_id: str, vertical: str) -> int:
    raw_lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines()
    imported_count = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("a", encoding="utf-8") as out_f:
        for line in raw_lines:
            m = WA_LINE_RE.match(line.strip())
            body = m.group(2).strip() if m else line.strip()
            if not body or "<Media omitted>" in body or "Messages and calls are end-to-end encrypted" in body:
                continue
            anonymised = mask_phone_numbers(body)
            msg_hash = hashlib.sha256(anonymised.encode("utf-8")).hexdigest()[:10]
            record = {
                "id": f"REAL-{msg_hash.upper()}",
                "source": "real_pilot",
                "tenant_id": tenant_id,
                "vertical": vertical,
                "language": "auto",
                "phenomenon": "real_distributor_chat",
                "text": anonymised,
                "expected_intent": "NEW_ORDER",
                "expected_lines": [],
                "expect_review": False,
            }
            out_f.write(json.dumps(record, ensure_ascii=False) + "\n")
            imported_count += 1

    return imported_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Import anonymised WhatsApp chat export into eval/dataset_real.jsonl")
    parser.add_argument("--input", required=True, type=Path, help="Path to exported WhatsApp .txt file")
    parser.add_argument(
        "--output",
        default=Path(__file__).resolve().parents[1] / "eval" / "dataset_real.jsonl",
        type=Path,
        help="Destination JSONL path",
    )
    parser.add_argument("--tenant", default="patel_fmcg", help="Target tenant_id")
    parser.add_argument("--vertical", default="fmcg", help="Target vertical tag")
    args = parser.parse_args()

    count = import_whatsapp_export(args.input, args.output, args.tenant, args.vertical)
    print(f"Imported {count} anonymised real pilot messages into {args.output}")


if __name__ == "__main__":
    main()
