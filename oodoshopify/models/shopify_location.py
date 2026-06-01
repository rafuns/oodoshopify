import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_LOCATIONS = '''
    query GetLocations {
        locations(first: 50, includeInactive: false) {
            edges {
                node {
                    id
                    name
                    isActive
                    fulfillsOnlineOrders
                    address {
                        address1
                        address2
                        city
                        zip
                        countryCode
                    }
                }
            }
        }
    }
'''

QUERY_INVENTORY_LEVELS = '''
    query GetInventoryLevels($locationId: ID!, $first: Int!, $after: String) {
        location(id: $locationId) {
            inventoryLevels(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        quantities(names: ["available"]) {
                            name
                            quantity
                        }
                        item {
                            id
                            sku
                            variant { id }
                        }
                    }
                }
            }
        }
    }
'''


class ShopifyLocation(models.Model):
    _name = 'shopify.location'
    _description = 'Shopify Location'
    _order = 'is_default desc, name'

    name = fields.Char(string='Location Name', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')

    shopify_location_id = fields.Char(string='Location ID (numeric)', readonly=True)
    shopify_location_gid = fields.Char(string='Location GID', readonly=True)

    is_active = fields.Boolean(string='Active on Shopify', readonly=True)
    fulfills_online = fields.Boolean(string='Fulfills Online Orders', readonly=True)

    # Address (read-only, from Shopify)
    address1 = fields.Char(string='Address', readonly=True)
    address2 = fields.Char(string='Address 2', readonly=True)
    city = fields.Char(string='City', readonly=True)
    zip = fields.Char(string='ZIP', readonly=True)
    country_code = fields.Char(string='Country Code', readonly=True)

    # Odoo mapping
    odoo_warehouse_id = fields.Many2one('stock.warehouse', string='Odoo Warehouse')
    odoo_location_id = fields.Many2one('stock.location', string='Odoo Stock Location',
                                       compute='_compute_odoo_location', store=True)
    is_default = fields.Boolean(string='Default for Sync', default=False)

    inventory_level_ids = fields.One2many('shopify.inventory.level', 'location_id', string='Inventory Levels')
    inventory_level_count = fields.Integer(compute='_compute_inventory_level_count')

    @api.depends('odoo_warehouse_id')
    def _compute_odoo_location(self):
        for rec in self:
            rec.odoo_location_id = rec.odoo_warehouse_id.lot_stock_id if rec.odoo_warehouse_id else False

    @api.depends('inventory_level_ids')
    def _compute_inventory_level_count(self):
        for rec in self:
            rec.inventory_level_count = len(rec.inventory_level_ids)

    # ------------------------------------------------------------------
    # Fetch locations from Shopify
    # ------------------------------------------------------------------

    @api.model
    def sync_locations(self, instance):
        """Pull all active locations from Shopify and upsert into Odoo."""
        data = instance._graphql_request(QUERY_LOCATIONS)
        edges = data.get('locations', {}).get('edges', [])
        synced = []
        for edge in edges:
            node = edge['node']
            gid = node['id']
            numeric_id = instance._gid_to_id(gid)
            address = node.get('address') or {}

            existing = self.search([
                ('instance_id', '=', instance.id),
                ('shopify_location_id', '=', numeric_id),
            ], limit=1)

            vals = {
                'name': node.get('name', ''),
                'shopify_location_id': numeric_id,
                'shopify_location_gid': gid,
                'instance_id': instance.id,
                'is_active': node.get('isActive', True),
                'fulfills_online': node.get('fulfillsOnlineOrders', False),
                'address1': address.get('address1', ''),
                'address2': address.get('address2', ''),
                'city': address.get('city', ''),
                'zip': address.get('zip', ''),
                'country_code': address.get('countryCode', ''),
            }

            if existing:
                existing.write(vals)
                synced.append(existing)
            else:
                synced.append(self.create(vals))

        # Auto-set the first active location as default if none is set yet
        if synced and not self.search([('instance_id', '=', instance.id), ('is_default', '=', True)], limit=1):
            active = next((l for l in synced if l.is_active), None)
            if active:
                active.is_default = True

        _logger.info('Synced %d locations for %s', len(synced), instance.name)
        return synced

    def action_set_default(self):
        """Set this location as the default for inventory sync, clearing others."""
        self.ensure_one()
        siblings = self.search([('instance_id', '=', self.instance_id.id)])
        siblings.write({'is_default': False})
        self.is_default = True

    # ------------------------------------------------------------------
    # Import inventory levels for this location
    # ------------------------------------------------------------------

    def action_import_inventory_levels(self):
        """Fetch current inventory quantities from Shopify for this location."""
        self.ensure_one()
        if not self.shopify_location_gid:
            raise UserError(_('No Shopify GID — sync locations first.'))

        cursor = None
        imported = 0
        while True:
            variables = {
                'locationId': self.shopify_location_gid,
                'first': 100,
                'after': cursor,
            }
            data = self.instance_id._graphql_request(QUERY_INVENTORY_LEVELS, variables=variables)
            location_data = data.get('location') or {}
            connection = location_data.get('inventoryLevels') or {}
            edges = connection.get('edges', [])

            for edge in edges:
                self._upsert_inventory_level(edge['node'])
                imported += 1

            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')

        _logger.info('Imported %d inventory levels for location %s', imported, self.name)
        return imported

    def _upsert_inventory_level(self, node):
        item = node.get('item') or {}
        item_gid = item.get('id', '')
        item_numeric = self.instance_id._gid_to_id(item_gid) if item_gid else ''
        variant_gid = (item.get('variant') or {}).get('id', '')

        quantities = {q['name']: q['quantity'] for q in (node.get('quantities') or [])}
        available_qty = quantities.get('available', 0)

        existing = self.env['shopify.inventory.level'].search([
            ('location_id', '=', self.id),
            ('shopify_inventory_item_id', '=', item_numeric),
        ], limit=1)

        vals = {
            'location_id': self.id,
            'shopify_inventory_item_id': item_numeric,
            'shopify_inventory_item_gid': item_gid,
            'shopify_variant_gid': variant_gid,
            'sku': item.get('sku', ''),
            'available_quantity': available_qty,
        }

        # Try to match to a product variant
        if variant_gid:
            variant = self.env['shopify.product.variant'].search([
                ('shopify_variant_gid', '=', variant_gid),
            ], limit=1)
            if variant:
                vals['shopify_product_variant_id'] = variant.id
                # Store location GID on the variant so inventory push works
                variant.write({
                    'shopify_location_gid': self.shopify_location_gid,
                    'shopify_location_id': self.shopify_location_id,
                })

        if existing:
            existing.write(vals)
        else:
            self.env['shopify.inventory.level'].create(vals)

    def action_view_inventory_levels(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Inventory Levels — %s') % self.name,
            'res_model': 'shopify.inventory.level',
            'view_mode': 'list',
            'domain': [('location_id', '=', self.id)],
        }

    @api.model
    def cron_import_inventory_levels(self):
        """Scheduled action: refresh inventory levels for all default locations."""
        default_locations = self.search([
            ('is_default', '=', True),
            ('is_active', '=', True),
            ('instance_id.state', '=', 'connected'),
            ('instance_id.sync_inventory', '=', True),
        ])
        for location in default_locations:
            try:
                count = location.action_import_inventory_levels()
                _logger.info('Cron: refreshed %d inventory levels for %s / %s',
                             count, location.instance_id.name, location.name)
            except Exception as e:
                _logger.error('Cron: inventory level import failed for %s / %s: %s',
                              location.instance_id.name, location.name, e)


class ShopifyInventoryLevel(models.Model):
    _name = 'shopify.inventory.level'
    _description = 'Shopify Inventory Level'
    _order = 'sku'

    location_id = fields.Many2one('shopify.location', string='Location', required=True, ondelete='cascade')
    instance_id = fields.Many2one(related='location_id.instance_id', store=True)

    shopify_inventory_item_id = fields.Char(string='Inventory Item ID')
    shopify_inventory_item_gid = fields.Char(string='Inventory Item GID')
    shopify_variant_gid = fields.Char(string='Variant GID')
    shopify_product_variant_id = fields.Many2one('shopify.product.variant', string='Variant Mapping')

    sku = fields.Char(string='SKU')
    available_quantity = fields.Integer(string='Available Qty', readonly=True)
    odoo_quantity = fields.Float(string='Odoo Qty', compute='_compute_odoo_quantity')
    quantity_diff = fields.Integer(string='Difference', compute='_compute_odoo_quantity')

    @api.depends('shopify_product_variant_id', 'location_id.odoo_location_id', 'available_quantity')
    def _compute_odoo_quantity(self):
        for rec in self:
            odoo_qty = 0.0
            variant = rec.shopify_product_variant_id
            location = rec.location_id.odoo_location_id
            if variant and variant.odoo_variant_id and location:
                odoo_qty = self.env['stock.quant']._get_available_quantity(
                    variant.odoo_variant_id, location
                )
            rec.odoo_quantity = odoo_qty
            rec.quantity_diff = int(odoo_qty) - rec.available_quantity
