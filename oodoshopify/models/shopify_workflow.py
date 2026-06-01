import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifyWorkflow(models.Model):
    """
    Per-instance automation rules that fire after a Shopify order
    is imported and a sale order is created.
    """
    _name = 'shopify.workflow'
    _description = 'Shopify Order Workflow'
    _rec_name = 'instance_id'

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance',
        required=True, ondelete='cascade', index=True,
    )

    # ── Confirmation ─────────────────────────────────────────────────────────
    auto_confirm_order = fields.Boolean(
        string='Auto-Confirm Sale Order',
        default=True,
        help='Automatically confirm (validate) the sale order when imported.',
    )

    # ── Delivery ─────────────────────────────────────────────────────────────
    auto_create_delivery = fields.Boolean(
        string='Auto-Validate Delivery',
        default=False,
        help='Immediately validate the delivery (stock.picking) after order confirmation.',
    )
    picking_policy = fields.Selection([
        ('direct', 'Ship products as soon as available'),
        ('one', 'Ship all products at once'),
    ], string='Shipping Policy', default='direct')

    # ── Invoice ──────────────────────────────────────────────────────────────
    auto_create_invoice = fields.Boolean(
        string='Auto-Create & Post Invoice',
        default=False,
        help='Create and post an invoice when the delivery is done.',
    )
    invoice_date_is_order_date = fields.Boolean(
        string='Use Shopify Order Date as Invoice Date',
        default=True,
    )

    # ── Payment ──────────────────────────────────────────────────────────────
    auto_register_payment = fields.Boolean(
        string='Auto-Register Payment',
        default=False,
        help='Register payment on the invoice when Shopify financial status is "paid".',
    )
    payment_journal_id = fields.Many2one(
        'account.journal',
        string='Payment Journal',
        domain=[('type', 'in', ['bank', 'cash'])],
        help='Journal used when auto-registering payment.',
    )

    # ── Auto Mark Paid ───────────────────────────────────────────────────────
    auto_mark_paid = fields.Boolean(
        string='Auto Mark Paid on Shopify',
        default=False,
        help='When the linked Odoo invoice is fully paid, automatically '
             'push "Mark as Paid" to Shopify. Checked every hour by cron.',
    )

    # ── Email Notifications ──────────────────────────────────────────────────
    suppress_shopify_emails = fields.Boolean(
        string='Suppress Shopify Native Emails',
        default=False,
        help='When enabled, Odoo sends all transactional emails instead of Shopify. '
             'Sets notifyCustomer=false on Shopify mutations (fulfillment, cancellation).',
    )

    # Customer-facing notifications
    send_order_confirmation = fields.Boolean(
        string='Send Order Confirmation', default=True,
        help='Email customer when a sale order is created from their Shopify order.',
    )
    send_shipping_notification = fields.Boolean(
        string='Send Shipping Notification', default=True,
        help='Email customer when tracking is pushed to Shopify.',
    )
    send_cancellation_email = fields.Boolean(
        string='Send Cancellation Email', default=True,
        help='Email customer when their order is cancelled from Odoo.',
    )
    send_refund_email = fields.Boolean(
        string='Send Refund Email', default=True,
        help='Email customer when a credit note / refund is created.',
    )
    send_pickup_ready_email = fields.Boolean(
        string='Send Pickup Ready Email', default=True,
        help='Email customer when their pickup order is marked ready.',
    )
    send_abandoned_checkout_email = fields.Boolean(
        string='Send Abandoned Checkout Recovery Email', default=False,
        help='Email the customer a recovery link when an abandoned checkout lead is created.',
    )

    # Internal alerts
    send_low_stock_alert = fields.Boolean(
        string='Send Low Stock Alert (Internal)', default=False,
        help='Email internal users when a product variant hits the low stock threshold.',
    )
    low_stock_threshold = fields.Integer(
        string='Low Stock Threshold', default=5,
        help='Alert fires when any variant inventory_quantity falls to or below this value.',
    )
    low_stock_notify_user_ids = fields.Many2many(
        'res.users', string='Notify Users (Low Stock)',
        help='These users receive the low stock alert email.',
    )

    # ── Tax ──────────────────────────────────────────────────────────────────
    fiscal_position_id = fields.Many2one(
        'account.fiscal.position',
        string='Fiscal Position',
        help='Applied to sale orders created from this store.',
    )
    tax_handling = fields.Selection([
        ('shopify', 'Use Shopify taxes'),
        ('fiscal', 'Use Odoo fiscal position'),
        ('none',   'No taxes'),
    ], string='Tax Handling', default='shopify',
       help='shopify: map Shopify tax lines to Odoo taxes.\n'
            'fiscal: apply fiscal position only, ignore Shopify taxes.\n'
            'none: clear all taxes from imported lines.')

    # ─────────────────────────────────────────────────────────────────────────
    # Email notification helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _send_notification(self, template_xml_id, record):
        """Send a mail.template email for the given record, silently on error."""
        try:
            template = self.env.ref(f'oodoshopify.{template_xml_id}', raise_if_not_found=False)
            if template and record and record.id:
                template.send_mail(record.id, force_send=True)
                _logger.info('Notification sent: %s for record %s(%s)',
                             template_xml_id, record._name, record.id)
        except Exception as e:
            _logger.error('Notification send failed (%s): %s', template_xml_id, e)

    def notify_order_confirmation(self, shopify_order):
        if self.send_order_confirmation:
            self._send_notification('email_template_order_confirmation', shopify_order)

    def notify_order_shipped(self, shopify_order):
        if self.send_shipping_notification:
            self._send_notification('email_template_order_shipped', shopify_order)

    def notify_order_cancelled(self, shopify_order):
        if self.send_cancellation_email:
            self._send_notification('email_template_order_cancelled', shopify_order)

    def notify_refund_processed(self, shopify_refund):
        if self.send_refund_email:
            self._send_notification('email_template_refund_processed', shopify_refund)

    def notify_pickup_ready(self, shopify_order):
        if self.send_pickup_ready_email:
            self._send_notification('email_template_pickup_ready', shopify_order)

    def notify_abandoned_checkout(self, checkout):
        if self.send_abandoned_checkout_email:
            self._send_notification('email_template_abandoned_checkout', checkout)

    def notify_low_stock(self, shopify_product):
        if not self.send_low_stock_alert:
            return
        try:
            template = self.env.ref('oodoshopify.email_template_low_stock_alert',
                                     raise_if_not_found=False)
            if not template:
                return
            recipients = self.low_stock_notify_user_ids.mapped('partner_id')
            if not recipients:
                recipients = self.env.user.partner_id
            for partner in recipients:
                template.with_context(default_partner_ids=[(4, partner.id)]).send_mail(
                    shopify_product.id, force_send=True, email_values={'email_to': partner.email}
                )
        except Exception as e:
            _logger.error('Low stock notification failed: %s', e)

    # ─────────────────────────────────────────────────────────────────────────
    # Execution
    # ─────────────────────────────────────────────────────────────────────────

    def run_workflow(self, sale_order, shopify_order):
        """
        Execute all enabled automation steps for a newly-created sale order.
        Called from ShopifyOrder.action_create_sale_order() after the SO is saved.
        """
        self.ensure_one()
        so = sale_order

        # Step 1 – Apply fiscal position / tax overrides
        self._apply_taxes(so)

        # Step 2 – Confirm
        if self.auto_confirm_order and so.state == 'draft':
            try:
                so.action_confirm()
                _logger.info('Workflow: confirmed SO %s', so.name)
            except Exception as e:
                _logger.error('Workflow: SO confirm failed for %s: %s', so.name, e)
                return

        # Step 3 – Validate delivery
        if self.auto_create_delivery and so.state == 'sale':
            self._validate_deliveries(so)

        # Step 4 – Create & post invoice
        invoice = False
        if self.auto_create_invoice and so.state == 'sale':
            invoice = self._create_invoice(so, shopify_order)

        # Step 5 – Register payment (only if paid on Shopify)
        if (self.auto_register_payment
                and invoice
                and shopify_order.financial_status == 'paid'):
            self._register_payment(invoice, shopify_order)

    def _apply_taxes(self, so):
        """Apply fiscal position or clear taxes based on tax_handling setting."""
        if self.tax_handling == 'none':
            so.order_line.write({'tax_ids': [(5, 0, 0)]})
        elif self.tax_handling == 'fiscal' and self.fiscal_position_id:
            so.fiscal_position_id = self.fiscal_position_id
            for line in so.order_line:
                new_taxes = self.fiscal_position_id.map_tax(line.tax_ids)
                line.tax_ids = new_taxes
        # 'shopify' taxes are mapped in shopify_order._sync_order_lines

    def _validate_deliveries(self, so):
        pickings = so.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
        for picking in pickings:
            try:
                # Set immediate transfer quantities
                for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
                    move.quantity = move.product_uom_qty
                picking.button_validate()
                _logger.info('Workflow: validated picking %s', picking.name)
            except Exception as e:
                _logger.error('Workflow: delivery validation failed for %s: %s', picking.name, e)

    def _create_invoice(self, so, shopify_order):
        try:
            so._create_invoices()
            invoice = so.invoice_ids.filtered(
                lambda inv: inv.move_type == 'out_invoice' and inv.state == 'draft'
            )[:1]
            if not invoice:
                return False
            if self.invoice_date_is_order_date and shopify_order.shopify_order_date:
                invoice.invoice_date = shopify_order.shopify_order_date.date()
            invoice.action_post()
            _logger.info('Workflow: posted invoice %s', invoice.name)
            return invoice
        except Exception as e:
            _logger.error('Workflow: invoice creation failed for SO %s: %s', so.name, e)
            return False

    def _register_payment(self, invoice, shopify_order):
        if not self.payment_journal_id:
            _logger.warning('Workflow: auto_register_payment enabled but no journal set.')
            return
        if invoice.payment_state in ('paid', 'in_payment'):
            return
        try:
            payment_vals = {
                'journal_id': self.payment_journal_id.id,
                'amount': invoice.amount_residual,
                'payment_date': (shopify_order.shopify_order_date.date()
                                 if shopify_order.shopify_order_date else fields.Date.today()),
                'ref': shopify_order.name,
            }
            invoice.action_register_payment().with_context(
                active_model='account.move',
                active_ids=invoice.ids,
                active_id=invoice.id,
                default_journal_id=self.payment_journal_id.id,
                default_amount=invoice.amount_residual,
                default_payment_date=payment_vals['payment_date'],
                default_ref=shopify_order.name,
            ).create(payment_vals).action_create_payments()
            _logger.info('Workflow: registered payment on invoice %s', invoice.name)
        except Exception as e:
            _logger.error('Workflow: payment registration failed for %s: %s', invoice.name, e)
