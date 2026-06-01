import logging
from psycopg2 import IntegrityError
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL queries and mutations
# ---------------------------------------------------------------------------

# GraphQL query for fetching tax lines per order (used in order detail enrichment)
QUERY_ORDER_TAXES = '''
    query GetOrderTaxLines($orderId: ID!) {
        order(id: $orderId) {
            id
            taxLines {
                title
                rate
                priceSet { shopMoney { amount } }
            }
            lineItems(first: 100) {
                edges {
                    node {
                        id
                        taxLines {
                            title
                            rate
                            priceSet { shopMoney { amount } }
                        }
                    }
                }
            }
        }
    }
'''

QUERY_ORDERS = '''
    query GetOrders($first: Int!, $after: String, $query: String) {
        orders(first: $first, after: $after, query: $query) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    name
                    email
                    createdAt
                    displayFinancialStatus
                    displayFulfillmentStatus
                    riskLevel
                    tags
                    note
                    totalPriceSet             { shopMoney { amount currencyCode } }
                    subtotalPriceSet          { shopMoney { amount } }
                    totalTaxSet               { shopMoney { amount } }
                    totalDiscountsSet         { shopMoney { amount } }
                    totalTipReceivedSet       { shopMoney { amount } }
                    currentTotalDutiesSet     { shopMoney { amount } }
                    sourceIdentifier
                    channelInformation {
                        channelDefinition { channelName }
                    }
                    shippingLines(first: 5) {
                        edges {
                            node {
                                id
                                title
                                code
                                originalPriceSet { shopMoney { amount currencyCode } }
                            }
                        }
                    }
                    fulfillmentOrders(first: 5) {
                        edges {
                            node {
                                id
                                status
                                deliveryMethod { methodType }
                                assignedLocation {
                                    name
                                    address1
                                    city
                                }
                            }
                        }
                    }
                    lineItems(first: 100) {
                        edges {
                            node {
                                id
                                title
                                variantTitle
                                sku
                                quantity
                                originalUnitPriceSet { shopMoney { amount } }
                                discountedTotalSet   { shopMoney { amount } }
                                variant { id }
                            }
                        }
                    }
                }
            }
        }
    }
'''

MUTATION_ORDER_CANCEL = '''
    mutation orderCancel(
        $orderId:         ID!
        $reason:          OrderCancelReason!
        $refund:          Boolean!
        $restock:         Boolean!
        $notifyCustomer:  Boolean!
        $staffNote:       String
    ) {
        orderCancel(
            orderId:        $orderId
            reason:         $reason
            refund:         $refund
            restock:        $restock
            notifyCustomer: $notifyCustomer
            staffNote:      $staffNote
        ) {
            order {
                id
                cancelledAt
                cancelReason
                displayFinancialStatus
                displayFulfillmentStatus
            }
            orderCancelUserErrors { code field message }
            userErrors               { field message }
        }
    }
'''

MUTATION_FULFILLMENT_ORDER_MARK_READY = '''
    mutation fulfillmentOrderMarkAsReady($id: ID!) {
        fulfillmentOrderMarkAsReady(id: $id) {
            fulfillmentOrder { id status }
            userErrors { field message }
        }
    }
'''

MUTATION_FULFILLMENT_ORDER_CLOSE = '''
    mutation fulfillmentOrderClose($id: ID!, $message: String) {
        fulfillmentOrderClose(id: $id, message: $message) {
            fulfillmentOrder { id status }
            userErrors { field message }
        }
    }
'''

MUTATION_ORDER_MARK_AS_PAID = '''
    mutation orderMarkAsPaid($input: OrderMarkAsPaidInput!) {
        orderMarkAsPaid(input: $input) {
            order {
                id
                displayFinancialStatus
            }
            userErrors { field message }
        }
    }
'''

MUTATION_FULFILLMENT_CREATE = '''
    mutation fulfillmentCreateV2($fulfillment: FulfillmentV2Input!) {
        fulfillmentCreateV2(fulfillment: $fulfillment) {
            fulfillment {
                id
                status
                trackingInfo { number company url }
            }
            userErrors { field message }
        }
    }
'''

# Financial status mapping: Shopify GQL enum → our selection values
_FINANCIAL_STATUS_MAP = {
    'PENDING': 'pending',
    'AUTHORIZED': 'authorized',
    'PARTIALLY_PAID': 'partially_paid',
    'PAID': 'paid',
    'PARTIALLY_REFUNDED': 'partially_refunded',
    'REFUNDED': 'refunded',
    'VOIDED': 'voided',
}

_FULFILLMENT_STATUS_MAP = {
    'UNFULFILLED': 'unfulfilled',
    'PARTIALLY_FULFILLED': 'partial',
    'FULFILLED': 'fulfilled',
    'RESTOCKED': 'restocked',
    None: 'unfulfilled',
}


class ShopifyOrder(models.Model):
    _name = 'shopify.order'
    _description = 'Shopify Order'
    _inherit = ['mail.thread']
    _order = 'shopify_order_date desc'

    _unique_order_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_order_id)',
        'This Shopify order ID already exists for this instance.',
    )

    name = fields.Char(string='Shopify Order #', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')
    shopify_order_id = fields.Char(string='Shopify ID (numeric)', readonly=True)
    shopify_order_gid = fields.Char(string='Shopify GID', readonly=True)
    odoo_sale_order_id = fields.Many2one('sale.order', string='Odoo Sale Order')

    shopify_order_date = fields.Datetime(string='Order Date')
    customer_email = fields.Char(string='Customer Email')
    shopify_customer_id = fields.Many2one('shopify.customer', string='Shopify Customer')

    total_price = fields.Float(string='Total Price')
    subtotal_price = fields.Float(string='Subtotal')
    total_tax = fields.Float(string='Total Tax')
    total_discounts = fields.Float(string='Total Discounts')
    tip_amount = fields.Float(string='Tip')
    duties_total = fields.Float(string='Duties')
    currency = fields.Char(string='Currency')

    # Sales channel + shipping
    source_name = fields.Char(
        string='Sales Channel',
        help='e.g. web, pos, draft_orders, or a custom channel name.',
    )
    shipping_total = fields.Float(string='Shipping Total')

    # Tags & notes (bidirectional)
    order_tags = fields.Char(string='Tags', help='Comma-separated Shopify order tags.')
    order_note = fields.Text(string='Order Note')

    # Fraud risk (Shopify-assigned)
    risk_level = fields.Selection([
        ('low',    'Low'),
        ('medium', 'Medium'),
        ('high',   'High'),
    ], string='Fraud Risk', tracking=True)
    risk_score = fields.Integer(string='Risk Score', readonly=True,
                                help='0–100 triage score blending Shopify risk and Odoo signals.')
    risk_recommendation = fields.Text(string='Risk Recommendation', readonly=True)

    def assess_risk(self):
        """Heuristic 0–100 triage score from Shopify risk + Odoo customer history."""
        for order in self:
            score = {'high': 60, 'medium': 30, 'low': 0}.get(order.risk_level, 10)
            if order.total_price and order.total_price > 500:
                score += 20
            if order.financial_status == 'pending':
                score += 10
            # New customer? (no other orders for this email)
            if order.customer_email:
                prior = order.search_count([
                    ('customer_email', '=', order.customer_email),
                    ('id', '!=', order.id)])
                if prior == 0:
                    score += 15
            order.risk_score = min(100, score)
        return True

    def action_assess_risk_ai(self):
        """Add an AI recommendation on top of the heuristic score."""
        self.assess_risk()
        for order in self:
            if not order.instance_id.ai_api_key:
                order.risk_recommendation = _('(Set an AI key on the store for AI recommendations.)')
                continue
            user = (
                f'Order {order.name}: total {order.total_price}, '
                f'payment {order.financial_status}, Shopify risk {order.risk_level or "n/a"}, '
                f'heuristic score {order.risk_score}/100. '
                'In 2 sentences, recommend: fulfill, review, or hold — and why.')
            try:
                order.risk_recommendation = order.instance_id.ai_complete(
                    'You are a fraud-prevention analyst for e-commerce orders.', user)
            except Exception as e:
                order.risk_recommendation = _('AI assessment failed: %s') % e
        return True

    # Quick Jump — direct link to Shopify admin
    shopify_admin_url = fields.Char(
        string='View on Shopify',
        compute='_compute_shopify_admin_url',
    )

    @api.depends('instance_id', 'shopify_order_id')
    def _compute_shopify_admin_url(self):
        for rec in self:
            if rec.instance_id and rec.shopify_order_id:
                rec.shopify_admin_url = (
                    f'https://{rec.instance_id.shop_domain}'
                    f'/admin/orders/{rec.shopify_order_id}'
                )
            else:
                rec.shopify_admin_url = ''

    financial_status = fields.Selection([
        ('pending', 'Pending'),
        ('authorized', 'Authorized'),
        ('partially_paid', 'Partially Paid'),
        ('paid', 'Paid'),
        ('partially_refunded', 'Partially Refunded'),
        ('refunded', 'Refunded'),
        ('voided', 'Voided'),
    ], string='Payment Status', tracking=True)

    fulfillment_status = fields.Selection([
        ('unfulfilled', 'Unfulfilled'),
        ('partial', 'Partially Fulfilled'),
        ('fulfilled', 'Fulfilled'),
        ('restocked', 'Restocked'),
    ], string='Fulfillment Status', tracking=True)

    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('imported', 'Imported'),
        ('error', 'Error'),
    ], default='pending', tracking=True)

    line_ids = fields.One2many('shopify.order.line', 'order_id', string='Order Lines')
    metafield_ids = fields.One2many('shopify.metafield', 'shopify_order_id', string='Metafields')

    # ── Pickup / Local Delivery ──────────────────────────────────────────────
    is_pickup_order = fields.Boolean(
        string='Pickup Order',
        default=False,
        help='True when the customer selected local pickup at checkout.',
    )
    pickup_location_name = fields.Char(string='Pickup Location', readonly=True)
    pickup_status = fields.Selection([
        ('pending',    'Awaiting Pickup'),
        ('ready',      'Ready for Pickup'),
        ('picked_up',  'Picked Up'),
    ], string='Pickup Status', default='pending', tracking=True)
    pickup_fulfillment_order_gid = fields.Char(
        string='Fulfillment Order GID', readonly=True,
    )

    # ------------------------------------------------------------------
    # Import: Shopify → Odoo  (GraphQL cursor pagination)
    # ------------------------------------------------------------------

    @api.model
    def import_orders_from_shopify(self, instance, status='any', days_back=7,
                                   order_ids=None, from_date=None, to_date=None):
        """
        Import orders from Shopify.

        :param order_ids: list of numeric Shopify order IDs to import (filter by ID)
        :param from_date: import orders created after this date (overrides days_back)
        :param to_date:   import orders created before this date
        """
        from datetime import datetime, timedelta

        # Respect instance-level import delay
        delay_minutes = instance.order_import_delay_minutes or 0

        if order_ids:
            # Filter by specific IDs
            query_parts = [f'id:{",".join(str(i) for i in order_ids)}']
        else:
            if from_date:
                since = from_date if isinstance(from_date, str) else from_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            else:
                since = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
            query_parts = [f'created_at:>={since}']

            if to_date:
                until = to_date if isinstance(to_date, str) else to_date.strftime('%Y-%m-%dT%H:%M:%SZ')
                query_parts.append(f'created_at:<={until}')

        if status != 'any':
            query_parts.append(f'status:{status}')
        query_filter = ' '.join(query_parts)

        cursor = None
        imported = 0
        while True:
            count, cursor, has_next = self.import_orders_page(instance, query_filter, cursor)
            imported += count
            if not has_next:
                break

        _logger.info('Imported %d orders from %s', imported, instance.name)
        return imported

    def build_orders_query_filter(self, status='any', days_back=7, order_ids=None,
                                  from_date=None, to_date=None):
        """Build the Shopify search 'query' string for an order import. Exposed so
        the chunked background importer can compute it once and reuse per page."""
        from datetime import datetime, timedelta
        if order_ids:
            return f'id:{",".join(str(i) for i in order_ids)}'
        if from_date:
            since = from_date if isinstance(from_date, str) else from_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            since = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        parts = [f'created_at:>={since}']
        if to_date:
            until = to_date if isinstance(to_date, str) else to_date.strftime('%Y-%m-%dT%H:%M:%SZ')
            parts.append(f'created_at:<={until}')
        if status != 'any':
            parts.append(f'status:{status}')
        return ' '.join(parts)

    def import_orders_page(self, instance, query_filter, cursor=None, create_sale_orders=False):
        """Import one page of orders → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small.

        If create_sale_orders is set, the matching Odoo sale order is created for
        each order on this page that doesn't already have one.
        """
        from datetime import datetime, timedelta
        delay_minutes = instance.order_import_delay_minutes or 0
        variables = {'first': 50, 'after': cursor, 'query': query_filter}
        data = instance._graphql_request(QUERY_ORDERS, variables=variables)
        connection = data.get('orders', {})
        edges = connection.get('edges', [])
        imported = 0
        for edge in edges:
            node = edge['node']
            # Respect import delay — skip orders created too recently
            if delay_minutes > 0:
                created_raw = (node.get('createdAt') or '').replace('T', ' ').replace('Z', '')
                if created_raw:
                    try:
                        created_dt = datetime.strptime(created_raw, '%Y-%m-%d %H:%M:%S')
                        if datetime.utcnow() - created_dt < timedelta(minutes=delay_minutes):
                            continue
                    except ValueError:
                        pass
            rec = self._upsert_order(instance, node)
            imported += 1
            if create_sale_orders and rec and not rec.odoo_sale_order_id:
                try:
                    rec.action_create_sale_order()
                except Exception as e:
                    _logger.warning('Sale order creation failed for order %s: %s', rec.id, e)
        page_info = connection.get('pageInfo', {})
        return imported, page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_order(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_order_id', '=', numeric_id),
        ], limit=1)

        total_price_set = node.get('totalPriceSet', {}).get('shopMoney', {})

        def _amt(key):
            return float((node.get(key) or {}).get('shopMoney', {}).get('amount', 0))

        raw_risk = (node.get('riskLevel') or '').lower()
        risk_level = raw_risk if raw_risk in ('low', 'medium', 'high') else False

        vals = {
            'name': node.get('name', numeric_id),
            'shopify_order_id': numeric_id,
            'shopify_order_gid': gid,
            'instance_id': instance.id,
            'shopify_order_date': (node.get('createdAt') or '').replace('T', ' ').replace('Z', '') or False,
            'customer_email': node.get('email', ''),
            'total_price':    float(total_price_set.get('amount', 0)),
            'subtotal_price': _amt('subtotalPriceSet'),
            'total_tax':      _amt('totalTaxSet'),
            'total_discounts':_amt('totalDiscountsSet'),
            'tip_amount':     _amt('totalTipReceivedSet'),
            'duties_total':   _amt('currentTotalDutiesSet'),
            'currency': total_price_set.get('currencyCode', ''),
            'financial_status': _FINANCIAL_STATUS_MAP.get(node.get('displayFinancialStatus'), 'pending'),
            'fulfillment_status': _FULFILLMENT_STATUS_MAP.get(node.get('displayFulfillmentStatus'), 'unfulfilled'),
            'risk_level':    risk_level,
            'sync_status':   'imported',
            'source_name':   (
                (node.get('channelInformation') or {})
                .get('channelDefinition', {}).get('channelName', '')
                or node.get('sourceIdentifier', '')
            ),
            'shipping_total': sum(
                float((e['node'].get('originalPriceSet') or {}).get('shopMoney', {}).get('amount', 0))
                for e in node.get('shippingLines', {}).get('edges', [])
            ),
            'order_tags': ', '.join(node.get('tags', [])) if isinstance(node.get('tags'), list) else (node.get('tags') or ''),
            'order_note': node.get('note', '') or '',
        }

        # Detect pickup (LOCAL delivery method in fulfillmentOrders)
        fo_edges = node.get('fulfillmentOrders', {}).get('edges', [])
        for fo_edge in fo_edges:
            fo = fo_edge.get('node', {})
            method_type = (fo.get('deliveryMethod') or {}).get('methodType', '')
            if method_type == 'LOCAL':
                vals['is_pickup_order'] = True
                location = fo.get('assignedLocation') or {}
                vals['pickup_location_name'] = location.get('name', '')
                vals['pickup_fulfillment_order_gid'] = fo.get('id', '')
                break

        if existing:
            existing.write(vals)
            rec = existing
        else:
            try:
                rec = self.create(vals)
            except IntegrityError:
                # Concurrent Shopify retry — another worker committed first
                self.env.cr.rollback()
                rec = self.search([
                    ('instance_id', '=', instance.id),
                    ('shopify_order_id', '=', numeric_id),
                ], limit=1)
                if rec:
                    rec.write(vals)

        rec._sync_order_lines(node.get('lineItems', {}).get('edges', []), instance)

        # Post fraud risk warning to chatter on first import or escalation
        if rec.risk_level in ('medium', 'high') and not existing:
            icon = '🟡' if rec.risk_level == 'medium' else '🔴'
            rec.message_post(
                body=_(
                    '%s Shopify Fraud Risk: <strong>%s</strong>. '
                    'Review this order before fulfilling.'
                ) % (icon, rec.risk_level.upper()),
                message_type='comment',
            )

        return rec

    def _sync_order_lines(self, line_edges, instance):
        self.line_ids.unlink()
        for edge in line_edges:
            item = edge['node']
            variant_node = item.get('variant') or {}
            variant_gid = variant_node.get('id', '')
            variant_numeric = instance._gid_to_id(variant_gid) if variant_gid else ''
            unit_price = float((item.get('originalUnitPriceSet') or {}).get('shopMoney', {}).get('amount', 0))
            discount_total = float((item.get('discountedTotalSet') or {}).get('shopMoney', {}).get('amount', 0))
            qty = item.get('quantity', 1)
            total_discount = max(0.0, (unit_price * qty) - discount_total)

            # Try to map Shopify tax to Odoo tax
            tax_ids = []
            for tax_line in item.get('taxLines', []):
                tax_name = tax_line.get('title', '')
                tax_rate = float(tax_line.get('rate', 0)) * 100
                odoo_tax = (
                    self.env['account.tax'].search(
                        [('name', '=', tax_name), ('type_tax_use', '=', 'sale')], limit=1
                    ) or
                    self.env['account.tax'].search(
                        [('amount', '=', tax_rate), ('type_tax_use', '=', 'sale')], limit=1
                    )
                )
                if odoo_tax:
                    tax_ids.append(odoo_tax.id)

            self.env['shopify.order.line'].create({
                'order_id': self.id,
                'shopify_line_id': instance._gid_to_id(item['id']),
                'product_name': item.get('title', ''),
                'variant_title': item.get('variantTitle', ''),
                'sku': item.get('sku', ''),
                'quantity': qty,
                'price': unit_price,
                'total_discount': total_discount,
                'shopify_variant_id': variant_numeric,
                'shopify_variant_gid': variant_gid,
                'tax_ids': [(6, 0, tax_ids)],
            })

    # ------------------------------------------------------------------
    # Create Odoo Sale Order
    # ------------------------------------------------------------------

    def action_create_sale_order(self):
        self.ensure_one()
        if self.odoo_sale_order_id:
            raise UserError(_('Sale order already exists: %s') % self.odoo_sale_order_id.name)

        partner = self._get_or_create_partner()

        # ── Multi-currency: resolve pricelist + currency for this order ────────
        order_currency_code = (self.currency or '').upper()
        pricelist = self.instance_id._get_pricelist_for_currency(order_currency_code)

        # Find the matching res.currency for the Shopify order
        odoo_currency = False
        if order_currency_code:
            odoo_currency = self.env['res.currency'].search(
                [('name', '=', order_currency_code), ('active', '=', True)], limit=1
            )
        # ──────────────────────────────────────────────────────────────────────

        so_vals = {
            'partner_id': partner.id,
            'company_id': self.instance_id.company_id.id,
            'warehouse_id': self.instance_id.warehouse_id.id if self.instance_id.warehouse_id else False,
            'pricelist_id': pricelist.id if pricelist else False,
            'note': f'Shopify Order: {self.name}',
            'origin': self.name,
        }
        # Override currency if it differs from the pricelist's currency
        if odoo_currency and (not pricelist or pricelist.currency_id != odoo_currency):
            so_vals['currency_id'] = odoo_currency.id

        sale_order = self.env['sale.order'].create(so_vals)

        for line in self.line_ids:
            product = self._match_odoo_product(line)
            # Prices from Shopify are already in the order currency — use directly
            self.env['sale.order.line'].create({
                'order_id': sale_order.id,
                'product_id': product.id if product else False,
                'name': line.product_name,
                'product_uom_qty': line.quantity,
                'price_unit': line.price,
                'tax_ids': [(6, 0, line.tax_ids.ids)] if line.tax_ids else False,
                'discount': (line.total_discount / (line.price * line.quantity) * 100)
                            if line.price and line.quantity else 0,
            })

        self.write({'odoo_sale_order_id': sale_order.id})

        # Run order workflow automation if configured
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        if workflow:
            workflow.run_workflow(sale_order, self)
            workflow.notify_order_confirmation(self)

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'sale.order',
            'res_id': sale_order.id,
            'view_mode': 'form',
        }

    def _get_or_create_partner(self):
        if not self.customer_email:
            # Use configured fallback customer, then public partner as last resort
            if self.instance_id.fallback_customer_id:
                return self.instance_id.fallback_customer_id
            return self.env['res.partner'].browse(self.env.ref('base.public_partner').id)
        partner = self.env['res.partner'].search([('email', '=ilike', self.customer_email)], limit=1)
        if not partner:
            partner = self.env['res.partner'].create({'name': self.customer_email, 'email': self.customer_email})
        return partner

    def _match_odoo_product(self, line):
        """
        Match an order line to an Odoo product.variant using the instance's
        product_matching_strategy: 'sku', 'barcode', or 'sku_then_barcode'.
        """
        strategy = self.instance_id.product_matching_strategy or 'sku'

        if line.sku:
            if strategy in ('sku', 'sku_then_barcode'):
                product = self.env['product.product'].search(
                    [('default_code', '=', line.sku)], limit=1
                )
                if product:
                    return product

            if strategy in ('barcode', 'sku_then_barcode'):
                product = self.env['product.product'].search(
                    [('barcode', '=', line.sku)], limit=1
                )
                if product:
                    return product

        # Fallback: match by Shopify variant GID (always tried)
        if line.shopify_variant_gid:
            variant = self.env['shopify.product.variant'].search([
                ('shopify_variant_gid', '=', line.shopify_variant_gid),
            ], limit=1)
            if variant and variant.odoo_variant_id:
                return variant.odoo_variant_id
        return False

    # ------------------------------------------------------------------
    # Push tracking to Shopify  (GraphQL fulfillmentCreateV2)
    # ------------------------------------------------------------------

    def action_push_tracking_to_shopify(self):
        self.ensure_one()
        if not self.odoo_sale_order_id:
            raise UserError(_('No linked sale order found.'))

        pickings = self.odoo_sale_order_id.picking_ids.filtered(
            lambda p: p.state == 'done' and p.carrier_tracking_ref
        )
        if not pickings:
            raise UserError(_('No done delivery with a tracking number found.'))

        for picking in pickings:
            tracking_info = []
            if picking.carrier_tracking_ref:
                tracking_info.append({
                    'number': picking.carrier_tracking_ref,
                    'company': picking.carrier_id.name if picking.carrier_id else '',
                })

            # Suppress Shopify's own notification if Odoo sends it
            workflow = self.env['shopify.workflow'].search(
                [('instance_id', '=', self.instance_id.id)], limit=1
            )
            notify_via_shopify = not (workflow and workflow.suppress_shopify_emails)

            fulfillment_input = {
                'lineItemsByFulfillmentOrder': [
                    {'fulfillmentOrderId': self.shopify_order_gid}
                ],
                'trackingInfo': tracking_info,
                'notifyCustomer': notify_via_shopify,
            }

            data = self.instance_id._graphql_request(
                MUTATION_FULFILLMENT_CREATE,
                variables={'fulfillment': fulfillment_input},
            )
            result = data.get('fulfillmentCreateV2', {})
            user_errors = result.get('userErrors', [])
            if user_errors:
                raise UserError(_('Fulfillment error: %s') % user_errors[0]['message'])

            # Send Odoo shipping notification if Shopify's is suppressed
            if workflow and workflow.suppress_shopify_emails:
                workflow.notify_order_shipped(self)

    # ------------------------------------------------------------------
    # Tags & Notes  (push Odoo values to Shopify)
    # ------------------------------------------------------------------

    def action_push_tags_and_note(self):
        """Push order tags and note back to Shopify."""
        self.ensure_one()
        if not self.shopify_order_gid:
            raise UserError(_('Order has no Shopify GID.'))

        # Update note via orderUpdate
        mutation_update = '''
            mutation orderUpdate($input: OrderInput!) {
                orderUpdate(input: $input) {
                    order { id }
                    userErrors { field message }
                }
            }
        '''
        tags_list = [t.strip() for t in (self.order_tags or '').split(',') if t.strip()]
        order_input = {
            'id': self.shopify_order_gid,
            'note': self.order_note or '',
            'tags': tags_list,
        }
        data = self.instance_id._graphql_request(mutation_update, variables={'input': order_input})
        errors = data.get('orderUpdate', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Order update error: %s') % errors[0]['message'])
        self.message_post(body=_('Tags and note pushed to Shopify.'))

    # ------------------------------------------------------------------
    # Order Editing  (add line items to an existing Shopify order)
    # ------------------------------------------------------------------

    def action_add_line_item(self, variant_gid, quantity=1):
        """
        Add a product variant to an existing Shopify order using the
        order-editing flow: begin → add variant → commit.
        """
        self.ensure_one()
        if not self.shopify_order_gid:
            raise UserError(_('Order has no Shopify GID.'))

        begin = '''
            mutation orderEditBegin($id: ID!) {
                orderEditBegin(id: $id) {
                    calculatedOrder { id }
                    userErrors { field message }
                }
            }
        '''
        add_variant = '''
            mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
                orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
                    calculatedOrder { id }
                    userErrors { field message }
                }
            }
        '''
        commit = '''
            mutation orderEditCommit($id: ID!, $notifyCustomer: Boolean) {
                orderEditCommit(id: $id, notifyCustomer: $notifyCustomer) {
                    order { id }
                    userErrors { field message }
                }
            }
        '''
        # Step 1 — begin edit
        data = self.instance_id._graphql_request(begin, variables={'id': self.shopify_order_gid})
        result = data.get('orderEditBegin', {})
        if result.get('userErrors'):
            raise UserError(_('Order edit begin error: %s') % result['userErrors'][0]['message'])
        calc_id = result.get('calculatedOrder', {}).get('id')
        if not calc_id:
            raise UserError(_('Could not begin order edit.'))

        # Step 2 — add variant
        data = self.instance_id._graphql_request(add_variant, variables={
            'id': calc_id, 'variantId': variant_gid, 'quantity': quantity,
        })
        result = data.get('orderEditAddVariant', {})
        if result.get('userErrors'):
            raise UserError(_('Add variant error: %s') % result['userErrors'][0]['message'])

        # Step 3 — commit
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        notify = not (workflow and workflow.suppress_shopify_emails)
        data = self.instance_id._graphql_request(commit, variables={
            'id': calc_id, 'notifyCustomer': notify,
        })
        result = data.get('orderEditCommit', {})
        if result.get('userErrors'):
            raise UserError(_('Order edit commit error: %s') % result['userErrors'][0]['message'])

        self.message_post(body=_('Line item added to Shopify order via order editing.'))
        return True

    # ------------------------------------------------------------------
    # Auto Mark Paid  (Odoo invoice paid → push to Shopify)
    # ------------------------------------------------------------------

    def action_mark_as_paid_on_shopify(self):
        """Push 'mark as paid' to Shopify for this order."""
        self.ensure_one()
        if not self.shopify_order_gid:
            raise UserError(_('Order %s has no Shopify GID.') % self.name)
        if self.financial_status == 'paid':
            return  # already paid on Shopify

        data = self.instance_id._graphql_request(
            MUTATION_ORDER_MARK_AS_PAID,
            variables={'input': {'id': self.shopify_order_gid}},
        )
        result = data.get('orderMarkAsPaid', {})
        user_errors = result.get('userErrors', [])
        if user_errors:
            raise UserError(_('Mark as paid failed: %s') % user_errors[0]['message'])

        self.write({'financial_status': 'paid'})
        self.message_post(body=_('Order marked as paid on Shopify from Odoo.'))

    @api.model
    def cron_auto_mark_paid(self):
        """
        Scheduled action: mark Shopify orders as paid when the linked
        Odoo invoice is fully paid and auto_mark_paid is enabled on the workflow.
        """
        instances = self.env['shopify.instance'].search([('state', '=', 'connected')])
        for instance in instances:
            workflow = self.env['shopify.workflow'].search([
                ('instance_id', '=', instance.id),
                ('auto_mark_paid', '=', True),
            ], limit=1)
            if not workflow:
                continue

            # Find orders that are not yet paid on Shopify but have a fully paid Odoo invoice
            candidates = self.search([
                ('instance_id', '=', instance.id),
                ('financial_status', 'not in', ['paid', 'refunded', 'voided']),
                ('odoo_sale_order_id', '!=', False),
                ('shopify_order_gid', '!=', False),
            ])
            for order in candidates:
                so = order.odoo_sale_order_id
                invoices = so.invoice_ids.filtered(
                    lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted'
                )
                if invoices and all(
                    inv.payment_state in ('paid', 'in_payment') for inv in invoices
                ):
                    try:
                        order.action_mark_as_paid_on_shopify()
                    except Exception as e:
                        _logger.error(
                            'Auto mark paid failed for order %s: %s', order.name, e
                        )

    # ------------------------------------------------------------------
    # Cancel Shopify order from Odoo
    # ------------------------------------------------------------------

    def action_open_cancel_wizard(self):
        """Open the cancel order wizard for this Shopify order."""
        self.ensure_one()
        if self.financial_status in ('voided', 'refunded'):
            raise UserError(_('Order %s is already cancelled/refunded.') % self.name)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Cancel Shopify Order'),
            'res_model': 'shopify.cancel.order.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_shopify_order_id': self.id},
        }

    def action_cancel_on_shopify(self, reason='OTHER', refund=True, restock=True,
                                  notify_customer=True, staff_note='',
                                  cancel_odoo_order=False):
        """
        Cancel this order on Shopify via the orderCancel GraphQL mutation.

        :param reason: OrderCancelReason enum — CUSTOMER | DECLINED | FRAUD |
                       INVENTORY | OTHER | STAFF
        :param refund: Whether to issue a refund.
        :param restock: Whether to restock line item quantities.
        :param notify_customer: Whether to send a cancellation email.
        :param staff_note: Optional internal note shown in the Shopify timeline.
        :param cancel_odoo_order: If True, also cancels the linked Odoo sale order.
        """
        self.ensure_one()
        if not self.shopify_order_gid:
            raise UserError(_('Order %s has no Shopify GID — cannot cancel.') % self.name)

        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        # If Odoo is sending cancellation email, suppress Shopify's
        if workflow and workflow.suppress_shopify_emails:
            notify_customer = False

        variables = {
            'orderId':        self.shopify_order_gid,
            'reason':         reason,
            'refund':         refund,
            'restock':        restock,
            'notifyCustomer': notify_customer,
            'staffNote':      staff_note or '',
        }

        data = self.instance_id._graphql_request(MUTATION_ORDER_CANCEL, variables=variables)
        result = data.get('orderCancel', {})

        # Check both error types Shopify can return
        errors = result.get('orderCancelUserErrors', []) or result.get('userErrors', [])
        if errors:
            raise UserError(_('Shopify cancel error: %s') % errors[0]['message'])

        order_node = result.get('order', {})
        new_financial = _FINANCIAL_STATUS_MAP.get(
            order_node.get('displayFinancialStatus', ''), 'voided'
        )
        self.write({
            'financial_status':   new_financial,
            'fulfillment_status': 'restocked' if restock else self.fulfillment_status,
        })

        # Log in chatter
        self.message_post(
            body=_('Order cancelled on Shopify. Reason: %s. Refund: %s. Restock: %s.') % (
                reason, _('Yes') if refund else _('No'), _('Yes') if restock else _('No')
            )
        )
        if workflow:
            workflow.notify_order_cancelled(self)

        # Optionally cancel the linked Odoo sale order
        if cancel_odoo_order and self.odoo_sale_order_id:
            so = self.odoo_sale_order_id
            if so.state not in ('cancel', 'done'):
                so.action_cancel()
                _logger.info('Cancelled Odoo sale order %s linked to Shopify order %s',
                             so.name, self.name)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title':   _('Order Cancelled'),
                'message': _('%s has been cancelled on Shopify.') % self.name,
                'type':    'success',
            },
        }

    # ------------------------------------------------------------------
    # Pickup order actions
    # ------------------------------------------------------------------

    def action_mark_ready_for_pickup(self):
        """Notify the customer their order is ready for pickup."""
        self.ensure_one()
        if not self.is_pickup_order:
            raise UserError(_('This is not a pickup order.'))
        if not self.pickup_fulfillment_order_gid:
            raise UserError(_('No fulfillment order GID — re-sync this order first.'))

        data = self.instance_id._graphql_request(
            MUTATION_FULFILLMENT_ORDER_MARK_READY,
            variables={'id': self.pickup_fulfillment_order_gid},
        )
        result = data.get('fulfillmentOrderMarkAsReady', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Mark ready error: %s') % errors[0]['message'])

        self.write({'pickup_status': 'ready'})
        self.message_post(body=_('Order marked as Ready for Pickup. Customer notified.'))
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', self.instance_id.id)], limit=1
        )
        if workflow and workflow.suppress_shopify_emails:
            workflow.notify_pickup_ready(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Ready for Pickup'),
                'message': _('%s is ready. Customer notified via Shopify.') % self.name,
                'type': 'success',
            },
        }

    def action_mark_picked_up(self):
        """Close the fulfillment order — mark as picked up."""
        self.ensure_one()
        if not self.is_pickup_order:
            raise UserError(_('This is not a pickup order.'))
        if not self.pickup_fulfillment_order_gid:
            raise UserError(_('No fulfillment order GID — re-sync this order first.'))

        data = self.instance_id._graphql_request(
            MUTATION_FULFILLMENT_ORDER_CLOSE,
            variables={
                'id': self.pickup_fulfillment_order_gid,
                'message': 'Picked up in store',
            },
        )
        result = data.get('fulfillmentOrderClose', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Mark picked up error: %s') % errors[0]['message'])

        self.write({
            'pickup_status': 'picked_up',
            'fulfillment_status': 'fulfilled',
        })
        self.message_post(body=_('Order picked up by customer.'))

    def action_fetch_metafields(self):
        """Fetch all metafields for this order from Shopify."""
        from .shopify_metafield import QUERY_ORDER_METAFIELDS
        for rec in self:
            if not rec.shopify_order_gid:
                continue
            cursor = None
            while True:
                variables = {'orderId': rec.shopify_order_gid, 'first': 50, 'after': cursor}
                data = rec.instance_id._graphql_request(QUERY_ORDER_METAFIELDS, variables=variables)
                connection = (data.get('order') or {}).get('metafields', {})
                edges = connection.get('edges', [])
                self.env['shopify.metafield']._upsert_from_nodes(
                    edges, rec.instance_id, shopify_order_id=rec.id
                )
                page_info = connection.get('pageInfo', {})
                if not page_info.get('hasNextPage'):
                    break
                cursor = page_info.get('endCursor')

    @api.model
    def cron_import_orders(self):
        instances = self.env['shopify.instance'].search([('state', '=', 'connected'), ('sync_orders', '=', True)])
        for instance in instances:
            try:
                self.import_orders_from_shopify(instance, days_back=1)
            except Exception as e:
                _logger.error('Order import cron failed for %s: %s', instance.name, e)


class ShopifyOrderLine(models.Model):
    _name = 'shopify.order.line'
    _description = 'Shopify Order Line'

    order_id = fields.Many2one('shopify.order', string='Order', ondelete='cascade')
    shopify_line_id = fields.Char(string='Shopify Line ID')
    product_name = fields.Char(string='Product')
    variant_title = fields.Char(string='Variant')
    sku = fields.Char(string='SKU')
    shopify_variant_id = fields.Char(string='Variant ID (numeric)')
    shopify_variant_gid = fields.Char(string='Variant GID')
    quantity = fields.Integer(string='Qty')
    price = fields.Float(string='Unit Price')
    total_discount = fields.Float(string='Discount')
    odoo_product_id = fields.Many2one('product.product', string='Odoo Product')
    tax_ids = fields.Many2many('account.tax', string='Taxes')

    # ── Net-margin intelligence (revenue vs Odoo COGS) ───────────────────────
    instance_id = fields.Many2one(
        related='order_id.instance_id', store=True, string='Store')
    order_date = fields.Datetime(
        related='order_id.shopify_order_date', store=True, string='Order Date')
    margin_revenue = fields.Float(compute='_compute_margin', store=True, string='Revenue')
    margin_cost = fields.Float(compute='_compute_margin', store=True, string='COGS')
    margin_value = fields.Float(compute='_compute_margin', store=True, string='Margin')
    margin_pct = fields.Float(compute='_compute_margin', store=True, string='Margin %')

    @api.depends('price', 'quantity', 'total_discount',
                 'odoo_product_id', 'odoo_product_id.standard_price')
    def _compute_margin(self):
        for line in self:
            rev = (line.price or 0.0) * (line.quantity or 0) - (line.total_discount or 0.0)
            cost = (line.odoo_product_id.standard_price or 0.0) * (line.quantity or 0)
            line.margin_revenue = rev
            line.margin_cost = cost
            line.margin_value = rev - cost
            line.margin_pct = (line.margin_value / rev * 100.0) if rev else 0.0
