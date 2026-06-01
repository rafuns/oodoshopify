"""Tests for shopify.order — import, sale order creation, currency, cancellation."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase, SAMPLE_ORDER_NODE


class TestOrderUpsert(ShopifyTestBase):

    def test_upsert_creates_record(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([
            ('instance_id', '=', self.instance.id),
            ('shopify_order_id', '=', '9999'),
        ])
        self.assertEqual(len(order), 1)

    def test_upsert_sets_name(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertEqual(order.name, '#1001')

    def test_upsert_sets_financial_status(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertEqual(order.financial_status, 'paid')

    def test_upsert_sets_fulfillment_status(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertEqual(order.fulfillment_status, 'unfulfilled')

    def test_upsert_sets_totals(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertAlmostEqual(order.total_price, 59.99)
        self.assertEqual(order.currency, 'USD')

    def test_upsert_creates_line_items(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertEqual(len(order.line_ids), 1)
        line = order.line_ids[0]
        self.assertEqual(line.product_name, 'Test T-Shirt')
        self.assertEqual(line.quantity, 2)
        self.assertAlmostEqual(line.price, 29.99)

    def test_upsert_idempotent(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        count = self.env['shopify.order'].search_count([
            ('instance_id', '=', self.instance.id),
            ('shopify_order_id', '=', '9999'),
        ])
        self.assertEqual(count, 1)

    def test_upsert_updates_status_on_second_call(self):
        self.env['shopify.order']._upsert_order(self.instance, SAMPLE_ORDER_NODE)
        updated = dict(SAMPLE_ORDER_NODE, displayFinancialStatus='PARTIALLY_REFUNDED')
        self.env['shopify.order']._upsert_order(self.instance, updated)
        order = self.env['shopify.order'].search([('shopify_order_id', '=', '9999')])
        self.assertEqual(order.financial_status, 'partially_refunded')


class TestCreateSaleOrder(ShopifyTestBase):

    def test_creates_sale_order(self):
        shopify_order = self._make_shopify_order()
        self.env['shopify.order.line'].create({
            'order_id': shopify_order.id,
            'product_name': 'Test T-Shirt',
            'quantity': 2,
            'price': 29.99,
            'sku': 'TSHIRT-S-WHT',
        })
        shopify_order.action_create_sale_order()
        self.assertTrue(shopify_order.odoo_sale_order_id)

    def test_creates_partner_from_email(self):
        shopify_order = self._make_shopify_order()
        shopify_order.action_create_sale_order()
        partner = self.env['res.partner'].search([('email', '=', 'alice@example.com')])
        self.assertTrue(partner)

    def test_reuses_existing_partner(self):
        partner = self.env['res.partner'].create({
            'name': 'Alice Smith',
            'email': 'alice@example.com',
        })
        shopify_order = self._make_shopify_order()
        shopify_order.action_create_sale_order()
        so = shopify_order.odoo_sale_order_id
        self.assertEqual(so.partner_id, partner)

    def test_error_if_sale_order_exists(self):
        shopify_order = self._make_shopify_order()
        shopify_order.action_create_sale_order()
        with self.assertRaises(UserError):
            shopify_order.action_create_sale_order()

    def test_sale_order_origin(self):
        shopify_order = self._make_shopify_order()
        shopify_order.action_create_sale_order()
        so = shopify_order.odoo_sale_order_id
        self.assertEqual(so.origin, '#1001')

    def test_currency_applied_to_sale_order(self):
        usd = self.env['res.currency'].search([('name', '=', 'USD')], limit=1)
        shopify_order = self._make_shopify_order()
        shopify_order.currency = 'USD'
        shopify_order.action_create_sale_order()
        so = shopify_order.odoo_sale_order_id
        # currency should be USD
        self.assertEqual(so.currency_id, usd)


class TestOrderFinancialStatusMapping(ShopifyTestBase):

    def _map(self, gql_status):
        from odoo.addons.oodoshopify.models.shopify_order import _FINANCIAL_STATUS_MAP
        return _FINANCIAL_STATUS_MAP.get(gql_status)

    def test_paid_maps_correctly(self):
        self.assertEqual(self._map('PAID'), 'paid')

    def test_pending_maps_correctly(self):
        self.assertEqual(self._map('PENDING'), 'pending')

    def test_voided_maps_correctly(self):
        self.assertEqual(self._map('VOIDED'), 'voided')

    def test_refunded_maps_correctly(self):
        self.assertEqual(self._map('REFUNDED'), 'refunded')

    def test_unknown_returns_none(self):
        self.assertIsNone(self._map('UNKNOWN_STATUS'))


class TestCancelOrderWizard(ShopifyTestBase):

    def test_wizard_creates_with_defaults(self):
        shopify_order = self._make_shopify_order()
        wizard = self.env['shopify.cancel.order.wizard'].create({
            'shopify_order_id': shopify_order.id,
        })
        self.assertEqual(wizard.reason, 'OTHER')
        self.assertTrue(wizard.refund)
        self.assertTrue(wizard.restock)
        self.assertTrue(wizard.notify_customer)

    def test_cannot_cancel_already_voided(self):
        shopify_order = self._make_shopify_order(financial_status='voided')
        with self.assertRaises(UserError):
            shopify_order.action_open_cancel_wizard()

    def test_cannot_cancel_already_refunded(self):
        shopify_order = self._make_shopify_order(financial_status='refunded')
        with self.assertRaises(UserError):
            shopify_order.action_open_cancel_wizard()

    def test_open_cancel_wizard_returns_action(self):
        shopify_order = self._make_shopify_order()
        result = shopify_order.action_open_cancel_wizard()
        self.assertEqual(result['res_model'], 'shopify.cancel.order.wizard')
        self.assertEqual(result['target'], 'new')
