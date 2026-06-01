"""Tests for shopify.abandoned.checkout — import, lead creation, recovery."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase

SAMPLE_CHECKOUT_NODE = {
    'id': 'gid://shopify/AbandonedCheckout/8080',
    'email': 'bob@example.com',
    'phone': '+1-555-0200',
    'createdAt': '2025-03-10T09:00:00Z',
    'updatedAt': '2025-03-10T09:30:00Z',
    'completedAt': None,
    'abandonedCheckoutUrl': 'https://test-store.myshopify.com/checkouts/recover/abc123',
    'totalPriceSet': {'shopMoney': {'amount': '89.99', 'currencyCode': 'USD'}},
    'lineItems': {'edges': [
        {'node': {
            'id': 'gid://shopify/CheckoutLineItem/101',
            'title': 'Blue Widget',
            'quantity': 1,
            'variant': {
                'id': 'gid://shopify/ProductVariant/202',
                'price': '89.99',
                'sku': 'WIDGET-BLU',
            },
        }},
    ]},
    'customer': {
        'id': 'gid://shopify/Customer/303',
        'firstName': 'Bob',
        'lastName': 'Jones',
        'email': 'bob@example.com',
        'phone': '+1-555-0200',
    },
}

RECOVERED_CHECKOUT_NODE = dict(
    SAMPLE_CHECKOUT_NODE,
    completedAt='2025-03-11T12:00:00Z',
)


class TestAbandonedCheckoutUpsert(ShopifyTestBase):

    def test_upsert_creates_record(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('instance_id', '=', self.instance.id),
            ('shopify_checkout_id', '=', '8080'),
        ])
        self.assertEqual(len(checkout), 1)

    def test_upsert_sets_customer_name(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertEqual(checkout.customer_name, 'Bob Jones')

    def test_upsert_sets_state_open(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertEqual(checkout.state, 'open')

    def test_upsert_sets_state_recovered_when_completed(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, RECOVERED_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertEqual(checkout.state, 'recovered')

    def test_upsert_sets_total_price(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertAlmostEqual(checkout.total_price, 89.99)

    def test_upsert_creates_line_items(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertEqual(len(checkout.line_ids), 1)
        self.assertEqual(checkout.line_ids[0].product_title, 'Blue Widget')

    def test_upsert_idempotent(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        count = self.env['shopify.abandoned.checkout'].search_count([
            ('instance_id', '=', self.instance.id),
            ('shopify_checkout_id', '=', '8080'),
        ])
        self.assertEqual(count, 1)

    def test_upsert_sets_checkout_url(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        checkout = self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])
        self.assertIn('abc123', checkout.checkout_url)


class TestCRMLeadCreation(ShopifyTestBase):

    def _make_checkout(self):
        self.env['shopify.abandoned.checkout']._upsert_checkout(
            self.instance, SAMPLE_CHECKOUT_NODE
        )
        return self.env['shopify.abandoned.checkout'].search([
            ('shopify_checkout_id', '=', '8080')
        ])

    def test_creates_crm_lead(self):
        checkout = self._make_checkout()
        if checkout.odoo_lead_id:
            return  # was auto-created during upsert
        checkout._create_crm_lead(self.instance)
        self.assertTrue(checkout.odoo_lead_id)

    def test_lead_has_expected_revenue(self):
        checkout = self._make_checkout()
        if not checkout.odoo_lead_id:
            checkout._create_crm_lead(self.instance)
        self.assertAlmostEqual(checkout.odoo_lead_id.expected_revenue, 89.99)

    def test_lead_has_email(self):
        checkout = self._make_checkout()
        if not checkout.odoo_lead_id:
            checkout._create_crm_lead(self.instance)
        self.assertEqual(checkout.odoo_lead_id.email_from, 'bob@example.com')

    def test_lead_probability_is_10(self):
        checkout = self._make_checkout()
        if not checkout.odoo_lead_id:
            checkout._create_crm_lead(self.instance)
        self.assertAlmostEqual(checkout.odoo_lead_id.probability, 10.0)

    def test_manual_create_raises_if_lead_exists(self):
        checkout = self._make_checkout()
        if not checkout.odoo_lead_id:
            checkout._create_crm_lead(self.instance)
        # Manual create should raise since lead already exists
        if checkout.odoo_lead_id:
            with self.assertRaises(UserError):
                checkout.action_create_lead_manual()

    def test_mark_lost_updates_state(self):
        checkout = self._make_checkout()
        checkout.action_mark_lost()
        self.assertEqual(checkout.state, 'lost')


class TestCheckoutLineSubtotal(ShopifyTestBase):

    def test_subtotal_computed(self):
        line = self.env['shopify.abandoned.checkout.line'].new({
            'quantity': 2,
            'unit_price': 44.995,
        })
        self.assertAlmostEqual(line.subtotal, 89.99, places=1)
