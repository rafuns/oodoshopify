# Regular (table-backed) models first — these create the real DB tables.
from . import shopify_instance
from . import shopify_pricelist_map
from . import shopify_metafield
from . import shopify_abandoned_checkout
from . import shopify_webhook_config
from . import shopify_collection
from . import shopify_shipping_method
from . import shopify_draft_order
from . import shopify_return
from . import shopify_discount
from . import shopify_bulk_operation
from . import shopify_gift_card
from . import shopify_market
from . import shopify_selling_plan
from . import shopify_content
from . import shopify_misc
from . import shopify_inventory_transfer
from . import shopify_location
from . import shopify_product
from . import shopify_order
from . import shopify_customer
from . import shopify_refund
from . import shopify_workflow
from . import shopify_theme
from . import shopify_payout
from . import shopify_stock_quant
from . import shopify_queue
from . import shopify_import_job
from . import shopify_sale_order
from . import shopify_scheduled_push
from . import shopify_scheduled_sale
from . import shopify_drift
from . import shopify_repricing
from . import shopify_replenishment
from . import shopify_metaobject
from . import shopify_log

# SQL-view models (_auto = False) LAST — they read from the tables above,
# so those tables must already exist when these views are created.
from . import shopify_sales_report
from . import shopify_dashboard
