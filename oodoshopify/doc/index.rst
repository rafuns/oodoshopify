Shopify Odoo Connector
======================

Full Shopify integration for Odoo 19: orders, products, pricing, customers,
inventory, refunds, and theme management — GraphQL-first, built on API 2025-04.

.. contents::
   :local:
   :depth: 2

Setup
-----

1. **Create a Shopify Custom App**

   Go to ``Shopify Admin → Settings → Apps and sales channels → Develop apps``.
   Create a new app and grant the following Admin API scopes:

   - ``read_products``, ``write_products``
   - ``read_orders``, ``write_orders``
   - ``read_customers``, ``write_customers``
   - ``read_inventory``, ``write_inventory``
   - ``read_themes``, ``write_themes``
   - ``read_fulfillments``, ``write_fulfillments``

   Copy the **API Key** and **API Secret**.

2. **Create an instance in Odoo**

   Go to ``Shopify → Instances → New``. Enter your shop domain
   (``my-store.myshopify.com``), API Key, and API Secret.

3. **Connect with OAuth**

   Click **Connect with OAuth**. You will be redirected to Shopify to authorise
   the app and then returned to Odoo automatically. The access token is saved.

4. **Register Webhooks**

   Click **Register Webhooks**. This creates real-time subscriptions for orders,
   products, customers, inventory, and refunds on Shopify.

5. **Sync Locations**

   Click **Sync Locations**. Open each location record, assign it to an Odoo
   warehouse, then set a **Default Inventory Location** on the instance.

6. **Import Inventory Levels**

   On the default location record, click **Import Inventory Levels** to load
   current stock quantities and wire up variant → location GID mappings.

Core Features
-------------

Orders
~~~~~~

- Real-time import via webhooks (``orders/create``, ``orders/updated``)
- Scheduled import: last N days, filtered by status
- Auto-create Odoo sale orders from Shopify orders
- Push delivery tracking numbers back to Shopify (``fulfillmentCreateV2``)
- Order cancellation handled via webhook

Products & Pricing
~~~~~~~~~~~~~~~~~~

- Bi-directional product sync (``productCreate`` / ``productUpdate``)
- Bulk variant price push using ``productVariantsBulkUpdate``
- Compare-at price (sale pricing) support
- Scheduled price push cron (disabled by default — enable in Technical → Automation)

Inventory
~~~~~~~~~

- Push Odoo stock quantities to Shopify via ``inventorySetQuantities``
- Real-time inventory level updates via webhook
- Inventory diff view (Odoo qty vs Shopify qty per location)
- Scheduled inventory level import (every 4 hours)

Customers
~~~~~~~~~

- Import Shopify customers → Odoo contacts (smart email/phone matching)
- Export Odoo contacts → Shopify customers
- Marketing consent and verified email tracking

Refunds
~~~~~~~

- Webhook-driven: ``refunds/create`` fires automatically
- Full refund detail fetched via GraphQL
- Odoo credit note created automatically against the original invoice
- Manual trigger available for historical refunds

Theme Manager
~~~~~~~~~~~~~

- List all Shopify themes and switch the active theme
- Load and edit ``config/settings_data.json`` (colors, fonts, layout toggles)
- Browse, load, and push individual CSS/JS/Liquid assets
- Theme assets editor within Odoo (note: Shopify Theme API is REST-only;
  this is the only part of the connector that does not use GraphQL)

Reliability
-----------

- **Sync Queue**: failed operations are queued and retried up to 3 times
- **Activity Logs**: all errors logged with HTTP status, 30-day retention
- **HMAC Verification**: all incoming webhooks verified against ``webhook_secret``
- **Rate Limit Detection**: warns when Shopify API call limit is near exhaustion
- **Multi-store**: configure one instance per Shopify store

Architecture
------------

All API calls use **Shopify Admin GraphQL API 2025-04** except for the
Theme API (REST-only — Shopify has no GraphQL equivalent for themes).

Cursor-based pagination is used for all list queries (``pageInfo.hasNextPage``
+ ``endCursor``). Shopify Global IDs (GIDs) are stored alongside numeric IDs
on all mapping records for use in mutations.

Cron Jobs
---------

+-------------------------------+-------------------+---------+
| Job                           | Default Interval  | Active  |
+===============================+===================+=========+
| Import Orders                 | Every 1 hour      | Yes     |
+-------------------------------+-------------------+---------+
| Import Customers              | Every 12 hours    | Yes     |
+-------------------------------+-------------------+---------+
| Import Inventory Levels       | Every 4 hours     | Yes     |
+-------------------------------+-------------------+---------+
| Process Sync Queue            | Every 15 minutes  | Yes     |
+-------------------------------+-------------------+---------+
| Push Prices                   | Every 6 hours     | No      |
+-------------------------------+-------------------+---------+
| Purge Old Logs                | Daily             | Yes     |
+-------------------------------+-------------------+---------+

Support
-------

Email: rafuns@gmail.com
