"""Tests for shopify.draft.order."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase, SAMPLE_DRAFT_ORDER_NODE


class TestDraftOrderUpsert(ShopifyTestBase):

    def test_upsert_creates_record(self):
        self.env['shopify.draft.order']._upsert_draft(self.instance, SAMPLE_DRAFT_ORDER_NODE)
        rec = self.env['shopify.draft.order'].search([('shopify_draft_id', '=', '3001')])
        self.assertEqual(len(rec), 1)

    def test_upsert_fields(self):
        self.env['shopify.draft.order']._upsert_draft(self.instance, SAMPLE_DRAFT_ORDER_NODE)
        rec = self.env['shopify.draft.order'].search([('shopify_draft_id', '=', '3001')])
        self.assertEqual(rec.customer_email, 'bob@example.com')
        self.assertAlmostEqual(rec.total_price, 120.0)
        self.assertEqual(rec.status, 'open')
        self.assertTrue(rec.invoice_url)

    def test_upsert_idempotent(self):
        self.env['shopify.draft.order']._upsert_draft(self.instance, SAMPLE_DRAFT_ORDER_NODE)
        self.env['shopify.draft.order']._upsert_draft(self.instance, SAMPLE_DRAFT_ORDER_NODE)
        self.assertEqual(
            self.env['shopify.draft.order'].search_count([('shopify_draft_id', '=', '3001')]), 1
        )


class TestDraftOrderFromSaleOrder(ShopifyTestBase):

    def _make_so(self):
        partner = self.env['res.partner'].create({'name': 'Wholesale Co', 'email': 'wc@example.com'})
        product = self.env['product.product'].create({'name': 'Bulk Item', 'type': 'consu', 'list_price': 10})
        return self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {'product_id': product.id, 'product_uom_qty': 5, 'price_unit': 10})],
        })

    def test_create_from_sale_order(self):
        so = self._make_so()
        draft = self.env['shopify.draft.order'].create_from_sale_order(so, self.instance)
        self.assertEqual(draft.odoo_sale_order_id, so)
        self.assertEqual(len(draft.line_ids), 1)
        self.assertEqual(draft.line_ids.quantity, 5)

    def test_build_input_has_line_items(self):
        so = self._make_so()
        draft = self.env['shopify.draft.order'].create_from_sale_order(so, self.instance)
        inp = draft._build_draft_input()
        self.assertIn('lineItems', inp)
        self.assertEqual(len(inp['lineItems']), 1)

    def test_line_subtotal(self):
        so = self._make_so()
        draft = self.env['shopify.draft.order'].create_from_sale_order(so, self.instance)
        self.assertAlmostEqual(draft.line_ids.subtotal, 50.0)


class TestDraftOrderActions(ShopifyTestBase):

    def test_push_requires_lines(self):
        draft = self.env['shopify.draft.order'].create({
            'name': 'Empty', 'instance_id': self.instance.id,
        })
        with self.assertRaises(UserError):
            draft.action_push_to_shopify()

    def test_send_invoice_requires_gid(self):
        draft = self.env['shopify.draft.order'].create({
            'name': 'NoGid', 'instance_id': self.instance.id,
        })
        with self.assertRaises(UserError):
            draft.action_send_invoice()
