import logging
from odoo import models, api

_logger = logging.getLogger(__name__)


class ShopifyStockQuant(models.Model):
    """
    Extends stock.quant to trigger real-time Shopify inventory push
    when on-hand quantities change — for instances with
    realtime_inventory_push = True.

    Only fires when the quant's location is a linked Shopify warehouse's
    lot_stock_id. Uses a lightweight search to avoid performance impact
    on every stock move across the entire Odoo instance.
    """
    _inherit = 'stock.quant'

    def write(self, vals):
        res = super().write(vals)
        if 'quantity' in vals or 'reserved_quantity' in vals:
            self._trigger_shopify_inventory_push()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._trigger_shopify_inventory_push()
        return records

    def _trigger_shopify_inventory_push(self):
        """
        For each changed quant, check if its location maps to a Shopify
        instance with realtime_inventory_push enabled, then push inventory.
        """
        # Collect unique (product, location) pairs to push
        affected_locations = self.mapped('location_id')
        if not affected_locations:
            return

        # Find Shopify locations that map to any of the affected Odoo locations
        shopify_locs = self.env['shopify.location'].search([
            ('odoo_location_id', 'in', affected_locations.ids),
            ('instance_id.realtime_inventory_push', '=', True),
            ('instance_id.state', '=', 'connected'),
            ('is_default', '=', True),
        ])
        if not shopify_locs:
            return

        for shopify_loc in shopify_locs:
            instance = shopify_loc.instance_id
            odoo_location = shopify_loc.odoo_location_id

            # Find products changed in this location
            changed_quants = self.filtered(
                lambda q: q.location_id == odoo_location
            )
            affected_products = changed_quants.mapped('product_id')
            if not affected_products:
                continue

            # Find mapped shopify.product variants for these products
            variants = self.env['shopify.product.variant'].search([
                ('shopify_product_id.instance_id', '=', instance.id),
                ('odoo_variant_id', 'in', affected_products.ids),
                ('shopify_inventory_item_gid', '!=', False),
                ('shopify_location_gid', '!=', False),
            ])
            if not variants:
                continue

            # Build quantities payload
            quantities = []
            for variant in variants:
                qty = self.env['stock.quant']._get_available_quantity(
                    variant.odoo_variant_id, odoo_location
                )
                quantities.append({
                    'inventoryItemId': variant.shopify_inventory_item_gid,
                    'locationId':      variant.shopify_location_gid,
                    'quantity':        int(qty),
                })

            if not quantities:
                continue

            from .shopify_product import MUTATION_INVENTORY_SET
            try:
                data = instance._graphql_request(
                    MUTATION_INVENTORY_SET,
                    variables={'input': {
                        'reason': 'correction',
                        'referenceDocumentUri': f'odoo://stock/realtime/{instance.id}',
                        'quantities': quantities,
                    }},
                )
                user_errors = data.get('inventorySetQuantities', {}).get('userErrors', [])
                if user_errors:
                    _logger.error(
                        'Real-time inventory push errors for instance %s: %s',
                        instance.name, user_errors,
                    )
                else:
                    _logger.info(
                        'Real-time inventory pushed: %d variants for instance %s',
                        len(quantities), instance.name,
                    )
            except Exception as e:
                _logger.error(
                    'Real-time inventory push failed for instance %s: %s',
                    instance.name, e,
                )
