from odoo import models, fields, _
from odoo.exceptions import UserError


class ShopifyStoreCreditWizard(models.TransientModel):
    _name = 'shopify.store.credit.wizard'
    _description = 'Adjust Store Credit'

    customer_id = fields.Many2one('shopify.customer', string='Customer', required=True)
    current_balance = fields.Float(related='customer_id.store_credit_balance', string='Current Balance')
    operation = fields.Selection([
        ('credit', 'Add Credit'),
        ('debit',  'Deduct Credit'),
    ], string='Operation', default='credit', required=True)
    amount = fields.Float(string='Amount', required=True)

    def action_confirm(self):
        self.ensure_one()
        if self.amount <= 0:
            raise UserError(_('Amount must be greater than zero.'))
        self.customer_id._adjust_store_credit(self.amount, credit=(self.operation == 'credit'))
        return {'type': 'ir.actions.act_window_close'}
