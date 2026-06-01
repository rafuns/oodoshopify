import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

MUTATION_INVENTORY_MOVE = '''
    mutation inventoryMoveQuantities($input: InventoryMoveQuantitiesInput!) {
        inventoryMoveQuantities(input: $input) {
            inventoryAdjustmentGroup { id reason }
            userErrors { field message }
        }
    }
'''


class ShopifyInventoryTransfer(models.Model):
    _name = 'shopify.inventory.transfer'
    _description = 'Shopify Inventory Transfer'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', required=True, default='Transfer')
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    origin_location_id = fields.Many2one(
        'shopify.location', string='From Location', required=True,
        domain="[('instance_id', '=', instance_id)]",
    )
    dest_location_id = fields.Many2one(
        'shopify.location', string='To Location', required=True,
        domain="[('instance_id', '=', instance_id)]",
    )
    state = fields.Selection([
        ('draft',     'Draft'),
        ('completed', 'Completed'),
        ('error',     'Error'),
    ], default='draft', tracking=True)
    line_ids = fields.One2many('shopify.inventory.transfer.line', 'transfer_id', string='Lines')

    def action_execute_transfer(self):
        """Move inventory between two Shopify locations."""
        self.ensure_one()
        if self.origin_location_id == self.dest_location_id:
            raise UserError(_('Origin and destination must differ.'))
        if not self.line_ids:
            raise UserError(_('Add at least one line to transfer.'))

        errors = []
        for line in self.line_ids.filtered(lambda l: l.quantity > 0):
            if not line.shopify_inventory_item_gid:
                continue
            move_input = {
                'reason': 'movement_created',
                'referenceDocumentUri': f'odoo://transfer/{self.id}',
                'changes': [{
                    'inventoryItemId': line.shopify_inventory_item_gid,
                    'quantity': line.quantity,
                    'from': {
                        'locationId': self.origin_location_id.shopify_location_gid,
                        'name': 'available',
                    },
                    'to': {
                        'locationId': self.dest_location_id.shopify_location_gid,
                        'name': 'available',
                    },
                }],
            }
            try:
                data = self.instance_id._graphql_request(
                    MUTATION_INVENTORY_MOVE, variables={'input': move_input}
                )
                user_errors = data.get('inventoryMoveQuantities', {}).get('userErrors', [])
                if user_errors:
                    errors.append(f'{line.product_name}: {user_errors[0]["message"]}')
            except Exception as e:
                errors.append(f'{line.product_name}: {e}')

        if errors:
            self.write({'state': 'error'})
            self.message_post(body=_('Transfer errors: %s') % '; '.join(errors))
        else:
            self.write({'state': 'completed'})
            self.message_post(body=_('Inventory transfer completed.'))


class ShopifyInventoryTransferLine(models.Model):
    _name = 'shopify.inventory.transfer.line'
    _description = 'Shopify Inventory Transfer Line'

    transfer_id = fields.Many2one('shopify.inventory.transfer', ondelete='cascade')
    product_name = fields.Char(string='Product')
    sku = fields.Char(string='SKU')
    shopify_inventory_item_gid = fields.Char(string='Inventory Item GID')
    quantity = fields.Integer(string='Quantity', default=1)
