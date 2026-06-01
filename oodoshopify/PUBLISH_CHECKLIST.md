# 📦 Odoo App Store — Pre-Submission Checklist & Audit

_Last audit: 2026-06-01 — module `oodoshopify`, version 19.0.1.0.0_

## ✅ Automated audit results

| Check | Status |
|---|---|
| Manifest `name` ("Shopify Odoo Connector", 22 chars) | ✅ |
| `version` = `19.0.1.0.0` (Odoo-19 prefix) | ✅ |
| `license` = `OPL-1` (valid for paid) | ✅ |
| `price` = 99.00, `currency` = USD | ✅ |
| `support` email set | ✅ |
| `summary` + rich HTML `description` | ✅ |
| All `depends` resolve (base, sale_management, stock, account, product, mail, crm, mrp, delivery) | ✅ |
| Icon present (`static/description/icon.png`) | ✅ |
| Cover/banner present (`static/description/banner.png`, in `images`) | ✅ |
| External-service (Shopify API) disclosure on description page | ✅ |
| ACL coverage — every model has access rules (0 warnings on load) | ✅ |
| Automated tests | ✅ 247 passing |
| Module installs & upgrades clean | ✅ |

## ⚠️ Things to confirm before upload (manual)

- **Screenshots** — required for a good listing (see shot-list below).
- **Icon/cover dimensions** — confirm against the live upload form (commonly square icon ≥140×140, banner cover). Current files exist; verify they look crisp.
- **Heavy dependency note:** the module depends on `mrp` (for kit/BoM) and `crm` (abandoned→leads). These auto-install but pull in Manufacturing/CRM. Acceptable, but be aware some buyers may not expect MRP. (Optional: make MRP optional later.)
- **Pricing parity rule:** the Odoo store price must be the lowest you offer anywhere.
- **Per-version SKU:** this is the Odoo 19 build only; 17/18 would be separate uploads.
- **`[UNVERIFIED]` AI feature:** the AI Content Studio calls an OpenAI-compatible API the merchant configures with their own key — this is disclosed; make sure the listing mentions "bring your own AI key."

## 📸 Screenshot shot-list (8–10 shots, English UI)

Take these at ~1280px wide, clean demo data, on the **odoo** DB:

1. **Dashboard** — store card with KPIs, health row (last order, imports running), and Sync buttons. *Caption: "Live store dashboard with one-click actions."*
2. **Instances form** — Connection + Sync Options + AI section (blur the token). *Caption: "Connect via OAuth or custom-app token."*
3. **Products list** → open one product form showing header buttons (Push, Push Prices, Push Tags & SEO, Sync Inventory) + Metafields tab. *Caption: "Two-way product sync with SEO, tags & metafields."*
4. **Sync menu open** — showing the full hub (Sync Products/Orders, Import Customers, Scheduled Pushes, etc.). *Caption: "Every sync in one place."*
5. **Import Jobs** — a running import with the progress bar. *Caption: "Background, chunked imports that scale to huge catalogs."*
6. **Profit Margin** pivot/graph — revenue vs COGS vs margin. *Caption: "Real profit per order, product & store — not just revenue."*
7. **Sync Drift** list — open drifts with Push/Pull/Ignore buttons. *Caption: "Detects Odoo↔Shopify mismatches and fixes them in one click."*
8. **AI Content Studio** wizard. *Caption: "Generate titles, descriptions, SEO & tags with AI."*
9. **Scheduled Sales** form — a % sale with start/end. *Caption: "Timed sales that apply and auto-revert."*
10. **Dry-run preview** dialog — before→after price diff. *Caption: "Preview every price change before it goes live."*

## How to capture
1. Reload `localhost:8069` on the **odoo** database (admin).
2. Create 3–5 demo products/orders (or run a small import from a dev store).
3. Use the browser at ~1280px; screenshot each screen above.
4. Crop to the content area; avoid showing real tokens/emails.
5. Upload on the Odoo app submission form (description page) — English captions.
