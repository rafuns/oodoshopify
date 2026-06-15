"""Tests for client-requirement features: SO mapping, number override, paid+unfulfilled filter."""
from unittest.mock import patch
from .common import ShopifyTestBase


class TestSalesImportFidelity(ShopifyTestBase):

    def _workflow(self, **kw):
        vals = {'instance_id': self.instance.id, 'auto_confirm_order': False,
                'tax_handling': 'none'}
        vals.update(kw)
        return self.env['shopify.workflow'].create(vals)

    def test_so_field_mapping_and_number_override(self):
        self.instance.order_prefix = 'ZA'
        self._workflow(use_shopify_order_number=True)
        order = self._make_shopify_order()           # name '#1001'
        order.order_tags = 'vip, wholesale'
        order.order_note = 'Leave at door'
        order.action_create_sale_order()
        so = order.odoo_sale_order_id
        self.assertTrue(so)
        self.assertEqual(so.shopify_order_number, '#1001')
        self.assertEqual(so.sales_order_prefix, 'ZA')
        self.assertEqual(so.shopify_instance_id, self.instance)
        self.assertEqual(so.shopify_tags, 'vip, wholesale')
        self.assertEqual(so.client_order_ref, '#1001')
        self.assertIn('Leave at door', str(so.note))
        self.assertEqual(so.name, '#1001')           # Odoo sequence overridden

    def test_so_number_not_overridden_by_default(self):
        self._workflow(use_shopify_order_number=False)
        order = self._make_shopify_order()
        order.action_create_sale_order()
        # Default Odoo sequence is used (not the Shopify number)
        self.assertNotEqual(order.odoo_sale_order_id.name, '#1001')
        # But provenance fields are still mapped
        self.assertEqual(order.odoo_sale_order_id.shopify_order_number, '#1001')

    def test_paid_unfulfilled_filter_applied(self):
        self.instance.import_paid_unfulfilled_only = True
        captured = {}

        def fake(query, variables=None, **kw):
            captured['q'] = (variables or {}).get('query', '')
            return {'orders': {'edges': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}

        with patch.object(type(self.instance), '_graphql_request', side_effect=fake):
            self.env['shopify.order'].import_orders_page(
                self.instance, 'created_at:>=2024-01-01', None)
        self.assertIn('financial_status:paid', captured['q'])
        self.assertIn('fulfillment_status:unfulfilled', captured['q'])

    def test_filter_not_applied_when_off(self):
        self.instance.import_paid_unfulfilled_only = False
        captured = {}

        def fake(query, variables=None, **kw):
            captured['q'] = (variables or {}).get('query', '')
            return {'orders': {'edges': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}

        with patch.object(type(self.instance), '_graphql_request', side_effect=fake):
            self.env['shopify.order'].import_orders_page(self.instance, 'created_at:>=2024-01-01', None)
        self.assertNotIn('financial_status:paid', captured['q'])


class TestOrderEditing(ShopifyTestBase):

    def _fake_graphql(self, calls, lines=None):
        if lines is None:
            lines = [{'id': 'gid://shopify/CalculatedLineItem/9', 'quantity': 2,
                      'variant': {'id': 'gid://shopify/ProductVariant/77', 'sku': 'A'}}]
        def fake(query, variables=None, **kw):
            calls.append((query, variables))
            if 'orderEditBegin' in query:
                return {'orderEditBegin': {'calculatedOrder': {
                    'id': 'gid://shopify/CalculatedOrder/1',
                    'lineItems': {'edges': [{'node': n} for n in lines]}}, 'userErrors': []}}
            if 'orderEditSetQuantity' in query:
                return {'orderEditSetQuantity': {'calculatedOrder': {'id': 'x'}, 'userErrors': []}}
            if 'orderEditCommit' in query:
                return {'orderEditCommit': {'order': {'id': 'x'}, 'userErrors': []}}
            return {}
        return fake

    def test_set_line_quantity(self):
        order = self._make_shopify_order()
        calls = []
        with patch.object(type(self.instance), '_graphql_request', side_effect=self._fake_graphql(calls)):
            order.action_set_line_quantity('gid://shopify/ProductVariant/77', 5)
        sq = next(v for q, v in calls if 'orderEditSetQuantity' in q)
        self.assertEqual(sq['quantity'], 5)
        self.assertEqual(sq['lineItemId'], 'gid://shopify/CalculatedLineItem/9')
        self.assertTrue(any('orderEditCommit' in q for q, v in calls))

    def test_remove_line_sets_qty_zero(self):
        order = self._make_shopify_order()
        calls = []
        with patch.object(type(self.instance), '_graphql_request', side_effect=self._fake_graphql(calls)):
            order.action_remove_line_item('gid://shopify/ProductVariant/77')
        sq = next(v for q, v in calls if 'orderEditSetQuantity' in q)
        self.assertEqual(sq['quantity'], 0)

    def test_variant_not_on_order_raises(self):
        from odoo.exceptions import UserError
        order = self._make_shopify_order()
        calls = []
        with patch.object(type(self.instance), '_graphql_request', side_effect=self._fake_graphql(calls)):
            with self.assertRaises(UserError):
                order.action_set_line_quantity('gid://shopify/ProductVariant/NOPE', 3)


class TestStoreCreditRefund(ShopifyTestBase):

    def _cancel_capture(self, **wf):
        self.env['shopify.workflow'].create({
            'instance_id': self.instance.id, 'tax_handling': 'none', **wf})
        order = self._make_shopify_order()
        order.shopify_order_gid = 'gid://shopify/Order/9999'
        cap = {}
        def fake(query, variables=None, **kw):
            if 'orderCancel' in query:
                cap['vars'] = variables
                return {'orderCancel': {'order': {'id': 'x', 'cancelledAt': '2026-01-01'},
                        'orderCancelUserErrors': []}}
            return {}
        with patch.object(type(self.instance), '_graphql_request', side_effect=fake):
            order.action_cancel_on_shopify(refund=True)
        return cap['vars']['refundMethod']

    def test_default_refund_original_payment(self):
        rm = self._cancel_capture(refund_to_store_credit=False)
        self.assertEqual(rm, {'originalPaymentMethodsRefund': True})

    def test_store_credit_refund_when_enabled(self):
        rm = self._cancel_capture(refund_to_store_credit=True)
        self.assertEqual(rm, {'storeCreditRefund': {}})


class TestInventoryBroadcast(ShopifyTestBase):

    def test_broadcast_hits_all_stores(self):
        self.instance.sync_inventory = True
        tmpl = self.env['product.product'].create(
            {'name': 'MultiStore Widget', 'type': 'consu'}).product_tmpl_id
        inst2 = self.env['shopify.instance'].create({
            'name': 'EU Store', 'shop_domain': 'eu-store.myshopify.com',
            'state': 'connected', 'sync_inventory': True,
            'company_id': self.company.id, 'access_token': 'shpat_x',
            'api_key': 'k', 'api_secret': 's',
        })
        p1 = self.env['shopify.product'].create({
            'name': 'MultiStore Widget', 'instance_id': self.instance.id,
            'shopify_product_id': 'M1', 'shopify_gid': 'gid://shopify/Product/M1',
            'odoo_product_id': tmpl.id})
        p2 = self.env['shopify.product'].create({
            'name': 'MultiStore Widget', 'instance_id': inst2.id,
            'shopify_product_id': 'M2', 'shopify_gid': 'gid://shopify/Product/M2',
            'odoo_product_id': tmpl.id})
        hit = []
        with patch.object(type(self.env['shopify.product']), 'action_sync_inventory',
                          autospec=True, side_effect=lambda self: hit.extend(self.ids)):
            p1.broadcast_inventory()
        self.assertIn(p1.id, hit)
        self.assertIn(p2.id, hit)  # fanned out to the other store


class TestAutoPush(ShopifyTestBase):

    def _linked_so_and_order(self, qty=3):
        op = self.env['product.product'].create({'name': 'Recon', 'type': 'consu'})
        sp = self.env['shopify.product'].create({
            'name': 'Recon', 'instance_id': self.instance.id,
            'shopify_product_id': 'R1', 'shopify_gid': 'gid://shopify/Product/R1',
            'odoo_product_id': op.product_tmpl_id.id})
        self.env['shopify.product.variant'].create({
            'shopify_product_id': sp.id, 'shopify_variant_gid': 'gid://shopify/ProductVariant/V1',
            'shopify_variant_id': 'V1', 'odoo_variant_id': op.id})
        partner = self.env['res.partner'].create({'name': 'C'})
        so = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {'product_id': op.id, 'product_uom_qty': qty, 'price_unit': 10})]})
        order = self._make_shopify_order()
        order.odoo_sale_order_id = so
        order.shopify_order_gid = 'gid://shopify/Order/9999'
        return so, order

    def _fake(self, calls):
        def fake(query, variables=None, **kw):
            calls.append((query, variables))
            if 'orderEditBegin' in query:
                return {'orderEditBegin': {'calculatedOrder': {'id': 'C1', 'lineItems': {'edges': [
                    {'node': {'id': 'L1', 'quantity': 1, 'variant': {'id': 'gid://shopify/ProductVariant/V1'}}}]}}, 'userErrors': []}}
            if 'orderEditSetQuantity' in query:
                return {'orderEditSetQuantity': {'calculatedOrder': {'id': 'C1'}, 'userErrors': []}}
            if 'orderEditCommit' in query:
                return {'orderEditCommit': {'order': {'id': 'x'}, 'userErrors': []}}
            if 'orderCancel' in query:
                return {'orderCancel': {'order': {'id': 'x', 'cancelledAt': '2026'}, 'orderCancelUserErrors': []}}
            return {}
        return fake

    def test_sync_lines_reconciles_quantity(self):
        so, order = self._linked_so_and_order(qty=3)
        calls = []
        with patch.object(type(self.instance), '_graphql_request', side_effect=self._fake(calls)):
            changed = order.with_context(shopify_skip_push=True).action_sync_lines_to_shopify()
        self.assertEqual(changed, 1)
        sq = next(v for q, v in calls if 'orderEditSetQuantity' in q)
        self.assertEqual(sq['quantity'], 3)   # Odoo qty 3 vs Shopify 1 → set to 3

    def test_auto_push_cancel(self):
        self.env['shopify.workflow'].create({
            'instance_id': self.instance.id, 'tax_handling': 'none', 'auto_push_cancel': True})
        so, order = self._linked_so_and_order()
        calls = []
        with patch.object(type(self.instance), '_graphql_request', side_effect=self._fake(calls)):
            so._action_cancel()
        self.assertTrue(any('orderCancel' in q for q, v in calls))
