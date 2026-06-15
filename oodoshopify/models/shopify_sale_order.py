import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Shopify provenance fields populated when an order is imported from Shopify.
    shopify_instance_id = fields.Many2one(
        'shopify.instance', string='Shopify Store', index=True, copy=False)
    shopify_order_number = fields.Char(
        string='Shopify Order Number', index=True, copy=False,
        help='The original Shopify order number (e.g. ZA1001).')
    sales_order_prefix = fields.Char(
        string='Store Prefix', copy=False,
        help='Store-of-origin identifier from the Shopify store (e.g. ZA, EU).')
    shopify_tags = fields.Char(string='Shopify Tags', copy=False)

    def _shopify_order_for(self):
        """Return the linked shopify.order records for these sale orders."""
        return self.env['shopify.order'].search([('odoo_sale_order_id', 'in', self.ids)])

    def _shopify_workflow(self, instance):
        return self.env['shopify.workflow'].search(
            [('instance_id', '=', instance.id)], limit=1)

    def write(self, vals):
        res = super().write(vals)
        # Auto-push line edits to Shopify when enabled (never during import, which
        # creates lines directly on sale.order.line, not via this write).
        if 'order_line' in vals and not self.env.context.get('shopify_skip_push'):
            for so in self:
                if so.state not in ('sale', 'done'):
                    continue
                sh = self.env['shopify.order'].search(
                    [('odoo_sale_order_id', '=', so.id)], limit=1)
                if not sh or not sh.shopify_order_gid:
                    continue
                wf = self._shopify_workflow(sh.instance_id)
                if wf and wf.auto_push_edits:
                    try:
                        sh.with_context(shopify_skip_push=True).action_sync_lines_to_shopify()
                    except Exception as e:
                        _logger.warning('Auto-push edit failed for %s: %s', so.name, e)
        return res

    def _action_cancel(self):
        res = super()._action_cancel()
        if not self.env.context.get('shopify_skip_push'):
            for so in self:
                sh = self.env['shopify.order'].search(
                    [('odoo_sale_order_id', '=', so.id)], limit=1)
                if not sh or not sh.shopify_order_gid:
                    continue
                wf = self._shopify_workflow(sh.instance_id)
                if wf and wf.auto_push_cancel:
                    try:
                        sh.with_context(shopify_skip_push=True).action_cancel_on_shopify(
                            reason='OTHER', refund=False)
                    except Exception as e:
                        _logger.warning('Auto-push cancel failed for %s: %s', so.name, e)
        return res
