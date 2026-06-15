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
