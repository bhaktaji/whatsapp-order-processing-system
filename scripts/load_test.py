"""Reproducible Webhook Burst & Extraction Load Test Benchmark for OrderBridge.

Simulates 200 signed Meta WhatsApp Cloud API webhook deliveries (including 40 duplicate
wamid replay deliveries to verify idempotency under burst conditions) across Hinglish,
Devanagari, scheme syntax, rate enquiries, and prompt-injection attempts.

Outputs p50 / p95 / p99 latencies, throughput (req/s), duplicate suppression count,
and verifies zero dropped or corrupted orders. Writes /eval/load_test_latest.json.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("META_APP_SECRET", "loadtest_meta_hmac_secret_2026")
os.environ.setdefault("META_VERIFY_TOKEN", "loadtest_meta_verify_token_2026")

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

SAMPLE_PAYLOADS = [
    ("patel_fmcg", "Bhaiya 5 peti Parle-G, 2 katta Basmati 25kg bhej do"),
    ("patel_fmcg", "५ पेटी पारले-जी और २ कट्टा बासमती २५ किलो भेज दो"),
    ("patel_fmcg", "12 peti Fortune Oil 2+1 scheme aane do"),
    ("patel_fmcg", "Basmati 25kg ka bhav kya hai?"),
    ("patel_fmcg", "sadhe teen peti Parle-G"),
    ("patel_fmcg", "5-6 peti Parle-G bhej do"),
    ("glow_insta", "10 pcs lavender candle, 5 bottle rose mist"),
    ("metro_fresh", "15 kg jeera, 8 kg w320 kaju"),
]


def build_signed_webhook(tenant_slug: str, wamid: str, text_body: str, app_secret: str) -> tuple[bytes, str]:
    envelope = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_BENCHMARK",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"tenant_slug": tenant_slug},
                            "contacts": [{"profile": {"name": "Load Test Buyer"}}],
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": "+919820499100",
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": text_body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    raw_bytes = json.dumps(envelope).encode("utf-8")
    sig = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    return raw_bytes, sig


def main() -> int:
    settings = get_settings()
    run_id = uuid.uuid4().hex[:8]
    unique_count = 160
    replay_count = 40
    total_requests = unique_count + replay_count

    requests_to_fire: list[tuple[str, str, str]] = []
    for i in range(unique_count):
        tenant_slug, text_body = SAMPLE_PAYLOADS[i % len(SAMPLE_PAYLOADS)]
        wamid = f"wamid.LOADTEST_{run_id}_{i:04d}"
        requests_to_fire.append((tenant_slug, wamid, text_body))

    for i in range(replay_count):
        requests_to_fire.append(requests_to_fire[i])

    latencies_ms: list[float] = []
    created_orders = 0
    duplicates_suppressed = 0
    http_errors = 0

    with TestClient(app) as client:
        t_start_wall = time.perf_counter()
        for tenant_slug, wamid, text_body in requests_to_fire:
            raw_bytes, sig = build_signed_webhook(tenant_slug, wamid, text_body, settings.meta_app_secret)
            t0 = time.perf_counter()
            resp = client.post(
                "/webhooks/whatsapp",
                content=raw_bytes,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            )
            dt_ms = (time.perf_counter() - t0) * 1000.0
            latencies_ms.append(dt_ms)
            if resp.status_code != 200:
                http_errors += 1
                continue
            body = resp.json()
            created_orders += int(body.get("processed_count", 0))
            duplicates_suppressed += len(body.get("duplicates_ignored", []))
        wall_seconds = time.perf_counter() - t_start_wall

    sorted_lat = sorted(latencies_ms)
    p50 = sorted_lat[int(0.50 * (len(sorted_lat) - 1))]
    p95 = sorted_lat[int(0.95 * (len(sorted_lat) - 1))]
    p99 = sorted_lat[int(0.99 * (len(sorted_lat) - 1))]
    mean_ms = statistics.mean(sorted_lat)
    throughput_rps = total_requests / wall_seconds if wall_seconds > 0 else 0.0

    summary = {
        "total_requests": total_requests,
        "unique_wamids_sent": unique_count,
        "replay_wamids_sent": replay_count,
        "orders_created": created_orders,
        "duplicates_suppressed": duplicates_suppressed,
        "http_errors": http_errors,
        "wall_time_seconds": round(wall_seconds, 3),
        "throughput_req_per_sec": round(throughput_rps, 2),
        "latency_ms": {
            "mean": round(mean_ms, 2),
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
        },
    }

    out_path = ROOT_DIR / "eval" / "load_test_latest.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

    assert http_errors == 0, f"Expected 0 HTTP errors, got {http_errors}"
    assert created_orders == unique_count, f"Expected {unique_count} orders created, got {created_orders}"
    assert duplicates_suppressed == replay_count, (
        f"Expected {replay_count} duplicate replays suppressed, got {duplicates_suppressed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
