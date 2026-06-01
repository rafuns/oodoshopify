from odoo import models, fields


class ShopifyPricelistMap(models.Model):
    """Maps a Shopify store currency to a specific Odoo pricelist.

    Example: for a store that sells in USD and EUR, you can map:
      - USD → USD Pricelist
      - EUR → EUR Pricelist

    When an order arrives in EUR, Odoo will use the EUR Pricelist automatically.
    When pushing prices, the correct pricelist price is converted to the
    Shopify store's primary currency before being sent.
    """
    _name = 'shopify.pricelist.map'
    _description = 'Shopify Currency → Pricelist Mapping'
    _order = 'instance_id, currency_id'

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance',
        required=True, ondelete='cascade', index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        required=True,
        help='The order/market currency this mapping applies to.',
    )
    pricelist_id = fields.Many2one(
        'product.pricelist', string='Pricelist',
        required=True,
        help='Odoo pricelist to use for orders/prices in this currency.',
    )
    pricelist_currency_id = fields.Many2one(
        related='pricelist_id.currency_id',
        string='Pricelist Currency',
        readonly=True,
    )
    note = fields.Char(string='Notes', help='e.g. "EU market", "Canadian store"')
