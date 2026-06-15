"""Tests for the second wave of differentiators."""
from unittest.mock import patch
from .common import ShopifyTestBase


class _Base(ShopifyTestBase):
    def _product(self, list_price=100.0, cost=40.0, shop_price=100.0, qty=0.0):
        tmpl = self.env['product.product'].create({
            'name': 'Repr Widget', 'type': 'consu',
            'list_price': list_price, 'standard_price': cost,
        })
        product = self.env['shopify.product'].create({
            'name': 'Repr Widget', 'instance_id': self.instance.id,
            'shopify_product_id': '850', 'shopify_gid': 'gid://shopify/Product/850',
            'odoo_product_id': tmpl.product_tmpl_id.id,
        })
        self.env['shopify.product.variant'].create({
            'shopify_product_id': product.id,
            'shopify_variant_gid': 'gid://shopify/ProductVariant/851',
            'shopify_variant_id': '851', 'odoo_variant_id': tmpl.id, 'price': shop_price,
        })
        return product, tmpl


class TestRepricing(_Base):
    def test_low_stock_markup_respects_floor(self):
        rule = self.env['shopify.repricing.rule'].create({
            'name': 'Scarcity', 'instance_id': self.instance.id,
            'trigger': 'low_stock', 'threshold': 5,
            'adjustment_type': 'percent', 'adjustment_value': 10, 'min_margin_pct': 0,
        })
        # base 100, +10% = 110, floor cost*1 = 40 → 110
        self.assertAlmostEqual(rule._new_price(100, 40, markup=True), 110.0)
        # markdown below floor clamps to floor
        self.assertAlmostEqual(rule._new_price(40, 40, markup=False), 40.0)

    def test_apply_pushes_when_low(self):
        product, _ = self._product(list_price=100.0, shop_price=100.0, qty=0)
        rule = self.env['shopify.repricing.rule'].create({
            'name': 'Scarcity', 'instance_id': self.instance.id,
            'trigger': 'low_stock', 'threshold': 5,
            'adjustment_type': 'percent', 'adjustment_value': 10,
            'product_ids': [(6, 0, product.ids)],
        })
        with patch.object(type(self.env['shopify.product']), 'push_variant_price_map') as m:
            rule.apply()
        self.assertTrue(m.called)


class TestReplenishment(_Base):
    def test_scan_creates_suggestion(self):
        from odoo import fields
        product, tmpl = self._product()
        order = self.env['shopify.order'].create({
            'name': '#7700', 'instance_id': self.instance.id, 'shopify_order_id': '7700',
            'shopify_order_date': fields.Datetime.now()})
        self.env['shopify.order.line'].create({
            'order_id': order.id, 'odoo_product_id': tmpl.id, 'quantity': 30, 'price': 10.0,
            'product_name': 'Repr Widget',
        })
        self.env.flush_all()
        self.env['shopify.replenishment'].scan_instance(self.instance, window_days=30, lead_time_days=14)
        rep = self.env['shopify.replenishment'].search([('odoo_product_id', '=', tmpl.id)])
        self.assertTrue(rep)
        self.assertGreater(rep.avg_daily_sales, 0)


class TestOrderRisk(_Base):
    def test_heuristic_score(self):
        order = self.env['shopify.order'].create({
            'name': '#7800', 'instance_id': self.instance.id, 'shopify_order_id': '7800',
            'risk_level': 'high', 'total_price': 600.0, 'financial_status': 'pending',
            'customer_email': 'new@buyer.com',
        })
        order.assess_risk()
        # 60 (high) + 20 (>500) + 10 (pending) + 15 (new) capped 100
        self.assertEqual(order.risk_score, 100)


class TestFieldMapping(_Base):
    def test_generate_metafields_from_mapping(self):
        product, tmpl = self._product()
        field = self.env['ir.model.fields'].search([
            ('model', '=', 'product.template'), ('name', '=', 'default_code')], limit=1)
        model = self.env['ir.model'].search([('model', '=', 'product.template')], limit=1)
        self.env['shopify.metafield.mapping'].create({
            'instance_id': self.instance.id, 'resource_type': 'product',
            'namespace': 'custom', 'key': 'code', 'direction': 'export',
            'odoo_model_id': model.id, 'odoo_field_id': field.id, 'transform': 'upper',
        })
        tmpl.default_code = 'abc'
        n = self.env['shopify.metafield.mapping'].generate_metafields(product)
        self.assertEqual(n, 1)
        mf = self.env['shopify.metafield'].search([
            ('shopify_product_id', '=', product.id), ('key', '=', 'code')])
        self.assertEqual(mf.value, 'ABC')  # transform applied


class TestMetaobject(_Base):
    def test_push_parses_json(self):
        mo = self.env['shopify.metaobject'].create({
            'instance_id': self.instance.id, 'object_type': 'spec', 'handle': 'h1',
            'fields_json': '[{"key": "material", "value": "cotton"}]',
        })
        with patch.object(type(self.instance), '_graphql_request',
                          return_value={'metaobjectUpsert': {'metaobject': {'id': 'gid://shopify/Metaobject/9'}, 'userErrors': []}}) as m:
            mo.action_push_to_shopify()
        self.assertTrue(m.called)
        self.assertEqual(mo.shopify_gid, 'gid://shopify/Metaobject/9')


class TestMarketPrices(_Base):
    def test_push_requires_pricelist_gid(self):
        from odoo.exceptions import UserError
        market = self.env['shopify.market'].create({
            'name': 'EU', 'instance_id': self.instance.id, 'shopify_market_id': '1'})
        with self.assertRaises(UserError):
            market.action_push_market_prices()
