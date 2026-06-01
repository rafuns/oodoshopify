from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ShopifyCancelOrderWizard(models.TransientModel):
    _name = 'shopify.cancel.order.wizard'
    _description = 'Cancel Shopify Order'

    shopify_order_id = fields.Many2one(
        'shopify.order', string='Shopify Order',
        required=True, ondelete='cascade',
    )
    order_name = fields.Char(
        related='shopify_order_id.name', string='Order', readonly=True,
    )
    current_status = fields.Selection(
        related='shopify_order_id.financial_status',
        string='Current Status', readonly=True,
    )
    total_price = fields.Float(
        related='shopify_order_id.total_price', string='Order Total', readonly=True,
    )
    currency = fields.Char(
        related='shopify_order_id.currency', string='Currency', readonly=True,
    )

    reason = fields.Selection([
        ('CUSTOMER',  'Customer requested'),
        ('DECLINED',  'Payment declined'),
        ('FRAUD',     'Fraudulent order'),
        ('INVENTORY', 'Out of stock'),
        ('STAFF',     'Staff cancelled'),
        ('OTHER',     'Other'),
    ], string='Cancellation Reason', default='OTHER', required=True)

    refund = fields.Boolean(
        string='Issue Refund',
        default=True,
        help='Automatically refund the customer when cancelling.',
    )
    restock = fields.Boolean(
        string='Restock Items',
        default=True,
        help='Return line item quantities to Shopify inventory.',
    )
    notify_customer = fields.Boolean(
        string='Notify Customer',
        default=True,
        help='Send a cancellation email to the customer via Shopify.',
    )
    staff_note = fields.Char(
        string='Staff Note',
        help='Internal note added to the Shopify order timeline (not visible to customer).',
    )
    cancel_odoo_order = fields.Boolean(
        string='Also Cancel Odoo Sale Order',
        default=True,
        help='If checked, the linked Odoo sale order will also be cancelled.',
    )
    has_odoo_order = fields.Boolean(
        compute='_compute_has_odoo_order',
    )

    @api.depends('shopify_order_id')
    def _compute_has_odoo_order(self):
        for rec in self:
            rec.has_odoo_order = bool(rec.shopify_order_id.odoo_sale_order_id)

    def action_confirm(self):
        self.ensure_one()
        return self.shopify_order_id.action_cancel_on_shopify(
            reason=self.reason,
            refund=self.refund,
            restock=self.restock,
            notify_customer=self.notify_customer,
            staff_note=self.staff_note or '',
            cancel_odoo_order=self.cancel_odoo_order,
        )
