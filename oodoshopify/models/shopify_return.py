import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL — Shopify Returns API (2024+)
# ---------------------------------------------------------------------------

QUERY_RETURNABLE_FULFILLMENTS = '''
    query GetReturnableFulfillments($orderId: ID!) {
        returnableFulfillments(orderId: $orderId, first: 10) {
            edges {
                node {
                    id
                    fulfillment { id }
                    returnableFulfillmentLineItems(first: 50) {
                        edges {
                            node {
                                quantity
                                fulfillmentLineItem {
                                    id
                                    lineItem {
                                        id
                                        title
                                        sku
                                        quantity
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
'''

QUERY_ORDER_RETURNS = '''
    query GetOrderReturns($orderId: ID!) {
        order(id: $orderId) {
            id
            returns(first: 20) {
                edges {
                    node {
                        id
                        name
                        status
                        totalQuantity
                        returnLineItems(first: 50) {
                            edges {
                                node {
                                    id
                                    quantity
                                    returnReason
                                    ... on ReturnLineItem {
                                        fulfillmentLineItem {
                                            lineItem { title sku }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
'''

MUTATION_RETURN_CREATE = '''
    mutation returnCreate($returnInput: ReturnInput!) {
        returnCreate(returnInput: $returnInput) {
            return {
                id
                name
                status
            }
            userErrors { field message }
        }
    }
'''

MUTATION_RETURN_APPROVE = '''
    mutation returnApproveRequest($input: ReturnApproveRequestInput!) {
        returnApproveRequest(input: $input) {
            return { id status }
            userErrors { field message }
        }
    }
'''

MUTATION_RETURN_DECLINE = '''
    mutation returnDeclineRequest($input: ReturnDeclineRequestInput!) {
        returnDeclineRequest(input: $input) {
            return { id status }
            userErrors { field message }
        }
    }
'''

MUTATION_RETURN_CLOSE = '''
    mutation returnClose($id: ID!) {
        returnClose(id: $id) {
            return { id status }
            userErrors { field message }
        }
    }
'''

MUTATION_RETURN_REFUND = '''
    mutation returnRefund($returnRefundInput: ReturnRefundInput!) {
        returnRefund(returnRefundInput: $returnRefundInput) {
            refund { id }
            userErrors { field message }
        }
    }
'''

_RETURN_STATUS_MAP = {
    'REQUESTED': 'requested',
    'OPEN':      'open',
    'CLOSED':    'closed',
    'DECLINED':  'declined',
    'CANCELED':  'canceled',
}

_RETURN_REASONS = [
    ('SIZE_TOO_SMALL',   'Size too small'),
    ('SIZE_TOO_LARGE',   'Size too large'),
    ('UNWANTED',         'Unwanted'),
    ('NOT_AS_DESCRIBED', 'Not as described'),
    ('WRONG_ITEM',       'Wrong item'),
    ('DEFECTIVE',        'Defective'),
    ('STYLE',            'Style'),
    ('COLOR',            'Color'),
    ('OTHER',            'Other'),
    ('UNKNOWN',          'Unknown'),
]


class ShopifyReturn(models.Model):
    _name = 'shopify.return'
    _description = 'Shopify Return / RMA'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Return #', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_return_id  = fields.Char(string='Return ID', readonly=True)
    shopify_return_gid = fields.Char(string='Return GID', readonly=True)

    shopify_order_id = fields.Many2one('shopify.order', string='Order', required=True)
    odoo_sale_order_id = fields.Many2one(
        related='shopify_order_id.odoo_sale_order_id', string='Sale Order', store=True,
    )

    status = fields.Selection([
        ('draft',     'Draft (local)'),
        ('requested', 'Requested'),
        ('open',      'Open / Approved'),
        ('closed',    'Closed'),
        ('declined',  'Declined'),
        ('canceled',  'Canceled'),
    ], string='Status', default='draft', tracking=True)

    total_quantity = fields.Integer(string='Total Qty', readonly=True)
    restock = fields.Boolean(string='Restock on Approval', default=True)
    notify_customer = fields.Boolean(string='Notify Customer', default=True)

    # Odoo return picking + refund links
    odoo_return_picking_id = fields.Many2one('stock.picking', string='Return Delivery')
    odoo_refund_id = fields.Many2one('account.move', string='Credit Note')

    line_ids = fields.One2many('shopify.return.line', 'return_id', string='Return Lines')

    _unique_return_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_return_id)',
        'This return already exists for this instance.',
    )

    # ------------------------------------------------------------------
    # Discover what's returnable on an order
    # ------------------------------------------------------------------

    @api.model
    def fetch_returnable_items(self, shopify_order):
        """Query Shopify for items that can be returned on this order."""
        if not shopify_order.shopify_order_gid:
            raise UserError(_('Order has no Shopify GID.'))
        data = shopify_order.instance_id._graphql_request(
            QUERY_RETURNABLE_FULFILLMENTS,
            variables={'orderId': shopify_order.shopify_order_gid},
        )
        items = []
        for fo_edge in data.get('returnableFulfillments', {}).get('edges', []):
            fo_node = fo_edge['node']
            for li_edge in fo_node.get('returnableFulfillmentLineItems', {}).get('edges', []):
                li = li_edge['node']
                fli = li.get('fulfillmentLineItem', {})
                line_item = fli.get('lineItem', {})
                items.append({
                    'fulfillment_line_item_gid': fli.get('id', ''),
                    'title': line_item.get('title', ''),
                    'sku': line_item.get('sku', ''),
                    'max_quantity': li.get('quantity', 0),
                })
        return items

    def action_load_returnable_items(self):
        """Populate return lines with everything returnable on the order."""
        self.ensure_one()
        items = self.fetch_returnable_items(self.shopify_order_id)
        self.line_ids.unlink()
        for item in items:
            self.env['shopify.return.line'].create({
                'return_id': self.id,
                'fulfillment_line_item_gid': item['fulfillment_line_item_gid'],
                'product_name': item['title'],
                'sku': item['sku'],
                'max_quantity': item['max_quantity'],
                'quantity': item['max_quantity'],
                'return_reason': 'UNWANTED',
            })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Returnable Items Loaded'),
                'message': _('%d line(s) available for return.') % len(items),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Create the return on Shopify
    # ------------------------------------------------------------------

    def action_create_return(self):
        """Create the return on Shopify via returnCreate."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_('Add return lines first (use "Load Returnable Items").'))

        return_line_items = []
        for line in self.line_ids.filtered(lambda l: l.quantity > 0):
            return_line_items.append({
                'fulfillmentLineItemId': line.fulfillment_line_item_gid,
                'quantity': line.quantity,
                'returnReason': line.return_reason or 'UNKNOWN',
            })
        if not return_line_items:
            raise UserError(_('No lines with a positive quantity to return.'))

        return_input = {
            'orderId': self.shopify_order_id.shopify_order_gid,
            'returnLineItems': return_line_items,
            'notifyCustomer': self.notify_customer,
        }

        data = self.instance_id._graphql_request(
            MUTATION_RETURN_CREATE, variables={'returnInput': return_input}
        )
        result = data.get('returnCreate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Return create error: %s') % errors[0]['message'])

        node = result.get('return', {})
        gid = node.get('id', '')
        self.write({
            'shopify_return_gid': gid,
            'shopify_return_id': self.instance_id._gid_to_id(gid),
            'name': node.get('name', self.name),
            'status': _RETURN_STATUS_MAP.get(node.get('status', 'OPEN'), 'open'),
        })
        self.message_post(body=_('Return created on Shopify.'))

    def action_approve(self):
        self.ensure_one()
        if not self.shopify_return_gid:
            raise UserError(_('Create the return on Shopify first.'))
        data = self.instance_id._graphql_request(
            MUTATION_RETURN_APPROVE,
            variables={'input': {'id': self.shopify_return_gid}},
        )
        errors = data.get('returnApproveRequest', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Approve error: %s') % errors[0]['message'])
        self.write({'status': 'open'})
        # Optionally create an Odoo return picking to receive the goods back
        if self.restock:
            self._create_return_picking()
        self.message_post(body=_('Return approved.'))

    def action_decline(self):
        self.ensure_one()
        if not self.shopify_return_gid:
            raise UserError(_('Create the return on Shopify first.'))
        data = self.instance_id._graphql_request(
            MUTATION_RETURN_DECLINE,
            variables={'input': {'id': self.shopify_return_gid}},
        )
        errors = data.get('returnDeclineRequest', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Decline error: %s') % errors[0]['message'])
        self.write({'status': 'declined'})
        self.message_post(body=_('Return declined.'))

    def action_close(self):
        self.ensure_one()
        if not self.shopify_return_gid:
            raise UserError(_('Create the return on Shopify first.'))
        data = self.instance_id._graphql_request(
            MUTATION_RETURN_CLOSE, variables={'id': self.shopify_return_gid}
        )
        errors = data.get('returnClose', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Close error: %s') % errors[0]['message'])
        self.write({'status': 'closed'})
        self.message_post(body=_('Return closed.'))

    # ------------------------------------------------------------------
    # Odoo return picking (receive goods back into stock)
    # ------------------------------------------------------------------

    def _create_return_picking(self):
        """Create an incoming stock picking to receive returned goods."""
        self.ensure_one()
        so = self.odoo_sale_order_id
        if not so:
            return
        # Find the outgoing delivery to reverse
        done_pickings = so.picking_ids.filtered(lambda p: p.state == 'done' and p.picking_type_code == 'outgoing')
        if not done_pickings:
            return
        warehouse = self.instance_id.warehouse_id
        if not warehouse:
            return
        return_picking_type = warehouse.return_type_id or warehouse.in_type_id
        if not return_picking_type:
            return

        picking = self.env['stock.picking'].create({
            'picking_type_id': return_picking_type.id,
            'partner_id': so.partner_id.id,
            'location_id': return_picking_type.default_location_src_id.id or self.env.ref('stock.stock_location_customers').id,
            'location_dest_id': warehouse.lot_stock_id.id,
            'origin': f'Return {self.name}',
        })
        for line in self.line_ids.filtered(lambda l: l.quantity > 0):
            product = False
            if line.sku:
                product = self.env['product.product'].search([('default_code', '=', line.sku)], limit=1)
            if product:
                self.env['stock.move'].create({
                    'name': line.product_name,
                    'picking_id': picking.id,
                    'product_id': product.id,
                    'product_uom_qty': line.quantity,
                    'product_uom': product.uom_id.id,
                    'location_id': picking.location_id.id,
                    'location_dest_id': picking.location_dest_id.id,
                })
        self.write({'odoo_return_picking_id': picking.id})
        return picking

    def action_view_return_picking(self):
        self.ensure_one()
        if not self.odoo_return_picking_id:
            raise UserError(_('No return delivery created.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.picking',
            'res_id': self.odoo_return_picking_id.id,
            'view_mode': 'form',
        }

    # ------------------------------------------------------------------
    # Import existing returns for an order
    # ------------------------------------------------------------------

    @api.model
    def import_returns_for_order(self, shopify_order):
        if not shopify_order.shopify_order_gid:
            return 0
        data = shopify_order.instance_id._graphql_request(
            QUERY_ORDER_RETURNS, variables={'orderId': shopify_order.shopify_order_gid}
        )
        order_node = data.get('order') or {}
        imported = 0
        for edge in order_node.get('returns', {}).get('edges', []):
            self._upsert_return(shopify_order, edge['node'])
            imported += 1
        return imported

    def _upsert_return(self, shopify_order, node):
        instance = shopify_order.instance_id
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_return_id', '=', numeric_id),
        ], limit=1)
        vals = {
            'name': node.get('name', f'Return/{numeric_id}'),
            'instance_id': instance.id,
            'shopify_return_id': numeric_id,
            'shopify_return_gid': gid,
            'shopify_order_id': shopify_order.id,
            'status': _RETURN_STATUS_MAP.get(node.get('status', 'OPEN'), 'open'),
            'total_quantity': node.get('totalQuantity', 0),
        }
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)


class ShopifyReturnLine(models.Model):
    _name = 'shopify.return.line'
    _description = 'Shopify Return Line'

    return_id = fields.Many2one('shopify.return', ondelete='cascade')
    fulfillment_line_item_gid = fields.Char(string='Fulfillment Line Item GID')
    product_name = fields.Char(string='Product')
    sku = fields.Char(string='SKU')
    max_quantity = fields.Integer(string='Max Returnable', readonly=True)
    quantity = fields.Integer(string='Return Qty', default=1)
    return_reason = fields.Selection(_RETURN_REASONS, string='Reason', default='UNWANTED')

    @api.constrains('quantity', 'max_quantity')
    def _check_quantity(self):
        for rec in self:
            if rec.max_quantity and rec.quantity > rec.max_quantity:
                raise ValidationError(_(
                    'Return quantity (%d) for "%s" exceeds the returnable maximum (%d).'
                ) % (rec.quantity, rec.product_name, rec.max_quantity))
