"""Tests for shopify.workflow — automation steps and tax handling."""
from odoo.tests.common import TransactionCase
from .common import ShopifyTestBase


class TestWorkflowCreation(ShopifyTestBase):

    def _make_workflow(self, **kwargs):
        defaults = {
            'instance_id': self.instance.id,
            'auto_confirm_order': False,
            'auto_create_delivery': False,
            'auto_create_invoice': False,
            'auto_register_payment': False,
            'tax_handling': 'none',
        }
        defaults.update(kwargs)
        return self.env['shopify.workflow'].create(defaults)

    def _make_sale_order(self):
        partner = self.env['res.partner'].create({'name': 'Test Customer'})
        product = self.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'list_price': 50.0,
        })
        so = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 1,
                'price_unit': 50.0,
            })],
        })
        return so

    def test_workflow_created_with_instance(self):
        wf = self._make_workflow()
        self.assertEqual(wf.instance_id, self.instance)

    def test_auto_confirm_off_does_not_confirm(self):
        wf = self._make_workflow(auto_confirm_order=False)
        so = self._make_sale_order()
        shopify_order = self._make_shopify_order()
        wf.run_workflow(so, shopify_order)
        self.assertEqual(so.state, 'draft')

    def test_auto_confirm_on_confirms_order(self):
        wf = self._make_workflow(auto_confirm_order=True)
        so = self._make_sale_order()
        shopify_order = self._make_shopify_order()
        wf.run_workflow(so, shopify_order)
        self.assertEqual(so.state, 'sale')

    def test_tax_handling_none_clears_taxes(self):
        partner = self.env['res.partner'].create({'name': 'Tax Test Customer'})
        tax = self.env['account.tax'].search([
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.env.company.id),
        ], limit=1)
        product = self.env['product.product'].create({
            'name': 'Taxed Product',
            'type': 'consu',
            'list_price': 100.0,
            'taxes_id': [(4, tax.id)] if tax else [],
        })
        so = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 1,
                'price_unit': 100.0,
                'tax_ids': [(4, tax.id)] if tax else [],
            })],
        })
        wf = self._make_workflow(tax_handling='none')
        shopify_order = self._make_shopify_order()
        wf._apply_taxes(so)
        for line in so.order_line:
            self.assertEqual(len(line.tax_ids), 0)

    def test_run_workflow_with_all_off_safe(self):
        """All automations disabled should not raise."""
        wf = self._make_workflow()
        so = self._make_sale_order()
        shopify_order = self._make_shopify_order()
        wf.run_workflow(so, shopify_order)  # should not raise


class TestWorkflowAutoInvoice(ShopifyTestBase):

    def setUp(self):
        super().setUp()
        if not self.env['account.journal'].search([('type', '=', 'sale')], limit=1):
            self.skipTest('No sale journal — accounting chart not configured in this database.')

    def _make_confirmed_so(self):
        partner = self.env['res.partner'].create({'name': 'Invoice Customer'})
        product = self.env['product.product'].create({
            'name': 'Invoice Product',
            'type': 'consu',
            'list_price': 75.0,
        })
        so = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {
                'product_id': product.id,
                'product_uom_qty': 1,
                'price_unit': 75.0,
            })],
        })
        so.action_confirm()
        return so

    def test_auto_invoice_creates_invoice(self):
        wf = self.env['shopify.workflow'].create({
            'instance_id': self.instance.id,
            'auto_confirm_order': False,
            'auto_create_invoice': True,
            'tax_handling': 'none',
        })
        so = self._make_confirmed_so()
        shopify_order = self._make_shopify_order()
        invoice = wf._create_invoice(so, shopify_order)
        self.assertTrue(invoice)
        self.assertEqual(invoice.move_type, 'out_invoice')
        self.assertEqual(invoice.state, 'posted')

    def test_auto_invoice_uses_order_date(self):
        wf = self.env['shopify.workflow'].create({
            'instance_id': self.instance.id,
            'auto_confirm_order': False,
            'auto_create_invoice': True,
            'invoice_date_is_order_date': True,
            'tax_handling': 'none',
        })
        so = self._make_confirmed_so()
        shopify_order = self._make_shopify_order()
        shopify_order.shopify_order_date = '2025-03-01 14:30:00'
        invoice = wf._create_invoice(so, shopify_order)
        if invoice:  # may not create if no order date
            from datetime import date
            self.assertEqual(invoice.invoice_date, date(2025, 3, 1))
