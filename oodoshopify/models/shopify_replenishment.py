import logging
from datetime import timedelta
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyReplenishment(models.Model):
    """Demand forecast: sell-through from Shopify orders → days of cover and a
    suggested reorder quantity, with one-click Odoo reordering-rule creation."""
    _name = 'shopify.replenishment'
    _description = 'Shopify Replenishment Suggestion'
    _order = 'days_cover asc'

    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade', index=True)
    shopify_product_id = fields.Many2one('shopify.product', string='Product', ondelete='cascade')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    avg_daily_sales = fields.Float(string='Avg Daily Sales', readonly=True)
    qty_available = fields.Float(string='On Hand', readonly=True)
    lead_time_days = fields.Integer(string='Lead Time (days)', default=14)
    days_cover = fields.Float(string='Days of Cover', readonly=True)
    suggested_qty = fields.Float(string='Suggested Reorder', readonly=True)
    state = fields.Selection([
        ('ok', 'OK'),
        ('reorder', 'Reorder Soon'),
        ('urgent', 'Urgent'),
    ], default='ok', index=True)
    computed_on = fields.Datetime(readonly=True)

    _unique_repl = models.Constraint(
        'UNIQUE(odoo_product_id, instance_id)',
        'One replenishment row per product/store.',
    )

    @api.model
    def scan_instance(self, instance, window_days=30, lead_time_days=14):
        """Recompute suggestions for all of an instance's products."""
        since = fields.Datetime.now() - timedelta(days=window_days)
        lines = self.env['shopify.order.line'].search([
            ('order_id.instance_id', '=', instance.id),
            ('order_id.shopify_order_date', '>=', since),
            ('odoo_product_id', '!=', False),
        ])
        sold = {}
        for line in lines:
            pid = line.odoo_product_id.id
            sold[pid] = sold.get(pid, 0) + (line.quantity or 0)

        products = self.env['shopify.product'].search([
            ('instance_id', '=', instance.id), ('odoo_product_id', '!=', False)])
        seen = set()
        for product in products:
            for variant in product.variant_ids:
                odoo_p = variant.odoo_variant_id  # product.product
                if not odoo_p or odoo_p.id in seen:
                    continue
                seen.add(odoo_p.id)
                qty_sold = sold.get(odoo_p.id, 0)
                avg = qty_sold / float(window_days)
                on_hand = odoo_p.qty_available
                days_cover = (on_hand / avg) if avg > 0 else 9999.0
                # Reorder up to lead-time + window buffer
                target = avg * (lead_time_days + window_days)
                suggested = max(0.0, round(target - on_hand))
                if avg <= 0:
                    state = 'ok'
                elif days_cover <= lead_time_days:
                    state = 'urgent'
                elif days_cover <= lead_time_days * 2:
                    state = 'reorder'
                else:
                    state = 'ok'
                vals = {
                    'instance_id': instance.id,
                    'shopify_product_id': product.id,
                    'odoo_product_id': odoo_p.id,
                    'avg_daily_sales': round(avg, 3),
                    'qty_available': on_hand,
                    'lead_time_days': lead_time_days,
                    'days_cover': round(days_cover, 1),
                    'suggested_qty': suggested,
                    'state': state,
                    'computed_on': fields.Datetime.now(),
                }
                existing = self.search([
                    ('instance_id', '=', instance.id), ('odoo_product_id', '=', odoo_p.id)], limit=1)
                if existing:
                    existing.write(vals)
                else:
                    self.create(vals)
        return True

    @api.model
    def cron_scan_replenishment(self):
        for instance in self.env['shopify.instance'].search([('state', '=', 'connected')]):
            try:
                self.scan_instance(instance)
            except Exception as e:
                _logger.error('Replenishment scan failed for %s: %s', instance.name, e)

    def action_create_orderpoint(self):
        """Create/refresh an Odoo reordering rule (min/max) from the suggestion."""
        Orderpoint = self.env['stock.warehouse.orderpoint']
        created = self.env['stock.warehouse.orderpoint']
        for rec in self:
            if not rec.odoo_product_id:
                continue
            existing = Orderpoint.search([('product_id', '=', rec.odoo_product_id.id)], limit=1)
            min_qty = rec.avg_daily_sales * rec.lead_time_days
            max_qty = rec.avg_daily_sales * (rec.lead_time_days * 2)
            vals = {'product_min_qty': round(min_qty), 'product_max_qty': round(max(max_qty, min_qty + 1))}
            if existing:
                existing.write(vals)
                created |= existing
            else:
                vals['product_id'] = rec.odoo_product_id.id
                created |= Orderpoint.create(vals)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reordering Rules'),
            'res_model': 'stock.warehouse.orderpoint',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created.ids)],
        }
