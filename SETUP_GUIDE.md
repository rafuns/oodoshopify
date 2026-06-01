# 🚀 Setup Guide — Running Your Shopify Odoo Connector

This guide gets your module running on your own computer, step by step.
**No prior experience needed.** Total time: ~30 minutes (most of it waiting for downloads).

---

## What you'll end up with

- Odoo 19 running in your web browser at `http://localhost:8069`
- Your `oodoshopify` module installed and clickable
- A free Shopify test store connected to it

---

## STEP 1 — Install Docker Desktop (one time)

Docker is a free tool that runs Odoo for you without messy manual installation.

1. Go to **https://www.docker.com/products/docker-desktop/**
2. Download **Docker Desktop for Mac** (Apple Silicon or Intel — pick your chip)
3. Open the downloaded `.dmg` and drag Docker to Applications
4. Launch **Docker** from Applications. Wait until the whale icon 🐳 in your
   top menu bar stops animating (means it's ready)

> ✅ Checkpoint: the Docker whale icon is steady in your menu bar.

---

## STEP 2 — Open the Terminal in the right folder

1. Open the **Terminal** app (press `Cmd + Space`, type "Terminal", hit Enter)
2. Copy-paste this exact line and press Enter:

```bash
cd /Users/rafunthapa/Documents/works/oodoo
```

> ✅ Checkpoint: run `ls` and you should see `docker-compose.yml` and `oodoshopify`.

---

## STEP 3 — Start Odoo

Copy-paste this and press Enter:

```bash
docker compose up
```

The first time, this downloads Odoo and PostgreSQL (a few hundred MB — give it
5–10 minutes). You'll see lots of text scroll by. When it slows down and you see
a line like `HTTP service (werkzeug) running`, it's ready.

> ⚠️ Leave this Terminal window open — closing it stops Odoo.
> To stop Odoo later: click in this window and press `Ctrl + C`.

> ✅ Checkpoint: the text stops scrolling and mentions "running on 0.0.0.0:8069".

---

## STEP 4 — Open Odoo & create your database

1. Open your browser to **http://localhost:8069**
2. You'll see a "Database setup" page. Fill in:
   - **Master Password:** `admin` (just for local testing)
   - **Database Name:** `shopify_test`
   - **Email:** your email (this becomes your login)
   - **Password:** pick something you'll remember
   - **Country:** your country
   - Leave "Demo data" **unchecked**
3. Click **Create database**. Wait ~1 minute.

> ✅ Checkpoint: you're logged into Odoo and see the apps screen.

---

## STEP 5 — Install your module

1. Top-left, click the **app menu** → go to **Apps**
2. Click **Update Apps List** (top menu). If you don't see it:
   - Click your **name** (top right) → **Settings** → scroll down → turn on
     **Developer Mode** → go back to **Apps**
3. In the Apps search box, **remove the "Apps" filter** (click the ✕ next to it),
   then type: **Shopify**
4. You'll see **Shopify Odoo Connector** → click **Activate / Install**

> ⚠️ If you get an error during install, **copy the full red error message** and
> paste it to me — that's exactly the kind of runtime bug we want to catch.

> ✅ Checkpoint: a **Shopify** menu appears in the top menu bar.

---

## STEP 6 — Get a free Shopify test store

1. Go to **https://www.shopify.com/partners** and sign up (free)
2. In the Partner Dashboard → **Stores** → **Add store** → **Create development store**
3. Pick "Create a store to test and build" → name it anything
4. Once created, you have a free store like `your-name.myshopify.com`

### Get API credentials
1. In your Shopify store admin → **Settings** → **Apps and sales channels**
   → **Develop apps** → **Create an app**
2. Name it "Odoo Connector"
3. Under **Configuration** → **Admin API integration** → select scopes
   (read/write products, orders, customers, inventory, etc.) → Save
4. **Install app** → reveal the **Admin API access token** + **API key/secret**

---

## STEP 7 — Connect Odoo to Shopify

There are **two ways** to connect. For local testing, use **Path A**.

### Path A — Custom app token (recommended for local / testing) ✅

This skips OAuth entirely, so it works on `localhost` with no public URL.

1. In your Shopify store admin → **Settings → Apps and sales channels
   → Develop apps → Create an app** → name it "Odoo Connector".
2. **Configuration → Admin API integration → Configure** → tick the scopes
   (read/write for products, orders, customers, inventory, themes,
   fulfillments) → **Save**.
3. **Install app** (top right) → then **Reveal token once** → copy the
   **Admin API access token** (it starts with `shpat_…`).
4. In Odoo: **Shopify → Instances → New**.
   - **Store Name:** anything (e.g. "My Dev Store")
   - **Shop Domain:** `your-store.myshopify.com`
   - **Access Token:** paste the `shpat_…` token
   - Leave **API Key / API Secret** blank (only needed for OAuth)
5. Click **Save**, then **Test / Connect**.

> ✅ Checkpoint: you see "Connection Successful", the status flips to
> **Connected**, and after a **browser refresh** the Dashboard, Operations,
> Sync, Marketing… menus appear.

### Path B — OAuth (for production / Shopify App Store)

OAuth needs Shopify's servers to reach your Odoo over **public HTTPS**, so it
won't work on bare `localhost`. To test it locally, expose Odoo with a tunnel:

1. Install a tunnel, e.g. `ngrok http 8069`, and copy the `https://…` URL.
2. In Odoo: **Settings → Technical → System Parameters**, set
   `web.base.url` to that HTTPS URL.
3. In your Shopify app's **Configuration**, set the redirect URL to
   `https://<your-tunnel>/shopify/oauth/callback`.
4. Create the instance with **API Key + API Secret** filled in, then click
   **Connect with OAuth** and approve in Shopify. The callback verifies
   Shopify's `hmac` + `shop` signature and stores the token automatically.

---

## Common hiccups

| Problem | Fix |
|---|---|
| `docker: command not found` | Docker Desktop isn't running — launch it, wait for the steady whale 🐳 |
| Port 8069 already in use | Something else is using it. In Terminal: `docker compose down` then retry |
| `odoo:19` image not found | Edit `docker-compose.yml`, change `odoo:19` to `odoo:18`, retry |
| Module not in Apps list | Did you click **Update Apps List** after enabling Developer Mode? |
| Install error (red box) | Copy the full message and send it to me — it's a real bug to fix |
| Want a clean restart | `docker compose down -v` wipes everything, then `docker compose up` |

---

## Handy commands (run in the `oodoo` folder)

```bash
docker compose up            # start Odoo (keep window open)
docker compose up -d         # start in background (frees your Terminal)
docker compose down          # stop Odoo (keeps your data)
docker compose down -v       # stop + WIPE all data (fresh start)
docker compose logs -f odoo  # watch Odoo's log messages
```

### To reinstall the module after code changes
```bash
docker compose exec odoo odoo -u oodoshopify -d shopify_test --stop-after-init
docker compose restart odoo
```

### To run the test suite
```bash
docker compose exec odoo odoo -u oodoshopify -d shopify_test --test-enable --stop-after-init
```

---

**Stuck anywhere? Copy the exact error text and send it to me.** That's how we
find and fix the real-world bugs that only show up on a live install.
