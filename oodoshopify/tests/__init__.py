# Tests for oodoshopify Shopify Odoo Connector
# Odoo discovers tests by importing them here.

# Core
from . import test_instance
from . import test_product
from . import test_order
from . import test_customer
from . import test_refund
from . import test_workflow
from . import test_metafield
from . import test_payout
from . import test_abandoned_checkout

# Tier 1
from . import test_draft_order
from . import test_return
from . import test_discount
from . import test_bulk_operation

# Tier 2-4
from . import test_tier2
from . import test_tier3
from . import test_tier4

# Collections + shipping
from . import test_collection
from . import test_shipping_method
from . import test_queue
from . import test_scheduled_push
from . import test_scheduled_sale
from . import test_refund_push
from . import test_innovations
from . import test_innovations2
from . import test_client_reqs
