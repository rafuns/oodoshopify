import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_DISCOUNT_NODES = '''
    query GetDiscounts($first: Int!, $after: String) {
        discountNodes(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    discount {
                        __typename
                        ... on DiscountCodeBasic {
                            title
                            status
                            startsAt
                            endsAt
                            usageLimit
                            asyncUsageCount
                            codes(first: 1) { edges { node { code } } }
                            customerGets {
                                value {
                                    ... on DiscountPercentage { percentage }
                                    ... on DiscountAmount { amount { amount currencyCode } }
                                }
                            }
                        }
                        ... on DiscountAutomaticBasic {
                            title
                            status
                            startsAt
                            endsAt
                            customerGets {
                                value {
                                    ... on DiscountPercentage { percentage }
                                    ... on DiscountAmount { amount { amount currencyCode } }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
'''

MUTATION_CODE_CREATE = '''
    mutation discountCodeBasicCreate($basicCodeDiscount: DiscountCodeBasicInput!) {
        discountCodeBasicCreate(basicCodeDiscount: $basicCodeDiscount) {
            codeDiscountNode { id }
            userErrors { field message }
        }
    }
'''

MUTATION_CODE_UPDATE = '''
    mutation discountCodeBasicUpdate($id: ID!, $basicCodeDiscount: DiscountCodeBasicInput!) {
        discountCodeBasicUpdate(id: $id, basicCodeDiscount: $basicCodeDiscount) {
            codeDiscountNode { id }
            userErrors { field message }
        }
    }
'''

MUTATION_AUTOMATIC_CREATE = '''
    mutation discountAutomaticBasicCreate($automaticBasicDiscount: DiscountAutomaticBasicInput!) {
        discountAutomaticBasicCreate(automaticBasicDiscount: $automaticBasicDiscount) {
            automaticDiscountNode { id }
            userErrors { field message }
        }
    }
'''

MUTATION_DISCOUNT_DELETE = '''
    mutation discountCodeDelete($id: ID!) {
        discountCodeDelete(id: $id) {
            deletedCodeDiscountId
            userErrors { field message }
        }
    }
'''

MUTATION_AUTOMATIC_DELETE = '''
    mutation discountAutomaticDelete($id: ID!) {
        discountAutomaticDelete(id: $id) {
            deletedAutomaticDiscountId
            userErrors { field message }
        }
    }
'''

_STATUS_MAP = {
    'ACTIVE':    'active',
    'EXPIRED':   'expired',
    'SCHEDULED': 'scheduled',
}


class ShopifyDiscount(models.Model):
    _name = 'shopify.discount'
    _description = 'Shopify Discount'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Title', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_discount_id  = fields.Char(string='Discount ID', readonly=True)
    shopify_discount_gid = fields.Char(string='Discount GID', readonly=True)

    discount_type = fields.Selection([
        ('code',      'Discount Code (customer enters code)'),
        ('automatic', 'Automatic (applied at checkout)'),
    ], string='Type', default='code', required=True)

    code = fields.Char(
        string='Discount Code',
        help='The code customers enter at checkout (code discounts only).',
    )
    value_type = fields.Selection([
        ('percentage', 'Percentage'),
        ('fixed',      'Fixed Amount'),
    ], string='Value Type', default='percentage', required=True)
    value = fields.Float(
        string='Value',
        help='For percentage: 10 = 10%. For fixed: amount in store currency.',
    )

    starts_at = fields.Datetime(string='Starts At', default=fields.Datetime.now)
    ends_at   = fields.Datetime(string='Ends At')
    usage_limit = fields.Integer(
        string='Usage Limit', help='Max number of times this code can be used (0 = unlimited).',
    )
    used_count = fields.Integer(string='Times Used', readonly=True)

    status = fields.Selection([
        ('draft',     'Draft (local)'),
        ('active',    'Active'),
        ('scheduled', 'Scheduled'),
        ('expired',   'Expired'),
    ], string='Status', default='draft', tracking=True)

    _unique_discount_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_discount_id)',
        'This discount already exists for this instance.',
    )

    # ------------------------------------------------------------------
    # Build input
    # ------------------------------------------------------------------

    def _build_customer_gets(self):
        """Build the customerGets value structure."""
        self.ensure_one()
        if self.value_type == 'percentage':
            value = {'percentage': self.value / 100.0}
        else:
            value = {'discountAmount': {'amount': str(self.value), 'appliesOnEachItem': False}}
        return {
            'value': value,
            'items': {'all': True},
        }

    def _build_code_input(self):
        self.ensure_one()
        if not self.code:
            raise UserError(_('A discount code is required for code discounts.'))
        inp = {
            'title': self.name,
            'code': self.code,
            'startsAt': self.starts_at.isoformat() if self.starts_at else None,
            'customerSelection': {'all': True},
            'customerGets': self._build_customer_gets(),
            'appliesOncePerCustomer': False,
        }
        if self.ends_at:
            inp['endsAt'] = self.ends_at.isoformat()
        if self.usage_limit:
            inp['usageLimit'] = self.usage_limit
        return inp

    def _build_automatic_input(self):
        self.ensure_one()
        inp = {
            'title': self.name,
            'startsAt': self.starts_at.isoformat() if self.starts_at else None,
            'customerGets': self._build_customer_gets(),
        }
        if self.ends_at:
            inp['endsAt'] = self.ends_at.isoformat()
        return inp

    # ------------------------------------------------------------------
    # Push to Shopify
    # ------------------------------------------------------------------

    def action_push_to_shopify(self):
        for rec in self:
            try:
                if rec.discount_type == 'code':
                    rec._push_code_discount()
                else:
                    rec._push_automatic_discount()
                rec.message_post(body=_('Discount pushed to Shopify.'))
            except UserError:
                raise

    def _push_code_discount(self):
        inp = self._build_code_input()
        if self.shopify_discount_gid:
            data = self.instance_id._graphql_request(
                MUTATION_CODE_UPDATE,
                variables={'id': self.shopify_discount_gid, 'basicCodeDiscount': inp},
            )
            result = data.get('discountCodeBasicUpdate', {})
        else:
            data = self.instance_id._graphql_request(
                MUTATION_CODE_CREATE, variables={'basicCodeDiscount': inp}
            )
            result = data.get('discountCodeBasicCreate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Shopify error: %s') % errors[0]['message'])
        node = result.get('codeDiscountNode', {})
        gid = node.get('id', '')
        self.write({
            'shopify_discount_gid': gid,
            'shopify_discount_id': self.instance_id._gid_to_id(gid),
            'status': 'active',
        })

    def _push_automatic_discount(self):
        inp = self._build_automatic_input()
        data = self.instance_id._graphql_request(
            MUTATION_AUTOMATIC_CREATE, variables={'automaticBasicDiscount': inp}
        )
        result = data.get('discountAutomaticBasicCreate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Shopify error: %s') % errors[0]['message'])
        node = result.get('automaticDiscountNode', {})
        gid = node.get('id', '')
        self.write({
            'shopify_discount_gid': gid,
            'shopify_discount_id': self.instance_id._gid_to_id(gid),
            'status': 'active',
        })

    def action_delete_from_shopify(self):
        self.ensure_one()
        if self.shopify_discount_gid:
            mutation = MUTATION_DISCOUNT_DELETE if self.discount_type == 'code' else MUTATION_AUTOMATIC_DELETE
            self.instance_id._graphql_request(
                mutation, variables={'id': self.shopify_discount_gid}
            )
        self.unlink()

    # ------------------------------------------------------------------
    # Import existing discounts
    # ------------------------------------------------------------------

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            count, cursor, has_next = self.import_discounts_page(instance, cursor)
            imported += count
            if not has_next:
                break
        _logger.info('Imported %d discounts from %s', imported, instance.name)
        return imported

    def import_discounts_page(self, instance, cursor=None):
        """Import one page of discounts → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small."""
        data = instance._graphql_request(
            QUERY_DISCOUNT_NODES, variables={'first': 50, 'after': cursor})
        connection = data.get('discountNodes', {})
        imported = 0
        for edge in connection.get('edges', []):
            if self._upsert_discount(instance, edge['node']):
                imported += 1
        page_info = connection.get('pageInfo', {})
        return imported, page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_discount(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        discount = node.get('discount') or {}
        typename = discount.get('__typename', '')
        if typename not in ('DiscountCodeBasic', 'DiscountAutomaticBasic'):
            return False  # skip BXGY / free shipping / app discounts for now

        is_code = typename == 'DiscountCodeBasic'

        # Extract value
        value_obj = (discount.get('customerGets') or {}).get('value') or {}
        if 'percentage' in value_obj:
            value_type = 'percentage'
            value = float(value_obj.get('percentage', 0)) * 100
        elif 'amount' in value_obj:
            value_type = 'fixed'
            value = float((value_obj.get('amount') or {}).get('amount', 0))
        else:
            value_type = 'percentage'
            value = 0.0

        code = ''
        if is_code:
            code_edges = (discount.get('codes') or {}).get('edges', [])
            if code_edges:
                code = code_edges[0]['node'].get('code', '')

        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_discount_id', '=', numeric_id),
        ], limit=1)

        vals = {
            'name': discount.get('title', f'Discount/{numeric_id}'),
            'instance_id': instance.id,
            'shopify_discount_id': numeric_id,
            'shopify_discount_gid': gid,
            'discount_type': 'code' if is_code else 'automatic',
            'code': code,
            'value_type': value_type,
            'value': value,
            'used_count': discount.get('asyncUsageCount', 0) if is_code else 0,
            'usage_limit': discount.get('usageLimit', 0) if is_code else 0,
            'status': _STATUS_MAP.get(discount.get('status', 'ACTIVE'), 'active'),
        }
        starts = (discount.get('startsAt') or '').replace('T', ' ').replace('Z', '')
        ends   = (discount.get('endsAt') or '').replace('T', ' ').replace('Z', '')
        if starts:
            vals['starts_at'] = starts
        if ends:
            vals['ends_at'] = ends

        if existing:
            existing.write(vals)
        else:
            self.create(vals)
        return True
