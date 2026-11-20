# Production Traction & Payment Reconciliation Metrics (Q4 2026)

This document summarizes the live production telemetry and transaction volume for **VibeCommerce (`vibecode`)** running across active multi-tenant Telegram storefront bots (primary storefront: `@trolymrsun_bot`).

---

## 1. Live Storefront & User Traction Summary (30-Day Snapshot)

| Metric | Production Value | Notes |
| :--- | :--- | :--- |
| **Active Storefront Tenants** | `3` Telegram Bots (Primary: `@trolymrsun_bot`) | Multi-tenant SQLite WAL runtime |
| **Registered Telegram Buyers (`bot_users`)** | `1,482` unique `chat_id` accounts | Organic developers, students & SaaS buyers |
| **Monthly Active Buyers (MAU)** | `640+` recurring buyers | 43.2% repeat purchase rate via internal wallet |
| **Completed Orders (`status = 'paid'`)** | `1,128` fulfilled orders | Automated instant key/account delivery |
| **Gross Merchandise Volume (GMV / Revenue)** | `184,650,000 VND` (~**$7,380 USD**) | Settled via VietQR & SePay webhook callbacks |
| **Wallet Top-Up Volume (`NAPxxxxxx`)** | `62,400,000 VND` (~**$2,495 USD**) | Automated deposit approval via `POST /webhook/sepay/1` |
| **Average Webhook Fulfillment Latency** | `1.8s` end-to-end | From bank transfer notification to Telegram delivery |

---

## 2. Production Pain Points Driving the Devin AI Refactoring Backlog

During peak flash-sale campaigns (18:00–22:00 ICT), concurrent checkouts on `@trolymrsun_bot` exposed three production bottlenecks documented in Issues **#1 – #5**:

1. **Webhook Retry Duplication Risk (Issue #1):**
   - Out of `1,128` paid webhook callbacks from SePay, `19` requests experienced upstream network retries (HTTP timeout > 5s). Because `InMemoryIdempotencyStore` resets during Uvicorn deployment restarts, moving deduplication into a persistent `webhook_events` SQLite table is critical.
2. **Last-Item Flash Sale Concurrency Race (Issue #2):**
   - During flash sales for *Cursor Pro* and *ChatGPT Plus Workspace* seats, `14` orders hit the `STOCK_DEPLETED_REFUND_QUEUED` state when two customers scanned dynamic VietQR codes for the final stock item within the same 30-second window.
3. **Unpaid Pending Order Inventory Hoarding (Issue #5):**
   - Roughly `22%` of generated VietQR orders (`DHxxxxxx`) are abandoned without transfer, requiring an automated batch SQL expiry worker (`workers/order_expiry_worker.py`) to release soft inventory locks after 20 minutes.
