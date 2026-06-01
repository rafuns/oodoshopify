"""Tests for shopify.refund — webhook creation, credit note generation."""
from odoo.exceptions import UserError
from .common import ShopifyTestBase, SAMPLE_REFUND_WEBHOOK


class TestRefundWebhook(ShopifyTestBase):

    def test_create_from_webhook_creates_record(self):
        shopify_order = self._make_shopify_order()
        self.env['shopify.refund'].create_from_webhook(self.instance, SAMPLE_REFUND_WEBHOOK)
        refund = self.env['shopify.refund'].search([
            ('instance_id', '=', self.instance.id),
            ('shopify_refund_id', '=', '77'),
        ])
        self.assertEqual(len(refund), 1)

    def test_create_from_webhook_sets_order_link(self):
        shopify_order = self._make_shopify_order()
        self.env['shopify.refund'].create_from_webhook(self.instance, SAMPLE_REFUND_WEBHOOK)
        refund = self.env['shopify.refund'].search([('shopify_refund_id', '=', '77')])
        self.assertEqual(refund.shopify_order_id, shopify_order)

    def test_create_from_webhook_idempotent(self):
        """Calling with same payload twice should not create duplicate."""
        shopify_order = self._make_shopify_order()
        self.env['shopify.refund'].create_from_webhook(self.instance, SAMPLE_REFUND_WEBHOOK)
        self.env['shopify.refund'].create_from_webhook(self.instance, SAMPLE_REFUND_WEBHOOK)
        count = self.env['shopify.refund'].search_count([('shopify_refund_id', '=', '77')])
        self.assertEqual(count, 1)

    def test_missing_order_id_returns_none(self):
        bad_payload = {'id': 88}  # no order_id
        result = self.env['shopify.refund'].create_from_webhook(self.instance, bad_payload)
        self.assertIsNone(result)

    def test_creates_refund_with_note(self):
        self._make_shopify_order()
        self.env['shopify.refund'].create_from_webhook(self.instance, SAMPLE_REFUND_WEBHOOK)
        refund = self.env['shopify.refund'].search([('shopify_refund_id', '=', '77')])
        self.assertEqual(refund.note, 'Customer changed mind')


class TestCreditNoteCreation(ShopifyTestBase):

    def _make_posted_invoice(self, partner, amount=59.99):
        """Create a posted customer invoice for testing."""
        if not self.env['account.journal'].search([('type', '=', 'sale')], limit=1):
            self.skipTest('No sale journal — accounting chart not configured in this database.')
        account = self.env['account.account'].search([
            ('account_type', '=', 'asset_receivable'),
            ('company_ids', 'in', self.company.id),
        ], limit=1)
        income_account = self.env['account.account'].search([
            ('account_type', '=', 'income'),
            ('company_ids', 'in', self.company.id),
        ], limit=1)
        journal = self.env['account.journal'].search([
            ('type', '=', 'sale'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'journal_id': journal.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test Sale',
                'quantity': 1,
                'price_unit': amount,
                'account_id': income_account.id,
            })],
        })
        move.action_post()
        return move

    def test_credit_note_requires_sale_order(self):
        refund = self.env['shopify.refund'].create({
            'name': 'Refund/Test/1',
            'instance_id': self.instance.id,
            'shopify_refund_id': '999',
            'total_refunded': 29.99,
        })
        with self.assertRaises(UserError):
            refund.action_create_credit_note()

    def test_credit_note_requires_posted_invoice(self):
        shopify_order = self._make_shopify_order()
        so = self.env['sale.order'].create({
            'partner_id': self.env['res.partner'].create({'name': 'Test'}).id,
        })
        shopify_order.odoo_sale_order_id = so
        refund = self.env['shopify.refund'].create({
            'name': 'Refund/Test/2',
            'instance_id': self.instance.id,
            'shopify_refund_id': '998',
            'shopify_order_id': shopify_order.id,
            'total_refunded': 29.99,
        })
        with self.assertRaises(UserError):
            refund.action_create_credit_note()

    def test_duplicate_credit_note_raises(self):
        partner = self.env['res.partner'].create({'name': 'Test Partner', 'email': 't@t.com'})
        invoice = self._make_posted_invoice(partner)
        so = self.env['sale.order'].create({'partner_id': partner.id})
        so.invoice_ids = [(4, invoice.id)]
        shopify_order = self._make_shopify_order()
        shopify_order.odoo_sale_order_id = so

        refund = self.env['shopify.refund'].create({
            'name': 'Refund/Test/3',
            'instance_id': self.instance.id,
            'shopify_refund_id': '997',
            'shopify_order_id': shopify_order.id,
            'total_refunded': 29.99,
        })
        refund.action_create_credit_note()
        # Second call should raise
        with self.assertRaises(UserError):
            refund.action_create_credit_note()


class TestRefundLineSubtotal(ShopifyTestBase):

    def test_subtotal_computed(self):
        line = self.env['shopify.refund.line'].new({
            'quantity': 3,
            'unit_price': 10.00,
        })
        self.assertAlmostEqual(line.subtotal, 30.00)

    def test_zero_qty_zero_subtotal(self):
        line = self.env['shopify.refund.line'].new({
            'quantity': 0,
            'unit_price': 10.00,
        })
        self.assertAlmostEqual(line.subtotal, 0.0)
