from odoo import models, fields


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
