"""Tests for the differentiator features: drift, margin, AI content, dry-run."""
from unittest.mock import patch
from .common import ShopifyTestBase


class _ProductBase(ShopifyTestBase):
    def _make_product(self, list_price=100.0, cost=40.0, shopify_price=100.0):
        odoo_tmpl = self.env['product.product'].create({
            'name': 'Drift Widget', 'type': 'consu',
            'list_price': list_price, 'standard_price': cost,
        })
        product = self.env['shopify.product'].create({
            'name': 'Drift Widget',
            'instance_id': self.instance.id,
            'shopify_product_id': '800',
            'shopify_gid': 'gid://shopify/Product/800',
            'odoo_product_id': odoo_tmpl.id,
        })
        variant = self.env['shopify.product.variant'].create({
            'shopify_product_id': product.id,
            'shopify_variant_gid': 'gid://shopify/ProductVariant/801',
            'shopify_variant_id': '801',
            'odoo_variant_id': odoo_tmpl.id,
            'price': shopify_price,
        })
        return product, variant, odoo_tmpl


class TestDrift(_ProductBase):

    def test_scan_detects_price_drift(self):
        # Odoo price 120, Shopify cached 100 → drift
        self._make_product(list_price=120.0, shopify_price=100.0)
        open_count = self.env['shopify.drift'].scan_instance(self.instance)
        drift = self.env['shopify.drift'].search([
            ('instance_id', '=', self.instance.id), ('drift_type', '=', 'price')])
        self.assertTrue(drift)
        self.assertAlmostEqual(drift.odoo_value, 120.0)
        self.assertAlmostEqual(drift.shopify_value, 100.0)

    def test_no_drift_when_equal(self):
        self._make_product(list_price=100.0, shopify_price=100.0)
        self.env['shopify.drift'].scan_instance(self.instance)
        price_drift = self.env['shopify.drift'].search([
            ('instance_id', '=', self.instance.id),
            ('drift_type', '=', 'price'), ('state', '=', 'open')])
        self.assertFalse(price_drift)

    def test_fix_pull_updates_odoo(self):
        product, variant, tmpl = self._make_product(list_price=120.0, shopify_price=100.0)
        self.env['shopify.drift'].scan_instance(self.instance)
        drift = self.env['shopify.drift'].search([('drift_type', '=', 'price')], limit=1)
        drift.action_fix_pull()
        self.assertAlmostEqual(tmpl.list_price, 100.0)
        self.assertEqual(drift.state, 'resolved')


class TestMargin(_ProductBase):

    def test_margin_computed_on_line(self):
        order = self.env['shopify.order'].create({
            'name': '#7001', 'instance_id': self.instance.id, 'shopify_order_id': '7001'})
        _, _, tmpl = self._make_product(list_price=100.0, cost=40.0)
        line = self.env['shopify.order.line'].create({
            'order_id': order.id, 'product_name': 'Drift Widget',
            'odoo_product_id': tmpl.id, 'quantity': 2, 'price': 100.0,
        })
        self.assertAlmostEqual(line.margin_revenue, 200.0)
        self.assertAlmostEqual(line.margin_cost, 80.0)
        self.assertAlmostEqual(line.margin_value, 120.0)
        self.assertAlmostEqual(line.margin_pct, 60.0)


class TestAIContent(_ProductBase):

    def test_generate_writes_fields(self):
        product, _, _ = self._make_product()
        wiz = self.env['shopify.ai.content.wizard'].create({
            'instance_id': self.instance.id,
            'product_ids': [(6, 0, product.ids)],
            'gen_seo': True, 'gen_tags': True, 'gen_description': False,
        })
        fake = {'seo_title': 'Best Widget', 'seo_description': 'Buy now',
                'tags': 'widget, gadget'}
        with patch.object(type(self.instance), 'ai_generate_product_content', return_value=fake):
            wiz.action_generate()
        self.assertEqual(product.seo_title, 'Best Widget')
        self.assertEqual(product.tags, 'widget, gadget')

    def test_no_fields_selected_raises(self):
        product, _, _ = self._make_product()
        wiz = self.env['shopify.ai.content.wizard'].create({
            'instance_id': self.instance.id,
            'product_ids': [(6, 0, product.ids)],
            'gen_seo': False, 'gen_tags': False, 'gen_description': False,
        })
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            wiz.action_generate()


class TestDryRun(_ProductBase):

    def test_preview_flags_change(self):
        product, _, _ = self._make_product(list_price=130.0, shopify_price=100.0)
        wiz = self.env['shopify.dry.run.wizard'].with_context(
            active_ids=product.ids).create({})
        self.assertEqual(wiz.change_count, 1)
        line = wiz.line_ids
        self.assertTrue(line.will_change)
        self.assertAlmostEqual(line.delta, 30.0)


class TestCustomerMerge(ShopifyTestBase):

    def _cust(self, gid):
        return self.env['shopify.customer'].create({
            'name': 'Customer ' + gid.split('/')[-1],
            'instance_id': self.instance.id,
            'shopify_customer_id': gid.split('/')[-1],
            'shopify_customer_gid': gid,
            'first_name': 'C' + gid.split('/')[-1],
        })

    def test_merge_calls_api_and_unlinks_dup(self):
        keeper = self._cust('gid://shopify/Customer/1')
        dup = self._cust('gid://shopify/Customer/2')
        from unittest.mock import patch
        with patch.object(type(self.instance), '_graphql_request',
                          return_value={'customerMerge': {'job': {'id': 'j', 'done': False}, 'userErrors': []}}) as m:
            dup.merge_into_on_shopify(keeper)
        self.assertTrue(m.called)
        self.assertFalse(dup.exists())
        self.assertTrue(keeper.exists())

    def test_merge_same_record_raises(self):
        from odoo.exceptions import UserError
        keeper = self._cust('gid://shopify/Customer/3')
        with self.assertRaises(UserError):
            keeper.merge_into_on_shopify(keeper)
