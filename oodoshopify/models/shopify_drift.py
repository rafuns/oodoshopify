import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyDrift(models.Model):
    """Detected mismatch between Odoo and the last-synced Shopify value.

    Drift = Odoo and Shopify disagree on a field (price, stock). It usually means
    a change was made on one side that never propagated. We surface each one with
    a one-click fix (push Odoo→Shopify or pull Shopify→Odoo).
    """
    _name = 'shopify.drift'
    _description = 'Shopify Sync Drift'
    _order = 'detected_date desc'

    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade', index=True)
    shopify_product_id = fields.Many2one('shopify.product', string='Product', ondelete='cascade')
    variant_id = fields.Many2one('shopify.product.variant', string='Variant', ondelete='cascade')
    drift_type = fields.Selection([
        ('price', 'Price'),
        ('inventory', 'Inventory'),
    ], string='Field', required=True)
    odoo_value = fields.Float(string='Odoo Value')
    shopify_value = fields.Float(string='Shopify Value')
    difference = fields.Float(string='Difference', compute='_compute_difference', store=True)
    state = fields.Selection([
        ('open', 'Open'),
        ('resolved', 'Resolved'),
        ('ignored', 'Ignored'),
    ], default='open', index=True)
    detected_date = fields.Datetime(string='Detected', default=fields.Datetime.now)

    _unique_open_drift = models.Constraint(
        'UNIQUE(variant_id, drift_type)',
        'Only one open drift per variant/field.',
    )

    @api.depends('odoo_value', 'shopify_value')
    def _compute_difference(self):
        for rec in self:
            rec.difference = (rec.odoo_value or 0) - (rec.shopify_value or 0)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------
    @api.model
    def scan_instance(self, instance, threshold=0.01):
        """Compare each synced variant's Odoo value with the last-synced Shopify
        value and record/refresh drifts. Returns the number of open drifts."""
        Drift = self.sudo()
        products = self.env['shopify.product'].search([
            ('instance_id', '=', instance.id),
            ('shopify_gid', '!=', False),
        ])
        seen_keys = set()
        for product in products:
            for variant in product.variant_ids:
                odoo_variant = variant.odoo_variant_id
                if not odoo_variant:
                    continue
                checks = [
                    ('price', odoo_variant.lst_price, variant.price),
                    ('inventory', odoo_variant.qty_available, variant.inventory_quantity),
                ]
                for dtype, odoo_val, shop_val in checks:
                    if abs((odoo_val or 0) - (shop_val or 0)) <= threshold:
                        continue
                    seen_keys.add((variant.id, dtype))
                    existing = Drift.search([
                        ('variant_id', '=', variant.id),
                        ('drift_type', '=', dtype),
                    ], limit=1)
                    vals = {
                        'instance_id': instance.id,
                        'shopify_product_id': product.id,
                        'variant_id': variant.id,
                        'drift_type': dtype,
                        'odoo_value': odoo_val,
                        'shopify_value': shop_val,
                        'state': 'open',
                        'detected_date': fields.Datetime.now(),
                    }
                    if existing:
                        if existing.state != 'ignored':
                            existing.write(vals)
                    else:
                        Drift.create(vals)
        # Auto-resolve drifts that no longer differ
        stale = Drift.search([
            ('instance_id', '=', instance.id), ('state', '=', 'open')])
        for d in stale:
            if (d.variant_id.id, d.drift_type) not in seen_keys:
                d.state = 'resolved'
        return Drift.search_count([('instance_id', '=', instance.id), ('state', '=', 'open')])

    @api.model
    def cron_scan_drift(self):
        for instance in self.env['shopify.instance'].search([('state', '=', 'connected')]):
            try:
                self.scan_instance(instance)
            except Exception as e:
                _logger.error('Drift scan failed for %s: %s', instance.name, e)

    # ------------------------------------------------------------------
    # One-click fixes
    # ------------------------------------------------------------------
    def action_fix_push(self):
        """Push the Odoo value up to Shopify (Odoo wins)."""
        for rec in self:
            if rec.drift_type == 'price':
                rec.shopify_product_id.action_push_prices()
            elif rec.drift_type == 'inventory':
                rec.shopify_product_id.action_sync_inventory()
            rec.state = 'resolved'

    def action_fix_pull(self):
        """Pull the Shopify value down into Odoo (Shopify wins)."""
        for rec in self:
            odoo_variant = rec.variant_id.odoo_variant_id
            if rec.drift_type == 'price' and odoo_variant:
                odoo_variant.write({'list_price': rec.shopify_value})
            rec.state = 'resolved'

    def action_ignore(self):
        self.write({'state': 'ignored'})
