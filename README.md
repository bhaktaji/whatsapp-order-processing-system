# OrderBridge — Multi-Tenant WhatsApp Order Capture & Ban-Resilient B2B Suite

A white-label, multi-tenant SaaS platform built for **Distributors, Wholesalers, Small Food Brands, and Instagram/WhatsApp Sellers** who receive unstructured orders over WhatsApp and spend hours every night re-typing them into Excel, Tally Prime, Xero, or Zoho Books.

## Why OrderBridge Exists

1. **Zero Customer Signup**: Buyers refuse B2B portals. They keep texting or sending voice notes on WhatsApp in Hinglish, shorthand, or regional slang (`"Bhaiya 5 peti Parle-G, 2 katta Basmati 25kg bhej do"`).
2. **Automated Draft Order Extraction**: Hybrid deterministic + LLM extraction maps colloquial aliases, unit multipliers (`peti`, `katta`, `crate`, `box`), and buyer price tiers into structured draft orders with per-line confidence scores.
3. **One-Click Accounting Sync**: Pushes verified orders straight into **Tally Prime (XML Vouchers)**, **Xero**, **Zoho Books**, **QuickBooks**, or formatted **Excel/CSV** batches.
4. **Day-One Meta Ban Resilience**: Treats Meta's WhatsApp Business API as a volatile vendor dependency. Includes automated 24h window guardrails, rate-limit protection, daily shadow contact backups, and instant failover to **Standby WhatsApp Numbers, Twilio SMS, Telegram Bot, and Zero-Signup Magic Micro-Store Links** if a primary number is banned or rate-limited.
5. **Commercial White-Label Ready**: Every tenant has an isolated configuration (`config/tenants.json` / PostgreSQL `Tenant` table) controlling branding, currency, tax engine (`GST`, `VAT`, `Sales Tax`), custom unit dictionaries, extraction prompts, and feature flags.

---

## Repository Structure

```text
whatsapp_order_capture_codebase/
├── README.md
├── docker-compose.yml
├── .env.example
├── config/
│   └── tenants.json                 # White-label presets for FMCG, Instagram D2C, Horeca & Custom clients
├── backend/
│   ├── requirements.txt
│   ├── prisma/
│   │   └── schema.prisma            # Multi-tenant PostgreSQL schema (Tenants, SKUs, Aliases, Orders, Channels)
│   └── app/
│       ├── __init__.py
│       ├── main.py                  # FastAPI server: webhooks, tenant config API, order lifecycle
│       ├── extractor.py             # Hybrid fuzzy-SKU + LLM order extraction engine (Hinglish/shorthand aware)
│       ├── failover.py              # Ban-resilience monitor, Meta policy guard & SMS/Telegram/Web failover
│       └── accounting_sync.py       # Tally Prime XML, Xero, Zoho Books, QuickBooks & Excel/CSV exporters
└── frontend/
    └── index.html                   # Single-file interactive Admin Workbench & White-Label Studio
```

---

## Quick Start

### 1. Run the Interactive Workbench Immediately (Browser / No Server Required)
Open `frontend/index.html` in any browser to use the full Order Capture Workbench, simulate incoming WhatsApp/Instagram/SMS orders, test Meta ban failover, export Tally XML / Xero JSON / CSV files, and configure white-label client profiles.

### 2. Run the Full-Stack Webhook & API Server
```bash
cp .env.example .env
docker compose up --build -d
```
Or run locally with Python 3.11+:
```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

---

## Selling & Customizing for Different Clients

To onboard a new client (e.g., a regional spice distributor, an Instagram apparel seller, or a restaurant supply wholesaler):
1. Open the **White-Label & Controls Studio** tab in the web app (or edit `config/tenants.json`).
2. Configure the client's **Brand Name, Accent Color, Currency, Tax Engine (CGST/SGST, IGST, VAT, Sales Tax), and ERP Connector**.
3. Import their SKU list and common buyer shorthand aliases (e.g., `"chota pack" -> SKU-102`).
4. Set their **Failover Channels** (Secondary WhatsApp Cloud API Phone ID, Twilio SMS number, Telegram Bot handle, and Magic Order Link slug).
5. Export the tenant JSON or deploy via Docker with their `TENANT_ID`.
