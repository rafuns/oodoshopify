"""Tests for shopify.shipping.method mapping."""
from .common import ShopifyTestBase


class TestShippingMethod(ShopifyTestBase):

    def test_match_by_title(self):
        carrier = self.env['delivery.carrier'].search([], limit=1)
        mapping = self.env['shopify.shipping.method'].create({
            'instance_id': self.instance.id,
            'shopify_shipping_title': 'Standard Shipping',
            'odoo_carrier_id': carrier.id if carrier else False,
        })
        found = self.env['shopify.shipping.method'].get_carrier_for_title(
            self.instance, 'standard shipping'  # case-insensitive
        )
        self.assertEqual(found, mapping)

    def test_match_by_code_priority(self):
        m1 = self.env['shopify.shipping.method'].create({
            'instance_id': self.instance.id,
            'shopify_shipping_title': 'Express',
            'shopify_shipping_code': 'EXP',
        })
        found = self.env['shopify.shipping.method'].get_carrier_for_title(
            self.instance, 'Anything', code='EXP'
        )
        self.assertEqual(found, m1)

    def test_unique_title_per_instance(self):
        self.env['shopify.shipping.method'].create({
            'instance_id': self.instance.id, 'shopify_shipping_title': 'Free',
        })
        with self.assertRaises(Exception):
            self.env['shopify.shipping.method'].create({
                'instance_id': self.instance.id, 'shopify_shipping_title': 'Free',
            })
            self.env.flush_all()
