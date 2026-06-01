"""Tests for shopify.scheduled.sale — apply sale price + auto-revert."""
from unittest.mock import patch
from datetime import timedelta
from odoo import fields
from odoo.exceptions import UserError, ValidationError
from .common import ShopifyTestBase


class TestScheduledSale(ShopifyTestBase):

    def setUp(self):
        super().setUp()
        self.product = self.env['shopify.product'].create({
            'name': 'Sale Product',
            'instance_id': self.instance.id,
            'shopify_product_id': '900',
            'shopify_gid': 'gid://shopify/Product/900',
        })
        self.variant = self.env['shopify.product.variant'].create({
            'shopify_product_id': self.product.id,
            'shopify_variant_gid': 'gid://shopify/ProductVariant/901',
            'shopify_variant_id': '901',
            'price': 100.0,
            'compare_at_price': 0.0,
        })

    def _make(self, **kw):
        vals = {
            'name': 'Test Sale',
            'instance_id': self.instance.id,
            'product_ids': [(6, 0, self.product.ids)],
            'discount_type': 'percent',
            'discount_value': 20.0,
            'start_date': fields.Datetime.now() + timedelta(hours=1),
        }
        vals.update(kw)
        return self.env['shopify.scheduled.sale'].create(vals)

    def test_percent_price_maths(self):
        sale = self._make(discount_type='percent', discount_value=25)
        self.assertAlmostEqual(sale._sale_price(100.0), 75.0)

    def test_amount_price_maths(self):
        sale = self._make(discount_type='amount', discount_value=30)
        self.assertAlmostEqual(sale._sale_price(100.0), 70.0)

    def test_fixed_price_maths(self):
        sale = self._make(discount_type='fixed', discount_value=9.99)
        self.assertAlmostEqual(sale._sale_price(100.0), 9.99)

    def test_invalid_percent_raises(self):
        with self.assertRaises(ValidationError):
            self._make(discount_type='percent', discount_value=150)

    def test_end_before_start_raises(self):
        with self.assertRaises(ValidationError):
            self._make(start_date=fields.Datetime.now(),
                       end_date=fields.Datetime.now() - timedelta(hours=1))

    def test_activate_snapshots_and_pushes(self):
        sale = self._make(discount_type='percent', discount_value=20)
        with patch.object(
            type(self.env['shopify.product']), 'push_variant_price_map', return_value=None,
        ) as mocked:
            sale._activate()
        self.assertEqual(sale.state, 'active')
        self.assertEqual(len(sale.line_ids), 1)
        line = sale.line_ids
        self.assertAlmostEqual(line.original_price, 100.0)
        self.assertAlmostEqual(line.sale_price, 80.0)
        # price_map carried sale price + original as compareAtPrice
        args = mocked.call_args.args[0]
        spec = args['gid://shopify/ProductVariant/901']
        self.assertEqual(spec['price'], '80.00')
        self.assertEqual(spec['compareAtPrice'], '100.00')

    def test_end_restores_original(self):
        sale = self._make(discount_type='percent', discount_value=20)
        with patch.object(type(self.env['shopify.product']), 'push_variant_price_map'):
            sale._activate()
        with patch.object(
            type(self.env['shopify.product']), 'push_variant_price_map', return_value=None,
        ) as mocked:
            sale._end()
        self.assertEqual(sale.state, 'ended')
        spec = mocked.call_args.args[0]['gid://shopify/ProductVariant/901']
        self.assertEqual(spec['price'], '100.00')
        self.assertIsNone(spec['compareAtPrice'])

    def test_cron_activates_and_ends(self):
        sale = self._make(
            discount_type='percent', discount_value=20,
            start_date=fields.Datetime.now() - timedelta(minutes=5))
        sale.action_schedule()
        with patch.object(type(self.env['shopify.product']), 'push_variant_price_map'):
            self.env['shopify.scheduled.sale'].cron_run_due()
        self.assertEqual(sale.state, 'active')
        # now give it a past end date and run again
        sale.end_date = fields.Datetime.now() - timedelta(minutes=1)
        with patch.object(type(self.env['shopify.product']), 'push_variant_price_map'):
            self.env['shopify.scheduled.sale'].cron_run_due()
        self.assertEqual(sale.state, 'ended')
