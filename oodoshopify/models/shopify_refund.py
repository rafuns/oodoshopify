import logging
import uuid
from psycopg2 import IntegrityError
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# GraphQL — push a refund Odoo → Shopify
QUERY_SUGGESTED_REFUND = '''
    query suggestedRefund($id: ID!, $refundLineItems: [RefundLineItemInput!]!) {
        order(id: $id) {
            suggestedRefund(refundLineItems: $refundLineItems) {
                amountSet { shopMoney { amount currencyCode } }
                maximumRefundableSet { shopMoney { amount } }
                suggestedTransactions {
                    amountSet { shopMoney { amount } }
                    gateway
                    kind
                    parentTransaction { id }
                }
            }
        }
    }
'''

MUTATION_REFUND_CREATE = '''
    mutation refundCreate($input: RefundInput!) {
        refundCreate(input: $input) {
            refund { id totalRefundedSet { shopMoney { amount } } }
            userErrors { field message }
        }
    }
'''

# ---------------------------------------------------------------------------
# GraphQL — fetch full refund detail (called on-demand from a webhook payload)
# ---------------------------------------------------------------------------

QUERY_REFUND = '''
    query GetRefund($orderId: ID!) {
        order(id: $orderId) {
            id
            name
            refunds {
                id
                createdAt
                note
                totalRefundedSet { shopMoney { amount currencyCode } }
                refundLineItems(first: 50) {
                    edges {
                        node {
                            quantity
                            restockType
                            lineItem {
                                id
                                title
                                sku
                                originalUnitPriceSet { shopMoney { amount } }
                                variant { id }
                            }
                        }
                    }
                }
                transactions(first: 10) {
                    edges {
                        node {
                            id
                            kind
                            status
                            amountSet { shopMoney { amount currencyCode } }
                            gateway
                        }
                    }
                }
            }
        }
    }
'''


class ShopifyRefund(models.Model):
    _name = 'shopify.refund'
    _description = 'Shopify Refund'
    _inherit = ['mail.thread']
    _order = 'refund_date desc'

    _unique_refund_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_refund_id)',
        'This Shopify refund ID has already been processed for this instance.',
    )

    name = fields.Char(string='Refund Ref', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')
    shopify_refund_id = fields.Char(string='Shopify Refund ID', readonly=True)
    shopify_refund_gid = fields.Char(string='Shopify Refund GID', readonly=True)

    shopify_order_id = fields.Many2one('shopify.order', string='Shopify Order')
    odoo_sale_order_id = fields.Many2one('sale.order', string='Sale Order',
                                          related='shopify_order_id.odoo_sale_order_id', store=True)
    odoo_credit_note_id = fields.Many2one('account.move', string='Credit Note')

    refund_date = fields.Datetime(string='Refund Date')
    note = fields.Text(string='Note')
    total_refunded = fields.Float(string='Total Refunded')
    currency = fields.Char(string='Currency')

    state = fields.Selection([
        ('pending', 'Pending'),
        ('credit_note_created', 'Credit Note Created'),
        ('error', 'Error'),
    ], default='pending', tracking=True)

    line_ids = fields.One2many('shopify.refund.line', 'refund_id', string='Refund Lines')

    # ------------------------------------------------------------------
    # Create from webhook payload (minimal data) + enrich via GraphQL
    # ------------------------------------------------------------------

    @api.model
    def create_from_webhook(self, instance, payload):
        """Called by webhook controller on refunds/create."""
        order_id_raw = str(payload.get('order_id', ''))
        refund_id_raw = str(payload.get('id', ''))
        if not order_id_raw or not refund_id_raw:
            return

        # Find the related Shopify order
        shopify_order = self.env['shopify.order'].search([
            ('instance_id', '=', instance.id),
            ('shopify_order_id', '=', order_id_raw),
        ], limit=1)

        # Idempotency check — also backed by UNIQUE(instance_id, shopify_refund_id) DB constraint
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_refund_id', '=', refund_id_raw),
        ], limit=1)
        if existing:
            return existing

        total = float(payload.get('transactions', [{}])[0].get('amount', 0)) if payload.get('transactions') else 0.0
        refund_date = (payload.get('created_at') or '').replace('T', ' ').replace('Z', '') or False

        try:
            refund = self.create({
                'name': f"Refund/{order_id_raw}/{refund_id_raw}",
                'instance_id': instance.id,
                'shopify_refund_id': refund_id_raw,
                'shopify_order_id': shopify_order.id if shopify_order else False,
                'refund_date': refund_date,
                'note': payload.get('note', ''),
                'total_refunded': total,
                'currency': payload.get('currency', ''),
                'state': 'pending',
            })
        except IntegrityError:
            # Concurrent Shopify retry — duplicate already committed by another worker
            self.env.cr.rollback()
            return self.search([
                ('instance_id', '=', instance.id),
                ('shopify_refund_id', '=', refund_id_raw),
            ], limit=1)

        # Enrich with full GraphQL data and auto-create credit note
        if shopify_order and shopify_order.shopify_order_gid:
            try:
                refund._enrich_from_graphql(shopify_order.shopify_order_gid, refund_id_raw)
                if shopify_order.odoo_sale_order_id:
                    refund.action_create_credit_note()
            except Exception as e:
                _logger.error('Refund enrichment failed for %s: %s', refund_id_raw, e)
                refund.write({'state': 'error'})

        return refund

    def _enrich_from_graphql(self, order_gid, refund_id_numeric):
        """Fetch full refund detail from Shopify and populate line_ids."""
        self.ensure_one()
        data = self.instance_id._graphql_request(QUERY_REFUND, variables={'orderId': order_gid})
        order_node = data.get('order') or {}

        for refund_node in order_node.get('refunds', []):
            node_id = self.instance_id._gid_to_id(refund_node['id'])
            if node_id != refund_id_numeric:
                continue

            gid = refund_node['id']
            total_set = refund_node.get('totalRefundedSet', {}).get('shopMoney', {})
            self.write({
                'shopify_refund_gid': gid,
                'total_refunded': float(total_set.get('amount', self.total_refunded)),
                'currency': total_set.get('currencyCode', self.currency),
                'note': refund_node.get('note') or self.note,
            })

            self.line_ids.unlink()
            for edge in refund_node.get('refundLineItems', {}).get('edges', []):
                rli = edge['node']
                line_item = rli.get('lineItem') or {}
                unit_price = float(
                    (line_item.get('originalUnitPriceSet') or {}).get('shopMoney', {}).get('amount', 0)
                )
                variant_gid = (line_item.get('variant') or {}).get('id', '')
                self.env['shopify.refund.line'].create({
                    'refund_id': self.id,
                    'product_name': line_item.get('title', ''),
                    'sku': line_item.get('sku', ''),
                    'quantity': rli.get('quantity', 1),
                    'unit_price': unit_price,
                    'restock_type': rli.get('restockType', 'NO_RESTOCK'),
                    'shopify_variant_gid': variant_gid,
                })
            break

    # ------------------------------------------------------------------
    # Push refund Odoo → Shopify
    # ------------------------------------------------------------------
    def action_push_to_shopify(self):
        """Create this refund on Shopify (refundCreate), refunding the recorded
        line items. Uses suggestedRefund to compute correct transactions and an
        idempotency key so a retry never double-refunds."""
        self.ensure_one()
        if self.shopify_refund_gid or self.shopify_refund_id:
            raise UserError(_('This refund already exists on Shopify.'))
        order = self.shopify_order_id
        if not order or not order.shopify_order_id:
            raise UserError(_('No linked Shopify order to refund against.'))
        if not self.line_ids:
            raise UserError(_('Add at least one refund line before pushing.'))

        instance = self.instance_id
        order_gid = instance._build_gid('Order', order.shopify_order_id)

        # Map each refund line to its order line item GID (matched by variant)
        refund_line_items = []
        for line in self.line_ids:
            order_line = order.line_ids.filtered(
                lambda ol, v=line.shopify_variant_gid: v and ol.shopify_variant_gid == v
            )[:1]
            if not order_line or not order_line.shopify_line_id:
                continue
            refund_line_items.append({
                'lineItemId': instance._build_gid('LineItem', order_line.shopify_line_id),
                'quantity': line.quantity,
                'restockType': line.restock_type or 'NO_RESTOCK',
            })
        if not refund_line_items:
            raise UserError(_('Could not match refund lines to order line items.'))

        # 1) Ask Shopify for the suggested transactions (correct gateway/amounts)
        data = instance._graphql_request(
            QUERY_SUGGESTED_REFUND,
            variables={'id': order_gid, 'refundLineItems': refund_line_items})
        suggested = (data.get('order') or {}).get('suggestedRefund') or {}
        transactions = []
        for tx in suggested.get('suggestedTransactions', []):
            amount = (tx.get('amountSet') or {}).get('shopMoney', {}).get('amount')
            parent = (tx.get('parentTransaction') or {}).get('id')
            if amount and parent:
                transactions.append({
                    'orderId': order_gid,
                    'gateway': tx.get('gateway'),
                    'kind': 'REFUND',
                    'amount': amount,
                    'parentId': parent,
                })

        # 2) Create the refund (idempotent)
        refund_input = {
            'orderId': order_gid,
            'note': self.note or '',
            'notify': True,
            'refundLineItems': refund_line_items,
        }
        if transactions:
            refund_input['transactions'] = transactions

        result = instance._graphql_request(
            MUTATION_REFUND_CREATE, variables={'input': refund_input},
            idempotency_key=str(uuid.uuid4()),
        ).get('refundCreate', {})

        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Shopify refund failed: %s') % errors[0].get('message'))

        refund_node = result.get('refund') or {}
        gid = refund_node.get('id', '')
        self.write({
            'shopify_refund_gid': gid,
            'shopify_refund_id': instance._gid_to_id(gid) if gid else False,
            'refund_date': fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Refund Created'),
                'message': _('Refund pushed to Shopify for order %s.') % order.name,
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Create Odoo credit note
    # ------------------------------------------------------------------

    def action_create_credit_note(self):
        self.ensure_one()
        if self.odoo_credit_note_id:
            raise UserError(_('Credit note already exists: %s') % self.odoo_credit_note_id.name)

        sale_order = self.odoo_sale_order_id
        if not sale_order:
            raise UserError(_('No linked sale order — cannot create credit note.'))

        # Find posted invoice(s) on this sale order
        invoices = sale_order.invoice_ids.filtered(
            lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted'
        )
        if not invoices:
            raise UserError(_('No posted invoice found on sale order %s.') % sale_order.name)

        invoice = invoices[0]

        # Security: cap refund amount against original invoice — prevents inflated credit notes
        # from compromised Shopify accounts or bypassed HMAC (1% tolerance for rounding)
        if self.total_refunded > invoice.amount_total * 1.01:
            raise UserError(_(
                'Refund amount (%.2f) exceeds original invoice total (%.2f) for %s. '
                'Manual review required.'
            ) % (self.total_refunded, invoice.amount_total, self.name))

        credit_note_vals = {
            'move_type': 'out_refund',
            'partner_id': invoice.partner_id.id,
            'invoice_date': self.refund_date or fields.Date.today(),
            'currency_id': invoice.currency_id.id,
            'invoice_origin': f'Shopify Refund {self.name}',
            'ref': self.name,
            'narration': self.note or '',
            'invoice_line_ids': [],
        }

        for line in self.line_ids:
            product = self._find_product(line)
            credit_note_vals['invoice_line_ids'].append((0, 0, {
                'name': line.product_name,
                'product_id': product.id if product else False,
                'quantity': line.quantity,
                'price_unit': line.unit_price,
            }))

        if not credit_note_vals['invoice_line_ids']:
            # Fallback: single line with total refund amount
            credit_note_vals['invoice_line_ids'].append((0, 0, {
                'name': _('Shopify Refund — %s') % self.name,
                'quantity': 1,
                'price_unit': self.total_refunded,
            }))

        credit_note = self.env['account.move'].create(credit_note_vals)
        self.write({'odoo_credit_note_id': credit_note.id, 'state': 'credit_note_created'})

        # Send refund notification email
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        if workflow:
            workflow.notify_refund_processed(self)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': credit_note.id,
            'view_mode': 'form',
        }

    def _find_product(self, line):
        if line.sku:
            product = self.env['product.product'].search([('default_code', '=', line.sku)], limit=1)
            if product:
                return product
        if line.shopify_variant_gid:
            variant = self.env['shopify.product.variant'].search([
                ('shopify_variant_gid', '=', line.shopify_variant_gid),
            ], limit=1)
            if variant and variant.odoo_variant_id:
                return variant.odoo_variant_id
        return False


class ShopifyRefundLine(models.Model):
    _name = 'shopify.refund.line'
    _description = 'Shopify Refund Line'

    refund_id = fields.Many2one('shopify.refund', string='Refund', ondelete='cascade')
    product_name = fields.Char(string='Product')
    sku = fields.Char(string='SKU')
    shopify_variant_gid = fields.Char(string='Variant GID')
    quantity = fields.Integer(string='Qty')
    unit_price = fields.Float(string='Unit Price')
    subtotal = fields.Float(compute='_compute_subtotal', string='Subtotal')
    restock_type = fields.Selection([
        ('NO_RESTOCK', 'No Restock'),
        ('CANCEL', 'Cancelled'),
        ('RETURN', 'Return'),
        ('LEGACY_RESTOCK', 'Legacy Restock'),
    ], string='Restock Type', default='NO_RESTOCK')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')

    @api.depends('quantity', 'unit_price')
    def _compute_subtotal(self):
        for rec in self:
            rec.subtotal = rec.quantity * rec.unit_price
