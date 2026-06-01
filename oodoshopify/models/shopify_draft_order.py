import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_DRAFT_ORDERS = '''
    query GetDraftOrders($first: Int!, $after: String) {
        draftOrders(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    name
                    status
                    invoiceUrl
                    createdAt
                    email
                    totalPriceSet { shopMoney { amount currencyCode } }
                    customer { id firstName lastName email }
                    lineItems(first: 100) {
                        edges {
                            node {
                                title
                                quantity
                                sku
                                originalUnitPriceSet { shopMoney { amount } }
                                variant { id }
                            }
                        }
                    }
                }
            }
        }
    }
'''

MUTATION_DRAFT_ORDER_CREATE = '''
    mutation draftOrderCreate($input: DraftOrderInput!) {
        draftOrderCreate(input: $input) {
            draftOrder {
                id
                name
                status
                invoiceUrl
                totalPriceSet { shopMoney { amount currencyCode } }
            }
            userErrors { field message }
        }
    }
'''

MUTATION_DRAFT_ORDER_UPDATE = '''
    mutation draftOrderUpdate($id: ID!, $input: DraftOrderInput!) {
        draftOrderUpdate(id: $id, input: $input) {
            draftOrder { id name status invoiceUrl }
            userErrors { field message }
        }
    }
'''

MUTATION_DRAFT_ORDER_COMPLETE = '''
    mutation draftOrderComplete($id: ID!, $paymentPending: Boolean) {
        draftOrderComplete(id: $id, paymentPending: $paymentPending) {
            draftOrder {
                id
                status
                order { id name }
            }
            userErrors { field message }
        }
    }
'''

MUTATION_DRAFT_ORDER_DELETE = '''
    mutation draftOrderDelete($input: DraftOrderDeleteInput!) {
        draftOrderDelete(input: $input) {
            deletedId
            userErrors { field message }
        }
    }
'''

MUTATION_DRAFT_ORDER_SEND_INVOICE = '''
    mutation draftOrderInvoiceSend($id: ID!) {
        draftOrderInvoiceSend(id: $id) {
            draftOrder { id status }
            userErrors { field message }
        }
    }
'''

_STATUS_MAP = {
    'OPEN':      'open',
    'INVOICE_SENT': 'invoice_sent',
    'COMPLETED': 'completed',
}


class ShopifyDraftOrder(models.Model):
    _name = 'shopify.draft.order'
    _description = 'Shopify Draft Order'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Draft Order #', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_draft_id  = fields.Char(string='Draft Order ID', readonly=True)
    shopify_draft_gid = fields.Char(string='Draft Order GID', readonly=True)

    # Source link (when created from an Odoo quotation)
    odoo_sale_order_id = fields.Many2one('sale.order', string='Source Quotation')
    # Result when completed
    shopify_order_id = fields.Many2one('shopify.order', string='Completed Order')

    customer_email = fields.Char(string='Customer Email')
    odoo_partner_id = fields.Many2one('res.partner', string='Customer')
    invoice_url = fields.Char(string='Invoice URL', readonly=True,
                              help='Shopify-hosted invoice link to send the customer.')

    total_price = fields.Float(string='Total', readonly=True)
    currency = fields.Char(string='Currency')

    status = fields.Selection([
        ('draft',        'Draft (local)'),
        ('open',         'Open on Shopify'),
        ('invoice_sent', 'Invoice Sent'),
        ('completed',    'Completed → Order'),
    ], string='Status', default='draft', tracking=True)

    line_ids = fields.One2many('shopify.draft.order.line', 'draft_order_id', string='Lines')

    _unique_draft_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_draft_id)',
        'This draft order already exists for this instance.',
    )

    # ------------------------------------------------------------------
    # Create from an Odoo quotation
    # ------------------------------------------------------------------

    @api.model
    def create_from_sale_order(self, sale_order, instance):
        """Build a Shopify draft order from an Odoo sale order / quotation."""
        draft = self.create({
            'name': f'Draft/{sale_order.name}',
            'instance_id': instance.id,
            'odoo_sale_order_id': sale_order.id,
            'customer_email': sale_order.partner_id.email or '',
            'odoo_partner_id': sale_order.partner_id.id,
            'currency': sale_order.currency_id.name,
            'status': 'draft',
        })
        for line in sale_order.order_line.filtered(lambda l: not l.display_type):
            draft.env['shopify.draft.order.line'].create({
                'draft_order_id': draft.id,
                'product_name': line.product_id.name or line.name,
                'sku': line.product_id.default_code or '',
                'quantity': int(line.product_uom_qty),
                'unit_price': line.price_unit,
                'odoo_product_id': line.product_id.id,
            })
        return draft

    def _build_draft_input(self):
        self.ensure_one()
        line_items = []
        for line in self.line_ids:
            item = {
                'title': line.product_name,
                'quantity': line.quantity,
                'originalUnitPrice': str(line.unit_price),
            }
            # Prefer linking to a real Shopify variant if mapped
            variant = False
            if line.sku:
                variant = self.env['shopify.product.variant'].search([
                    ('sku', '=', line.sku),
                    ('shopify_product_id.instance_id', '=', self.instance_id.id),
                ], limit=1)
            if variant and variant.shopify_variant_gid:
                item = {
                    'variantId': variant.shopify_variant_gid,
                    'quantity': line.quantity,
                }
            line_items.append(item)

        draft_input = {'lineItems': line_items}
        if self.customer_email:
            draft_input['email'] = self.customer_email
        return draft_input

    # ------------------------------------------------------------------
    # Push to Shopify
    # ------------------------------------------------------------------

    def action_push_to_shopify(self):
        for rec in self:
            draft_input = rec._build_draft_input()
            if not draft_input.get('lineItems'):
                raise UserError(_('Draft order %s has no lines.') % rec.name)
            try:
                if rec.shopify_draft_gid:
                    data = rec.instance_id._graphql_request(
                        MUTATION_DRAFT_ORDER_UPDATE,
                        variables={'id': rec.shopify_draft_gid, 'input': draft_input},
                    )
                    result = data.get('draftOrderUpdate', {})
                else:
                    data = rec.instance_id._graphql_request(
                        MUTATION_DRAFT_ORDER_CREATE,
                        variables={'input': draft_input},
                    )
                    result = data.get('draftOrderCreate', {})

                errors = result.get('userErrors', [])
                if errors:
                    raise UserError(_('Shopify error: %s') % errors[0]['message'])

                node = result.get('draftOrder', {})
                gid = node.get('id', '')
                vals = {
                    'shopify_draft_gid': gid,
                    'shopify_draft_id': rec.instance_id._gid_to_id(gid),
                    'invoice_url': node.get('invoiceUrl', ''),
                    'status': _STATUS_MAP.get(node.get('status', 'OPEN'), 'open'),
                }
                total_set = node.get('totalPriceSet', {}).get('shopMoney', {})
                if total_set:
                    vals['total_price'] = float(total_set.get('amount', 0))
                    vals['currency'] = total_set.get('currencyCode', rec.currency)
                rec.write(vals)
                rec.message_post(body=_('Draft order pushed to Shopify.'))
            except UserError:
                raise

    def action_send_invoice(self):
        """Email the Shopify-hosted invoice to the customer."""
        self.ensure_one()
        if not self.shopify_draft_gid:
            raise UserError(_('Push to Shopify first.'))
        data = self.instance_id._graphql_request(
            MUTATION_DRAFT_ORDER_SEND_INVOICE,
            variables={'id': self.shopify_draft_gid},
        )
        errors = data.get('draftOrderInvoiceSend', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Invoice send error: %s') % errors[0]['message'])
        self.write({'status': 'invoice_sent'})
        self.message_post(body=_('Invoice sent to customer via Shopify.'))

    def action_complete(self):
        """Complete the draft order — converts it into a real Shopify order."""
        self.ensure_one()
        if not self.shopify_draft_gid:
            raise UserError(_('Push to Shopify first.'))
        data = self.instance_id._graphql_request(
            MUTATION_DRAFT_ORDER_COMPLETE,
            variables={'id': self.shopify_draft_gid, 'paymentPending': True},
        )
        result = data.get('draftOrderComplete', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Complete error: %s') % errors[0]['message'])

        self.write({'status': 'completed'})
        order_node = result.get('draftOrder', {}).get('order') or {}
        if order_node.get('id'):
            self.message_post(
                body=_('Draft completed → Shopify order %s created.') % order_node.get('name', '')
            )
        return True

    def action_delete_from_shopify(self):
        self.ensure_one()
        if self.shopify_draft_gid:
            self.instance_id._graphql_request(
                MUTATION_DRAFT_ORDER_DELETE,
                variables={'input': {'id': self.shopify_draft_gid}},
            )
        self.unlink()

    # ------------------------------------------------------------------
    # Import existing draft orders
    # ------------------------------------------------------------------

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            data = instance._graphql_request(
                QUERY_DRAFT_ORDERS, variables={'first': 50, 'after': cursor}
            )
            connection = data.get('draftOrders', {})
            for edge in connection.get('edges', []):
                self._upsert_draft(instance, edge['node'])
                imported += 1
            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        _logger.info('Imported %d draft orders from %s', imported, instance.name)
        return imported

    def _upsert_draft(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_draft_id', '=', numeric_id),
        ], limit=1)
        total_set = node.get('totalPriceSet', {}).get('shopMoney', {})
        vals = {
            'name': node.get('name', f'Draft/{numeric_id}'),
            'instance_id': instance.id,
            'shopify_draft_id': numeric_id,
            'shopify_draft_gid': gid,
            'customer_email': node.get('email', ''),
            'invoice_url': node.get('invoiceUrl', ''),
            'total_price': float(total_set.get('amount', 0)),
            'currency': total_set.get('currencyCode', ''),
            'status': _STATUS_MAP.get(node.get('status', 'OPEN'), 'open'),
        }
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)


class ShopifyDraftOrderLine(models.Model):
    _name = 'shopify.draft.order.line'
    _description = 'Shopify Draft Order Line'

    draft_order_id = fields.Many2one('shopify.draft.order', ondelete='cascade')
    product_name = fields.Char(string='Product')
    sku = fields.Char(string='SKU')
    quantity = fields.Integer(string='Qty', default=1)
    unit_price = fields.Float(string='Unit Price')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    subtotal = fields.Float(compute='_compute_subtotal', string='Subtotal')

    @api.depends('quantity', 'unit_price')
    def _compute_subtotal(self):
        for rec in self:
            rec.subtotal = rec.quantity * rec.unit_price


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_create_shopify_draft(self):
        """Create a Shopify draft order from this quotation."""
        self.ensure_one()
        instance = self.env['shopify.instance'].search(
            [('state', '=', 'connected')], limit=1
        )
        if not instance:
            raise UserError(_('No connected Shopify instance found.'))
        draft = self.env['shopify.draft.order'].create_from_sale_order(self, instance)
        draft.action_push_to_shopify()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.draft.order',
            'res_id': draft.id,
            'view_mode': 'form',
        }
