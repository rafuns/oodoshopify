"""Tests for shopify.return / RMA."""
from odoo.exceptions import UserError, ValidationError
from .common import ShopifyTestBase, SAMPLE_RETURNABLE_FULFILLMENTS


class TestReturnFetch(ShopifyTestBase):

    def test_fetch_returnable_items(self):
        order = self._make_shopify_order()
        self.mock_gql.return_value = SAMPLE_RETURNABLE_FULFILLMENTS
        items = self.env['shopify.return'].fetch_returnable_items(order)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['sku'], 'TSHIRT-S-WHT')
        self.assertEqual(items[0]['max_quantity'], 2)

    def test_load_returnable_items_creates_lines(self):
        order = self._make_shopify_order()
        self.mock_gql.return_value = SAMPLE_RETURNABLE_FULFILLMENTS
        ret = self.env['shopify.return'].create({
            'name': 'R1', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
        })
        ret.action_load_returnable_items()
        self.assertEqual(len(ret.line_ids), 1)
        self.assertEqual(ret.line_ids.max_quantity, 2)


class TestReturnLineValidation(ShopifyTestBase):

    def test_quantity_cannot_exceed_max(self):
        order = self._make_shopify_order()
        ret = self.env['shopify.return'].create({
            'name': 'R2', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
        })
        with self.assertRaises(ValidationError):
            self.env['shopify.return.line'].create({
                'return_id': ret.id, 'product_name': 'X',
                'max_quantity': 1, 'quantity': 5,
            })


class TestReturnActions(ShopifyTestBase):

    def test_create_return_requires_lines(self):
        order = self._make_shopify_order()
        ret = self.env['shopify.return'].create({
            'name': 'R3', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
        })
        with self.assertRaises(UserError):
            ret.action_create_return()

    def test_create_return_builds_and_sets_status(self):
        order = self._make_shopify_order()
        ret = self.env['shopify.return'].create({
            'name': 'R4', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
        })
        self.env['shopify.return.line'].create({
            'return_id': ret.id, 'product_name': 'Shirt',
            'fulfillment_line_item_gid': 'gid://shopify/FulfillmentLineItem/501',
            'max_quantity': 2, 'quantity': 1, 'return_reason': 'DEFECTIVE',
        })
        self.mock_gql.return_value = {
            'returnCreate': {'return': {'id': 'gid://shopify/Return/777', 'name': '#R4',
                                        'status': 'OPEN'}, 'userErrors': []},
        }
        ret.action_create_return()
        self.assertEqual(ret.shopify_return_id, '777')
        self.assertEqual(ret.status, 'open')

    def test_unique_constraint(self):
        order = self._make_shopify_order()
        self.env['shopify.return'].create({
            'name': 'RA', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
            'shopify_return_id': 'DUP1',
        })
        with self.assertRaises(Exception):
            self.env['shopify.return'].create({
                'name': 'RB', 'instance_id': self.instance.id, 'shopify_order_id': order.id,
                'shopify_return_id': 'DUP1',
            })
