{
    'name': 'Shopify Odoo Connector',
    'version': '19.0.1.0.0',
    'category': 'eCommerce',
    'summary': 'Real-time Shopify sync: orders, products, inventory, customers, '
               'payouts, themes + AI content, profit margins & drift detection',
    'description': """
Shopify Odoo Connector
======================

Two-way, GraphQL-first (API 2025-04) Shopify integration:

* Orders, products & variants, customers, collections, inventory, refunds,
  returns, draft orders, payouts, gift cards, discounts, metafields, themes.
* Real-time webhooks (incl. app/uninstalled) + scheduled & background imports
  that scale to very large catalogs (chunked, queue-driven, with progress).
* Multi-store, multi-currency, multi-warehouse, B2B companies, markets.

Beyond a connector — features no other Shopify connector offers:

* Profit-margin intelligence (Shopify revenue vs Odoo COGS).
* Sync-drift detection with one-click self-healing.
* AI product content studio (titles, descriptions, SEO, tags).
* Dry-run preview of price pushes before they go live.
* Scheduled price pushes and timed sales with automatic revert.

Connects to the Shopify Admin API using your own store credentials.
""",
    'author': 'Ennovation Brands',
    'website': 'https://ennovationbrands.com',
    'license': 'OPL-1',
    'price': 99.00,
    'currency': 'USD',
    'support': 'rafuns@gmail.com',
    'depends': [
        'base',
        'sale_management',
        'stock',
        'account',
        'product',
        'mail',
        'crm',
        'mrp',
        'delivery',
    ],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/email_templates.xml',
        'data/scheduled_actions.xml',
        'views/shopify_dashboard_views.xml',
        'views/shopify_metafield_views.xml',
        'views/shopify_instance_views.xml',
        'views/shopify_location_views.xml',
        'views/shopify_product_views.xml',
        'views/shopify_order_views.xml',
        'views/shopify_workflow_views.xml',
        'views/shopify_refund_views.xml',
        'views/shopify_payout_views.xml',
        'views/shopify_collection_views.xml',
        'views/shopify_shipping_method_views.xml',
        'views/shopify_draft_order_views.xml',
        'views/shopify_return_views.xml',
        'views/shopify_discount_views.xml',
        'views/shopify_bulk_operation_views.xml',
        'views/shopify_tier2_views.xml',
        'views/shopify_tier3_views.xml',
        'views/shopify_tier4_views.xml',
        'views/shopify_abandoned_checkout_views.xml',
        'views/shopify_customer_views.xml',
        'views/shopify_theme_views.xml',
        'views/shopify_queue_views.xml',
        'views/shopify_import_job_views.xml',
        'views/shopify_scheduled_push_views.xml',
        'views/shopify_scheduled_sale_views.xml',
        'views/shopify_drift_views.xml',
        'views/shopify_margin_views.xml',
        'views/shopify_innovation2_views.xml',
        'views/shopify_log_views.xml',
        'wizards/sync_products_wizard_views.xml',
        'wizards/sync_orders_wizard_views.xml',
        'wizards/theme_settings_wizard_views.xml',
        'wizards/cancel_order_wizard_views.xml',
        'wizards/setup_wizard_views.xml',
        'wizards/ai_content_wizard_views.xml',
        'wizards/dry_run_wizard_views.xml',
        'wizards/nl_console_wizard_views.xml',
        'views/sync_actions.xml',
        # Menus LAST — they reference actions defined in all files above.
        'views/menu_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'assets': {
        'web.assets_backend': [
            'oodoshopify/static/src/scss/instance_form.scss',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
