from odoo import models, fields, api, _


class ShopifyDryRunWizard(models.TransientModel):
    _name = 'shopify.dry.run.wizard'
    _description = 'Shopify Dry-Run Price Preview'

    product_ids = fields.Many2many('shopify.product', string='Products')
    line_ids = fields.One2many('shopify.dry.run.line', 'wizard_id', string='Changes')
    change_count = fields.Integer(compute='_compute_change_count')

    @api.depends('line_ids.will_change')
    def _compute_change_count(self):
        for wiz in self:
            wiz.change_count = len(wiz.line_ids.filtered('will_change'))

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        products = self.env['shopify.product'].browse(self.env.context.get('active_ids', []))
        products = products.exists() or self.env['shopify.product']
        res['product_ids'] = [(6, 0, products.ids)]
        lines = []
        for product in products:
            for variant in product.variant_ids:
                odoo_v = variant.odoo_variant_id
                new_price = odoo_v.lst_price if odoo_v else 0.0
                current = variant.price or 0.0
                lines.append((0, 0, {
                    'product_name': product.name,
                    'variant_name': variant.title or product.name,
                    'current_price': current,
                    'new_price': new_price,
                    'will_change': abs(new_price - current) > 0.01,
                }))
        res['line_ids'] = lines
        return res

    def action_apply(self):
        """Actually push prices for the products that have changes."""
        self.ensure_one()
        to_push = self.line_ids.filtered('will_change').mapped('product_name')
        products = self.product_ids.filtered(lambda p: p.name in to_push)
        (products or self.product_ids).action_push_prices()
        return {'type': 'ir.actions.act_window_close'}


class ShopifyDryRunLine(models.TransientModel):
    _name = 'shopify.dry.run.line'
    _description = 'Shopify Dry-Run Preview Line'

    wizard_id = fields.Many2one('shopify.dry.run.wizard', ondelete='cascade')
    product_name = fields.Char()
    variant_name = fields.Char()
    current_price = fields.Float(string='Current (Shopify)')
    new_price = fields.Float(string='New (Odoo)')
    delta = fields.Float(string='Δ', compute='_compute_delta')
    will_change = fields.Boolean(string='Changes')

    @api.depends('current_price', 'new_price')
    def _compute_delta(self):
        for line in self:
            line.delta = (line.new_price or 0) - (line.current_price or 0)
