import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_ABANDONED_CHECKOUTS = '''
    query GetAbandonedCheckouts($first: Int!, $after: String, $query: String) {
        abandonedCheckouts(first: $first, after: $after, query: $query) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    createdAt
                    updatedAt
                    completedAt
                    abandonedCheckoutUrl
                    totalPriceSet { shopMoney { amount currencyCode } }
                    lineItems(first: 20) {
                        edges {
                            node {
                                id
                                title
                                quantity
                                variant {
                                    id
                                    price
                                    sku
                                }
                            }
                        }
                    }
                    customer {
                        id
                        firstName
                        lastName
                        email
                        phone
                    }
                }
            }
        }
    }
'''


class ShopifyAbandonedCheckout(models.Model):
    _name = 'shopify.abandoned.checkout'
    _description = 'Shopify Abandoned Checkout'
    _inherit = ['mail.thread']
    _order = 'created_at desc'

    name = fields.Char(string='Reference', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )

    # Shopify identity
    shopify_checkout_id  = fields.Char(string='Checkout ID', readonly=True)
    shopify_checkout_gid = fields.Char(string='Checkout GID', readonly=True)
    checkout_url = fields.Char(string='Checkout Recovery URL', readonly=True)

    # Customer
    email = fields.Char(string='Email')
    phone = fields.Char(string='Phone')
    customer_name = fields.Char(string='Customer')
    shopify_customer_gid = fields.Char(string='Customer GID')
    odoo_partner_id = fields.Many2one('res.partner', string='Odoo Contact')

    # Financials
    total_price = fields.Float(string='Cart Value')
    currency = fields.Char(string='Currency')

    # Dates
    created_at   = fields.Datetime(string='Created At', readonly=True)
    updated_at   = fields.Datetime(string='Updated At', readonly=True)
    completed_at = fields.Datetime(string='Recovered At', readonly=True,
                                    help='Set by Shopify when the checkout was completed.')

    # State
    state = fields.Selection([
        ('open',      'Abandoned'),
        ('recovered', 'Recovered — Order Placed'),
        ('lost',      'Lost'),
    ], string='State', default='open', tracking=True)

    # CRM
    odoo_lead_id = fields.Many2one('crm.lead', string='CRM Lead')

    # Lines
    line_ids = fields.One2many(
        'shopify.abandoned.checkout.line', 'checkout_id', string='Cart Items',
    )

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    @api.model
    def build_checkouts_query_filter(self, days_back=30):
        """Build the Shopify search 'query' string for an abandoned-checkout import.
        Exposed so the chunked background importer can reuse it across pages."""
        from datetime import datetime, timedelta
        since = (datetime.utcnow() - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
        return f'created_at:>={since}'

    def import_from_shopify(self, instance, days_back=30):
        """Fetch abandoned checkouts from Shopify and create/update records + CRM leads."""
        query_filter = self.build_checkouts_query_filter(days_back)

        cursor = None
        imported = 0
        while True:
            count, cursor, has_next = self.import_checkouts_page(instance, query_filter, cursor)
            imported += count
            if not has_next:
                break

        _logger.info('Imported %d abandoned checkouts from %s', imported, instance.name)
        return imported

    def import_checkouts_page(self, instance, query_filter, cursor=None):
        """Import one page of abandoned checkouts → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small."""
        variables = {'first': 50, 'after': cursor, 'query': query_filter}
        data = instance._graphql_request(QUERY_ABANDONED_CHECKOUTS, variables=variables)
        connection = data.get('abandonedCheckouts', {})
        edges = connection.get('edges', [])
        imported = 0
        for edge in edges:
            rec = self._upsert_checkout(instance, edge['node'])
            if rec:
                imported += 1
        page_info = connection.get('pageInfo', {})
        return imported, page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_checkout(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)

        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_checkout_id', '=', numeric_id),
        ], limit=1)

        # Parse customer info
        customer_node = node.get('customer') or {}
        first = customer_node.get('firstName', '') or ''
        last  = customer_node.get('lastName',  '') or ''
        customer_name = f'{first} {last}'.strip() or node.get('email', numeric_id)

        total_set = node.get('totalPriceSet', {}).get('shopMoney', {})
        completed_raw = (node.get('completedAt') or '').replace('T', ' ').replace('Z', '')
        created_raw   = (node.get('createdAt')   or '').replace('T', ' ').replace('Z', '')
        updated_raw   = (node.get('updatedAt')   or '').replace('T', ' ').replace('Z', '')

        state = 'recovered' if completed_raw else 'open'

        vals = {
            'name':                 f'Checkout/{numeric_id}',
            'instance_id':          instance.id,
            'shopify_checkout_id':  numeric_id,
            'shopify_checkout_gid': gid,
            'checkout_url':         node.get('abandonedCheckoutUrl', ''),
            'email':                node.get('email', '') or customer_node.get('email', ''),
            'phone':                node.get('phone', '') or customer_node.get('phone', ''),
            'customer_name':        customer_name,
            'shopify_customer_gid': customer_node.get('id', ''),
            'total_price':          float(total_set.get('amount', 0)),
            'currency':             total_set.get('currencyCode', ''),
            'created_at':           created_raw or False,
            'updated_at':           updated_raw or False,
            'completed_at':         completed_raw or False,
            'state':                state,
        }

        if existing:
            existing.write(vals)
            rec = existing
        else:
            rec = self.create(vals)

        rec._sync_lines(node.get('lineItems', {}).get('edges', []), instance)
        rec._match_partner()

        # Auto-create CRM lead for new open checkouts
        if not rec.odoo_lead_id and rec.state == 'open':
            rec._create_crm_lead(instance)

        # Auto-win lead if checkout was completed
        if rec.state == 'recovered' and rec.odoo_lead_id and rec.odoo_lead_id.active:
            rec._mark_lead_won(instance)

        return rec

    def _sync_lines(self, line_edges, instance):
        self.line_ids.unlink()
        for edge in line_edges:
            item = edge['node']
            variant = item.get('variant') or {}
            self.env['shopify.abandoned.checkout.line'].create({
                'checkout_id':       self.id,
                'product_title':     item.get('title', ''),
                'quantity':          item.get('quantity', 1),
                'unit_price':        float(variant.get('price', 0) or 0),
                'sku':               variant.get('sku', '') or '',
                'shopify_variant_gid': variant.get('id', '') or '',
            })

    def _match_partner(self):
        """Try to find an existing Odoo contact for this checkout."""
        if self.odoo_partner_id:
            return
        partner = False
        if self.email:
            partner = self.env['res.partner'].search([('email', '=', self.email)], limit=1)
        if not partner and self.phone:
            partner = self.env['res.partner'].search([('phone', '=', self.phone)], limit=1)
        if partner:
            self.odoo_partner_id = partner.id

    # ------------------------------------------------------------------
    # CRM lead creation
    # ------------------------------------------------------------------

    def _create_crm_lead(self, instance):
        """Create a crm.lead for this abandoned checkout."""
        self.ensure_one()
        if not self.env['ir.model'].search([('model', '=', 'crm.lead')], limit=1):
            _logger.warning('CRM module not installed — skipping lead creation.')
            return

        # Build description from cart items
        lines_text = '\n'.join(
            f'  • {line.product_title} × {line.quantity}  ({line.unit_price} {self.currency})'
            for line in self.line_ids
        )
        description = (
            f'Shopify Abandoned Checkout\n'
            f'Store: {instance.name}\n'
            f'Recovery URL: {self.checkout_url}\n\n'
            f'Cart:\n{lines_text}\n\n'
            f'Total: {self.total_price} {self.currency}'
        )

        lead_vals = {
            'name':              f'Abandoned Checkout — {self.customer_name or self.email}',
            'email_from':        self.email or '',
            'phone':             self.phone or '',
            'description':       description,
            'expected_revenue':  self.total_price,
            'probability':       10.0,
            'partner_id':        self.odoo_partner_id.id if self.odoo_partner_id else False,
        }

        # Apply instance-level CRM config
        if instance.crm_team_id:
            lead_vals['team_id'] = instance.crm_team_id.id
        if instance.crm_stage_id:
            lead_vals['stage_id'] = instance.crm_stage_id.id

        # Tag
        tag = self.env['crm.tag'].search([('name', '=', 'Shopify Abandoned')], limit=1)
        if not tag:
            tag = self.env['crm.tag'].create({'name': 'Shopify Abandoned'})
        lead_vals['tag_ids'] = [(4, tag.id)]

        lead = self.env['crm.lead'].create(lead_vals)
        self.write({'odoo_lead_id': lead.id})
        _logger.info('Created CRM lead %s for checkout %s', lead.name, self.name)

        # Send recovery email if enabled in workflow
        workflow = self.env['shopify.workflow'].search(
            [('instance_id', '=', instance.id)], limit=1
        )
        if workflow:
            workflow.notify_abandoned_checkout(self)

        return lead

    def _mark_lead_won(self, instance):
        """Move the linked CRM lead to the Won stage."""
        self.ensure_one()
        if not self.odoo_lead_id:
            return
        if instance.crm_won_stage_id:
            self.odoo_lead_id.write({'stage_id': instance.crm_won_stage_id.id})
        else:
            # Find a stage with is_won = True
            won_stage = self.env['crm.stage'].search([('is_won', '=', True)], limit=1)
            if won_stage:
                self.odoo_lead_id.write({'stage_id': won_stage.id})
        self.odoo_lead_id.action_set_won()
        _logger.info('Lead %s marked as Won (checkout %s recovered)', self.odoo_lead_id.name, self.name)

    # ------------------------------------------------------------------
    # Manual actions
    # ------------------------------------------------------------------

    def action_view_lead(self):
        self.ensure_one()
        if not self.odoo_lead_id:
            raise UserError(_('No CRM lead linked to this checkout.'))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'crm.lead',
            'res_id': self.odoo_lead_id.id,
            'view_mode': 'form',
        }

    def action_create_lead_manual(self):
        """Manually create a CRM lead for this checkout."""
        self.ensure_one()
        if self.odoo_lead_id:
            raise UserError(_('Lead already exists: %s') % self.odoo_lead_id.name)
        self._create_crm_lead(self.instance_id)
        return self.action_view_lead()

    def action_mark_lost(self):
        self.write({'state': 'lost'})
        if self.odoo_lead_id:
            self.odoo_lead_id.action_set_lost()

    def action_open_checkout_url(self):
        self.ensure_one()
        if not self.checkout_url:
            raise UserError(_('No checkout URL available.'))
        return {'type': 'ir.actions.act_url', 'url': self.checkout_url, 'target': 'new'}

    # ------------------------------------------------------------------
    # Cron
    # ------------------------------------------------------------------

    @api.model
    def cron_import_abandoned_checkouts(self):
        """Scheduled action: import abandoned checkouts for all connected instances."""
        instances = self.env['shopify.instance'].search([
            ('state', '=', 'connected'),
        ])
        for instance in instances:
            try:
                days_back = instance.abandoned_checkout_days_back or 30
                self.import_from_shopify(instance, days_back=days_back)
            except Exception as e:
                _logger.error(
                    'Abandoned checkout import failed for %s: %s', instance.name, e
                )

    @api.model
    def cron_check_recovered_checkouts(self):
        """Scheduled action: check open checkouts that may now be recovered."""
        open_checkouts = self.search([('state', '=', 'open'), ('odoo_lead_id', '!=', False)])
        for checkout in open_checkouts:
            if not checkout.shopify_checkout_gid:
                continue
            try:
                # Re-fetch just this checkout to see if completedAt is now set
                query = '''
                    query CheckCheckout($id: ID!) {
                        node(id: $id) {
                            ... on AbandonedCheckout {
                                id
                                completedAt
                            }
                        }
                    }
                '''
                data = checkout.instance_id._graphql_request(
                    query, variables={'id': checkout.shopify_checkout_gid}
                )
                node = data.get('node') or {}
                completed_raw = (node.get('completedAt') or '').replace('T', ' ').replace('Z', '')
                if completed_raw:
                    checkout.write({'completed_at': completed_raw, 'state': 'recovered'})
                    checkout._mark_lead_won(checkout.instance_id)
            except Exception as e:
                _logger.debug('Recovery check failed for checkout %s: %s', checkout.name, e)


class ShopifyAbandonedCheckoutLine(models.Model):
    _name = 'shopify.abandoned.checkout.line'
    _description = 'Abandoned Checkout Line'

    checkout_id       = fields.Many2one('shopify.abandoned.checkout', ondelete='cascade')
    product_title     = fields.Char(string='Product')
    sku               = fields.Char(string='SKU')
    shopify_variant_gid = fields.Char(string='Variant GID')
    quantity          = fields.Integer(string='Qty')
    unit_price        = fields.Float(string='Unit Price')
    subtotal          = fields.Float(compute='_compute_subtotal', string='Subtotal')

    @api.depends('quantity', 'unit_price')
    def _compute_subtotal(self):
        for rec in self:
            rec.subtotal = rec.quantity * rec.unit_price
