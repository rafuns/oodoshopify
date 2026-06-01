import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyRepricingRule(models.Model):
    """Stock-aware automatic repricing: mark prices up when stock is scarce,
    down when overstocked — never below a margin floor. Applied to Shopify."""
    _name = 'shopify.repricing.rule'
    _description = 'Shopify Repricing Rule'
    _order = 'sequence, id'

    name = fields.Char(required=True, default='Repricing Rule')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    product_ids = fields.Many2many(
        'shopify.product', string='Products',
        help='Leave empty to apply to all products of the store.')

    trigger = fields.Selection([
        ('low_stock', 'Low stock (scarcity markup)'),
        ('overstock', 'Overstock (clearance markdown)'),
    ], required=True, default='low_stock')
    threshold = fields.Float(
        string='Qty Threshold',
        help='Low stock: trigger when on-hand ≤ this. Overstock: when on-hand ≥ this.')
    adjustment_type = fields.Selection([
        ('percent', 'Percent'),
        ('fixed', 'Fixed amount'),
    ], default='percent', required=True)
    adjustment_value = fields.Float(
        string='Adjustment',
        help='Markup (low stock) or markdown (overstock). Percent or currency amount.')
    min_margin_pct = fields.Float(
        string='Min Margin %', default=0.0,
        help='Never price below cost × (1 + this%). 0 = floor at cost.')
    last_run = fields.Datetime(readonly=True)
    last_count = fields.Integer(string='Last Affected', readonly=True)

    def _targets(self):
        self.ensure_one()
        if self.product_ids:
            return self.product_ids
        return self.env['shopify.product'].search([
            ('instance_id', '=', self.instance_id.id), ('shopify_gid', '!=', False)])

    def _new_price(self, base, cost, markup):
        """Apply the adjustment to base, then enforce the margin floor."""
        if self.adjustment_type == 'percent':
            sign = 1 if markup else -1
            price = base * (1 + sign * self.adjustment_value / 100.0)
        else:
            price = base + self.adjustment_value if markup else base - self.adjustment_value
        floor = (cost or 0.0) * (1 + (self.min_margin_pct or 0.0) / 100.0)
        return round(max(price, floor), 2)

    def apply(self):
        for rule in self:
            markup = rule.trigger == 'low_stock'
            affected = 0
            for product in rule._targets():
                price_map = {}
                for variant in product.variant_ids:
                    odoo_v = variant.odoo_variant_id
                    if not odoo_v or not variant.shopify_variant_gid:
                        continue
                    qty = odoo_v.qty_available
                    hit = (qty <= rule.threshold) if markup else (qty >= rule.threshold)
                    if not hit:
                        continue
                    new_price = rule._new_price(odoo_v.lst_price, odoo_v.standard_price, markup)
                    if abs(new_price - (variant.price or 0)) < 0.01:
                        continue
                    price_map[variant.shopify_variant_gid] = {
                        'price': '{:.2f}'.format(new_price),
                        'compareAtPrice': '{:.2f}'.format(odoo_v.lst_price)
                        if (markup and odoo_v.lst_price > new_price) else None,
                    }
                if price_map:
                    try:
                        product.push_variant_price_map(price_map)
                        affected += len(price_map)
                    except Exception as e:
                        _logger.error('Repricing push failed for %s: %s', product.name, e)
            rule.write({'last_run': fields.Datetime.now(), 'last_count': affected})
        return True

    @api.model
    def cron_apply_repricing(self):
        for rule in self.search([('active', '=', True)]):
            try:
                rule.apply()
            except Exception as e:
                _logger.error('Repricing rule %s failed: %s', rule.id, e)
