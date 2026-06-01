"""
Base test class and shared fixtures for oodoshopify tests.

All API calls (both GraphQL and REST) are mocked so tests run
without a real Shopify store connection.
"""
from unittest.mock import MagicMock, patch
from odoo.tests.common import TransactionCase


# ---------------------------------------------------------------------------
# Realistic Shopify API response fixtures
# ---------------------------------------------------------------------------

SAMPLE_PRODUCT_NODE = {
    'id': 'gid://shopify/Product/123456',
    'title': 'Test T-Shirt',
    'descriptionHtml': '<p>A great shirt.</p>',
    'vendor': 'Acme Co',
    'productType': 'Apparel',
    'status': 'ACTIVE',
    'tags': ['cotton', 'summer', 'sale'],
    'seo': {
        'title': 'Best T-Shirt | Acme Co',
        'description': 'Buy our best-selling cotton t-shirt.',
    },
    'variants': {'edges': [
        {'node': {
            'id': 'gid://shopify/ProductVariant/789',
            'title': 'Default Title',
            'price': '29.99',
            'compareAtPrice': '39.99',
            'sku': 'TSHIRT-S-WHT',
            'inventoryQuantity': 50,
            'inventoryItem': {'id': 'gid://shopify/InventoryItem/111'},
        }},
    ]},
    'images': {'edges': []},
}

SAMPLE_ORDER_NODE = {
    'id': 'gid://shopify/Order/9999',
    'name': '#1001',
    'email': 'alice@example.com',
    'createdAt': '2025-03-01T14:30:00Z',
    'displayFinancialStatus': 'PAID',
    'displayFulfillmentStatus': 'UNFULFILLED',
    'totalPriceSet':    {'shopMoney': {'amount': '59.99', 'currencyCode': 'USD'}},
    'subtotalPriceSet': {'shopMoney': {'amount': '54.99'}},
    'totalTaxSet':      {'shopMoney': {'amount': '5.00'}},
    'totalDiscountsSet':{'shopMoney': {'amount': '0.00'}},
    'lineItems': {'edges': [
        {'node': {
            'id': 'gid://shopify/LineItem/555',
            'title': 'Test T-Shirt',
            'variantTitle': 'Default Title',
            'sku': 'TSHIRT-S-WHT',
            'quantity': 2,
            'originalUnitPriceSet': {'shopMoney': {'amount': '29.99'}},
            'discountedTotalSet':   {'shopMoney': {'amount': '59.98'}},
            'variant': {'id': 'gid://shopify/ProductVariant/789'},
            'taxLines': [],
        }},
    ]},
}

SAMPLE_CUSTOMER_NODE = {
    'id': 'gid://shopify/Customer/4242',
    'firstName': 'Alice',
    'lastName': 'Smith',
    'email': 'alice@example.com',
    'phone': '+1-555-0100',
    'numberOfOrders': 3,
    'amountSpent': {'amount': '179.97', 'currencyCode': 'USD'},
    'tags': ['vip', 'wholesale'],
    'verifiedEmail': True,
    'emailMarketingConsent': {'marketingState': 'SUBSCRIBED'},
    'defaultAddress': {
        'address1': '123 Main St',
        'address2': '',
        'city': 'New York',
        'zip': '10001',
        'countryCodeV2': 'US',
    },
}

SAMPLE_REFUND_WEBHOOK = {
    'id': 77,
    'order_id': 9999,
    'created_at': '2025-03-02T10:00:00Z',
    'note': 'Customer changed mind',
    'currency': 'USD',
    'transactions': [{'amount': '29.99'}],
}

SAMPLE_PAYOUT_NODE = {
    'id': 'gid://shopify/ShopifyPaymentsPayout/5050',
    'issuedAt': '2025-03-05T00:00:00Z',
    'status': 'PAID',
    'net': {'amount': '145.50', 'currencyCode': 'USD'},
    'summary': {
        'chargesGross':     {'amount': '179.97', 'currencyCode': 'USD'},
        'chargesFee':       {'amount': '-5.40',  'currencyCode': 'USD'},
        'refundsGross':     {'amount': '-29.99', 'currencyCode': 'USD'},
        'refundsFee':       {'amount': '0.90',   'currencyCode': 'USD'},
        'adjustmentsGross': {'amount': '0.00',   'currencyCode': 'USD'},
        'adjustmentsFee':   {'amount': '0.00',   'currencyCode': 'USD'},
    },
}

SAMPLE_DRAFT_ORDER_NODE = {
    'id': 'gid://shopify/DraftOrder/3001',
    'name': 'D1',
    'status': 'OPEN',
    'invoiceUrl': 'https://test-store.myshopify.com/invoices/abc',
    'email': 'bob@example.com',
    'totalPriceSet': {'shopMoney': {'amount': '120.00', 'currencyCode': 'USD'}},
    'lineItems': {'edges': [
        {'node': {'title': 'Widget', 'quantity': 2, 'sku': 'WDG-1',
                  'originalUnitPriceSet': {'shopMoney': {'amount': '60.00'}},
                  'variant': {'id': 'gid://shopify/ProductVariant/55'}}},
    ]},
}

SAMPLE_DISCOUNT_NODE = {
    'id': 'gid://shopify/DiscountCodeNode/4001',
    'discount': {
        '__typename': 'DiscountCodeBasic',
        'title': 'Summer Sale',
        'status': 'ACTIVE',
        'usageLimit': 100,
        'asyncUsageCount': 12,
        'startsAt': '2025-06-01T00:00:00Z',
        'endsAt': '2025-09-01T00:00:00Z',
        'codes': {'edges': [{'node': {'code': 'SUMMER20'}}]},
        'customerGets': {'value': {'percentage': 0.2}},
    },
}

SAMPLE_GIFT_CARD_NODE = {
    'id': 'gid://shopify/GiftCard/6001',
    'lastCharacters': 'AB12',
    'enabled': True,
    'balance': {'amount': '45.00', 'currencyCode': 'USD'},
    'initialValue': {'amount': '50.00', 'currencyCode': 'USD'},
    'note': 'Loyalty reward',
    'customer': {'id': 'gid://shopify/Customer/4242', 'email': 'alice@example.com'},
}

SAMPLE_MARKET_NODE = {
    'id': 'gid://shopify/Market/7001',
    'name': 'Europe',
    'handle': 'europe',
    'status': 'ACTIVE',
    'currencySettings': {'baseCurrency': {'currencyCode': 'EUR'}},
}

SAMPLE_COMPANY_NODE = {
    'id': 'gid://shopify/Company/8001',
    'name': 'Acme Wholesale Ltd',
    'externalId': 'EXT-001',
    'totalSpent': {'amount': '15000.00', 'currencyCode': 'USD'},
    'locationsCount': {'count': 3},
    'contactsCount': {'count': 5},
}

SAMPLE_RETURNABLE_FULFILLMENTS = {
    'returnableFulfillments': {'edges': [
        {'node': {
            'id': 'gid://shopify/ReturnableFulfillment/9001',
            'fulfillment': {'id': 'gid://shopify/Fulfillment/123'},
            'returnableFulfillmentLineItems': {'edges': [
                {'node': {
                    'quantity': 2,
                    'fulfillmentLineItem': {
                        'id': 'gid://shopify/FulfillmentLineItem/501',
                        'lineItem': {'id': 'gid://shopify/LineItem/601',
                                     'title': 'Test T-Shirt', 'sku': 'TSHIRT-S-WHT', 'quantity': 2},
                    },
                }},
            ]},
        }},
    ]},
}

SAMPLE_METAFIELD_EDGES = [
    {'node': {
        'id': 'gid://shopify/Metafield/9001',
        'namespace': 'custom',
        'key': 'material',
        'value': '100% Cotton',
        'type': 'single_line_text_field',
        'updatedAt': '2025-03-01T00:00:00Z',
    }},
    {'node': {
        'id': 'gid://shopify/Metafield/9002',
        'namespace': 'custom',
        'key': 'weight_grams',
        'value': '250',
        'type': 'number_integer',
        'updatedAt': '2025-03-01T00:00:00Z',
    }},
]


# ---------------------------------------------------------------------------
# Base test case
# ---------------------------------------------------------------------------

class ShopifyTestBase(TransactionCase):
    """
    Base class for all oodoshopify tests.

    Provides:
    - A connected ShopifyInstance with mocked _graphql_request
    - Helper methods for creating linked Odoo records
    - A patch context manager for API calls
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.company = self.env.company

        # Create a minimal instance record (state='connected')
        self.instance = self.env['shopify.instance'].create({
            'name': 'Test Store',
            'shop_domain': 'test-store.myshopify.com',
            'api_key': 'fake_api_key',
            'api_secret': 'fake_api_secret',
            'access_token': 'shpat_test_token',
            'webhook_secret': 'my_webhook_secret',
            'state': 'connected',
            'company_id': self.company.id,
        })

        # Mock _graphql_request to prevent real HTTP calls
        self.mock_gql = MagicMock(return_value={})
        patcher = patch.object(
            self.instance.__class__,
            '_graphql_request',
            self.mock_gql,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # ---- helpers ----

    def _make_odoo_product(self, name='Test Product', default_code='SKU-001'):
        return self.env['product.template'].create({
            'name': name,
            'default_code': default_code,
            'type': 'consu',
            'list_price': 29.99,
        })

    def _make_shopify_product(self, odoo_product=None):
        vals = {
            'name': 'Test T-Shirt',
            'instance_id': self.instance.id,
            'shopify_product_id': '123456',
            'shopify_gid': 'gid://shopify/Product/123456',
            'shopify_status': 'ACTIVE',
        }
        if odoo_product:
            vals['odoo_product_id'] = odoo_product.id
        return self.env['shopify.product'].create(vals)

    def _make_shopify_order(self, financial_status='paid'):
        return self.env['shopify.order'].create({
            'name': '#1001',
            'instance_id': self.instance.id,
            'shopify_order_id': '9999',
            'shopify_order_gid': 'gid://shopify/Order/9999',
            'customer_email': 'alice@example.com',
            'total_price': 59.99,
            'currency': 'USD',
            'financial_status': financial_status,
            'fulfillment_status': 'unfulfilled',
            'sync_status': 'imported',
        })
