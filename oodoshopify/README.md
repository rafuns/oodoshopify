# Shopify Odoo Connector (`oodoshopify`)

A commercial-grade, two-way **Shopify ⇄ Odoo 19** integration — GraphQL-first
(Admin API 2025-04), built to go beyond a sync tool with intelligence features
no other connector offers.

> License: **OPL-1** · Price: **$99 USD** · Odoo **19.0** · Support: rafuns@gmail.com

---

## Highlights

**Core sync (two-way where it makes sense)**
- Products & variants, media, SEO, tags, metafields
- Orders (+ fulfilment via `fulfillmentCreateV2`), draft orders, returns/RMA, refunds (two-way)
- Customers (+ `customerMerge` dedupe), collections, discounts, gift cards
- Inventory (multi-location), payouts/accounting, abandoned checkouts → CRM leads
- Themes, blogs, articles, pages, files, translations, markets, B2B companies

**Real-time + scale**
- 15 webhook topics (incl. `app/uninstalled`), HMAC-verified
- Background, **chunked** imports with live progress (Import Jobs) — scales to huge catalogs
- Cost-based rate limiting + `@idempotent` on financial mutations
- Scheduled pushes & timed sales with automatic revert

**Differentiators (unique to this connector)**
| Feature | What it does |
|---|---|
| Profit-Margin intelligence | Real net margin = Shopify revenue − Odoo COGS |
| Sync-Drift detection | Flags Odoo↔Shopify mismatches, one-click self-heal |
| AI Content Studio | LLM-generate titles, descriptions, SEO, tags |
| Dry-run preview | Before→after diff of price pushes before they go live |
| Dynamic repricing | Scarcity markup / overstock markdown with a margin floor |
| Demand→replenishment | Sell-through forecast → Odoo reordering rules |
| AI order-risk triage | Heuristic + AI fulfill/review/hold recommendation |
| NL sync console | "import paid orders from last 30 days" → runs it |
| Field-mapping studio | Map Odoo fields ↔ metafields with transforms |
| Metaobjects sync | Custom structured content types |
| Markets / B2B pricing | Push Odoo pricelists into Shopify market price lists |

---

## Install (local dev)

Requires Docker. From the parent folder:

```bash
docker compose up -d           # Odoo 19 + Postgres 16
# open http://localhost:8069, create a DB, install "Shopify Odoo Connector"
```

See `../SETUP_GUIDE.md` for the full beginner walkthrough and
`../PUBLISH_CHECKLIST.md` for App-Store submission steps.

## Connect a store

1. Shopify admin → **Settings → Apps → Develop apps** → create a custom app, grant scopes.
2. In Odoo: **Shopify → Instances → New** → paste the Admin API token → **Test / Connect**
   (or use **Connect with OAuth** with a public HTTPS URL).
3. **Register Webhooks** for real-time sync (needs public HTTPS — use a tunnel locally).

## AI features

Set an **OpenAI-compatible API key** on the instance (Connection tab) to enable
the AI Content Studio, AI order-risk recommendations, and the NL sync console.
Bring-your-own-key; no data goes to the publisher.

## Tests

```bash
docker compose run --rm -u root odoo odoo -d <db> -u oodoshopify \
  --test-enable --test-tags /oodoshopify --stop-after-init --no-http
```
Current suite: **254 tests**.

## External services

This module calls the **Shopify Admin API** using your store credentials
(stored only in your Odoo database). See the in-app description for the full
data-flow disclosure.
