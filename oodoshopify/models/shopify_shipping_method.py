from odoo import models, fields, api, _


class ShopifyShippingMethod(models.Model):
    """
    Maps a Shopify shipping line title to an Odoo delivery carrier.
    When an order is imported, the shipping line is matched and the
    corresponding carrier is applied to the sale order.
    """
    _name = 'shopify.shipping.method'
    _description = 'Shopify → Odoo Shipping Method Mapping'
    _order = 'instance_id, shopify_shipping_title'

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance',
        required=True, ondelete='cascade',
    )
    shopify_shipping_title = fields.Char(
        string='Shopify Shipping Title',
        required=True,
        help='Title as it appears in Shopify (e.g. "Standard Shipping", "Free Shipping").',
    )
    odoo_carrier_id = fields.Many2one(
        'delivery.carrier', string='Odoo Carrier',
        help='Mapped delivery carrier in Odoo.',
    )
    add_shipping_line = fields.Boolean(
        string='Add Shipping Line to Sale Order',
        default=True,
        help='When checked, a sale order line for shipping cost is created.',
    )
    shopify_shipping_code = fields.Char(
        string='Shopify Code',
        help='Optional: Shopify shipping code for more precise matching.',
    )

    _unique_title_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_shipping_title)',
        'A mapping for this shipping title already exists on this instance.',
    )

    @api.model
    def get_carrier_for_title(self, instance, title, code=None):
        """Return the matched carrier (or False) for a given Shopify shipping title."""
        # Try exact code match first
        if code:
            mapping = self.search([
                ('instance_id', '=', instance.id),
                ('shopify_shipping_code', '=', code),
            ], limit=1)
            if mapping:
                return mapping

        # Fallback: case-insensitive title match
        return self.search([
            ('instance_id', '=', instance.id),
            ('shopify_shipping_title', '=ilike', title),
        ], limit=1)
