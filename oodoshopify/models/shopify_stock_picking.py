import logging
from odoo import models, fields

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # Set once this delivery has been pushed to Shopify as a fulfilment, so the
    # same shipment is never fulfilled twice (split / multiple shipments).
    shopify_fulfillment_id = fields.Char(
        string='Shopify Fulfilment GID', readonly=True, copy=False,
        help='GID of the Shopify fulfilment created from this delivery.')

    def _shopify_order_for_picking(self):
        """Return the linked shopify.order for this delivery, if any."""
        self.ensure_one()
        so = self.sale_id
        if not so:
            return self.env['shopify.order']
        return self.env['shopify.order'].search(
            [('odoo_sale_order_id', '=', so.id)], limit=1)

    def _action_done(self):
        res = super()._action_done()
        if self.env.context.get('shopify_skip_push'):
            return res
        for picking in self.filtered(
                lambda p: p.picking_type_code == 'outgoing'
                and not p.shopify_fulfillment_id):
            sh = picking._shopify_order_for_picking()
            if not sh or not sh.shopify_order_gid:
                continue
            wf = self.env['shopify.workflow'].search(
                [('instance_id', '=', sh.instance_id.id)], limit=1)
            if wf and wf.auto_push_fulfillments:
                try:
                    sh.with_context(shopify_skip_push=True)\
                        .action_push_fulfillments_to_shopify()
                except Exception as e:
                    _logger.warning(
                        'Auto-push fulfilment failed for %s: %s', picking.name, e)
        return res
