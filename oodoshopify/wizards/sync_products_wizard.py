from odoo import models, fields, api, _


class SyncProductsWizard(models.TransientModel):
    _name = 'shopify.sync.products.wizard'
    _description = 'Sync Products Wizard'

    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True)
    direction = fields.Selection([
        ('import', 'Import from Shopify → Odoo'),
        ('export', 'Export from Odoo → Shopify'),
        ('push_prices', 'Push Prices to Shopify'),
        ('push_inventory', 'Push Inventory to Shopify'),
    ], string='Direction', default='import', required=True)
    product_ids = fields.Many2many('shopify.product', string='Products (leave empty for all)')
    run_in_background = fields.Boolean(
        string='Run in background',
        help='Recommended for large catalogs. Imports run as a background job so '
             'the screen does not freeze — track progress in Monitoring → Sync Queue.',
    )

    def action_confirm(self):
        # Background path (import only) — enqueue and return immediately.
        if self.run_in_background and self.direction == 'import':
            self.env['shopify.queue'].enqueue_historical_import(
                self.instance_id, 'products', {'chunked': True})
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Import started'),
                    'message': _('Products are importing in the background. '
                                 'Track progress in Monitoring → Sync Queue.'),
                    'type': 'success',
                    'next': {'type': 'ir.actions.act_window_close'},
                },
            }

        products = self.product_ids or self.env['shopify.product'].search([('instance_id', '=', self.instance_id.id)])
        if self.direction == 'import':
            count = self.env['shopify.product'].import_products_from_shopify(self.instance_id)
            msg = _('Imported %d products from Shopify.') % count
        elif self.direction == 'export':
            products.action_export_to_shopify()
            msg = _('Exported %d products to Shopify.') % len(products)
        elif self.direction == 'push_prices':
            products.action_push_prices()
            msg = _('Pushed prices for %d products.') % len(products)
        elif self.direction == 'push_inventory':
            products.action_sync_inventory()
            msg = _('Pushed inventory for %d products.') % len(products)
        else:
            msg = _('Done.')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Sync Complete'), 'message': msg, 'type': 'success'},
        }
