"""Tests for shopify.discount."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase, SAMPLE_DISCOUNT_NODE


class TestDiscountUpsert(ShopifyTestBase):

    def test_upsert_creates_code_discount(self):
        self.env['shopify.discount']._upsert_discount(self.instance, SAMPLE_DISCOUNT_NODE)
        rec = self.env['shopify.discount'].search([('shopify_discount_id', '=', '4001')])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec.discount_type, 'code')
        self.assertEqual(rec.code, 'SUMMER20')

    def test_upsert_percentage_value(self):
        self.env['shopify.discount']._upsert_discount(self.instance, SAMPLE_DISCOUNT_NODE)
        rec = self.env['shopify.discount'].search([('shopify_discount_id', '=', '4001')])
        self.assertEqual(rec.value_type, 'percentage')
        self.assertAlmostEqual(rec.value, 20.0)  # 0.2 → 20%

    def test_upsert_usage(self):
        self.env['shopify.discount']._upsert_discount(self.instance, SAMPLE_DISCOUNT_NODE)
        rec = self.env['shopify.discount'].search([('shopify_discount_id', '=', '4001')])
        self.assertEqual(rec.used_count, 12)
        self.assertEqual(rec.usage_limit, 100)

    def test_skip_unsupported_type(self):
        node = {'id': 'gid://shopify/DiscountNode/999',
                'discount': {'__typename': 'DiscountCodeBxgy'}}
        result = self.env['shopify.discount']._upsert_discount(self.instance, node)
        self.assertFalse(result)


class TestDiscountBuild(ShopifyTestBase):

    def test_build_code_input(self):
        disc = self.env['shopify.discount'].create({
            'name': 'Test', 'instance_id': self.instance.id,
            'discount_type': 'code', 'code': 'TEST10',
            'value_type': 'percentage', 'value': 10,
        })
        inp = disc._build_code_input()
        self.assertEqual(inp['code'], 'TEST10')
        self.assertAlmostEqual(inp['customerGets']['value']['percentage'], 0.1)

    def test_build_code_requires_code(self):
        disc = self.env['shopify.discount'].create({
            'name': 'NoCode', 'instance_id': self.instance.id,
            'discount_type': 'code', 'value_type': 'percentage', 'value': 10,
        })
        with self.assertRaises(UserError):
            disc._build_code_input()

    def test_build_fixed_amount(self):
        disc = self.env['shopify.discount'].create({
            'name': 'Fixed', 'instance_id': self.instance.id,
            'discount_type': 'automatic', 'value_type': 'fixed', 'value': 15,
        })
        gets = disc._build_customer_gets()
        self.assertIn('discountAmount', gets['value'])

    def test_push_code_discount(self):
        disc = self.env['shopify.discount'].create({
            'name': 'Push', 'instance_id': self.instance.id,
            'discount_type': 'code', 'code': 'PUSH5',
            'value_type': 'percentage', 'value': 5,
        })
        self.mock_gql.return_value = {
            'discountCodeBasicCreate': {
                'codeDiscountNode': {'id': 'gid://shopify/DiscountCodeNode/5050'},
                'userErrors': [],
            },
        }
        disc.action_push_to_shopify()
        self.assertEqual(disc.shopify_discount_id, '5050')
        self.assertEqual(disc.status, 'active')
