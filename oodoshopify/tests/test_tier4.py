"""Tests for Tier 4 — inventory transfers, sales report, scheduled reports."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase


class TestInventoryTransfer(ShopifyTestBase):

    def _make_locations(self):
        loc_a = self.env['shopify.location'].create({
            'name': 'Warehouse A', 'instance_id': self.instance.id,
            'shopify_location_id': '111', 'shopify_location_gid': 'gid://shopify/Location/111',
        })
        loc_b = self.env['shopify.location'].create({
            'name': 'Warehouse B', 'instance_id': self.instance.id,
            'shopify_location_id': '222', 'shopify_location_gid': 'gid://shopify/Location/222',
        })
        return loc_a, loc_b

    def test_same_location_rejected(self):
        loc_a, _ = self._make_locations()
        transfer = self.env['shopify.inventory.transfer'].create({
            'name': 'T1', 'instance_id': self.instance.id,
            'origin_location_id': loc_a.id, 'dest_location_id': loc_a.id,
            'line_ids': [(0, 0, {'product_name': 'X', 'quantity': 1})],
        })
        with self.assertRaises(UserError):
            transfer.action_execute_transfer()

    def test_requires_lines(self):
        loc_a, loc_b = self._make_locations()
        transfer = self.env['shopify.inventory.transfer'].create({
            'name': 'T2', 'instance_id': self.instance.id,
            'origin_location_id': loc_a.id, 'dest_location_id': loc_b.id,
        })
        with self.assertRaises(UserError):
            transfer.action_execute_transfer()

    def test_execute_success(self):
        loc_a, loc_b = self._make_locations()
        transfer = self.env['shopify.inventory.transfer'].create({
            'name': 'T3', 'instance_id': self.instance.id,
            'origin_location_id': loc_a.id, 'dest_location_id': loc_b.id,
            'line_ids': [(0, 0, {
                'product_name': 'Widget', 'quantity': 5,
                'shopify_inventory_item_gid': 'gid://shopify/InventoryItem/901',
            })],
        })
        self.mock_gql.return_value = {
            'inventoryMoveQuantities': {'inventoryAdjustmentGroup': {'id': 'x', 'reason': 'movement_created'},
                                        'userErrors': []},
        }
        transfer.action_execute_transfer()
        self.assertEqual(transfer.state, 'completed')


class TestSalesReport(ShopifyTestBase):

    def test_best_sellers_query(self):
        # Create an order with lines so the SQL view has data
        order = self._make_shopify_order()
        self.env['shopify.order.line'].create({
            'order_id': order.id, 'product_name': 'Top Seller',
            'sku': 'TS-1', 'quantity': 10, 'price': 20.0,
        })
        # SQL view is read-only; just ensure the helper runs without error
        result = self.env['shopify.sales.report'].get_best_sellers(self.instance.id, limit=5)
        self.assertIsInstance(result, list)


class TestScheduledReport(ShopifyTestBase):

    def test_build_best_sellers_body(self):
        report = self.env['shopify.scheduled.report'].create({
            'name': 'Weekly', 'instance_id': self.instance.id,
            'report_type': 'best_sellers', 'frequency': 'weekly',
        })
        body = report._build_report_body()
        self.assertIn('Best Sellers', body)

    def test_build_summary_body(self):
        report = self.env['shopify.scheduled.report'].create({
            'name': 'Summary', 'instance_id': self.instance.id,
            'report_type': 'sales_summary', 'frequency': 'daily',
        })
        body = report._build_report_body()
        self.assertIn('Sales Summary', body)
