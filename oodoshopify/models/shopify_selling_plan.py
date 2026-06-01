import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

QUERY_SELLING_PLAN_GROUPS = '''
    query GetSellingPlanGroups($first: Int!, $after: String) {
        sellingPlanGroups(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    name
                    merchantCode
                    options
                    sellingPlans(first: 20) {
                        edges {
                            node {
                                id
                                name
                                options
                                billingPolicy {
                                    ... on SellingPlanRecurringBillingPolicy { interval intervalCount }
                                }
                            }
                        }
                    }
                    productsCount { count }
                }
            }
        }
    }
'''

MUTATION_SELLING_PLAN_GROUP_CREATE = '''
    mutation sellingPlanGroupCreate($input: SellingPlanGroupInput!) {
        sellingPlanGroupCreate(input: $input) {
            sellingPlanGroup { id name }
            userErrors { field message }
        }
    }
'''

MUTATION_SELLING_PLAN_GROUP_DELETE = '''
    mutation sellingPlanGroupDelete($id: ID!) {
        sellingPlanGroupDelete(id: $id) {
            deletedSellingPlanGroupId
            userErrors { field message }
        }
    }
'''


class ShopifySellingPlanGroup(models.Model):
    _name = 'shopify.selling.plan.group'
    _description = 'Shopify Selling Plan Group (Subscriptions)'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(string='Name', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_group_id  = fields.Char(string='Group ID', readonly=True)
    shopify_group_gid = fields.Char(string='Group GID', readonly=True)

    merchant_code = fields.Char(string='Merchant Code',
                                help='Internal code for the subscription program.')
    products_count = fields.Integer(string='Products', readonly=True)
    plan_ids = fields.One2many('shopify.selling.plan', 'group_id', string='Plans')

    _unique_group_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_group_id)',
        'This selling plan group already exists for this instance.',
    )

    def action_create_on_shopify(self):
        self.ensure_one()
        if self.shopify_group_gid:
            raise UserError(_('Already created on Shopify.'))
        if not self.plan_ids:
            raise UserError(_('Add at least one selling plan first.'))

        plans = []
        for plan in self.plan_ids:
            plans.append({
                'name': plan.name,
                'options': plan.option_label or 'Delivery',
                'category': 'SUBSCRIPTION',
                'billingPolicy': {
                    'recurring': {'interval': plan.interval, 'intervalCount': plan.interval_count}
                },
                'deliveryPolicy': {
                    'recurring': {'interval': plan.interval, 'intervalCount': plan.interval_count}
                },
                'pricingPolicies': [{
                    'fixed': {
                        'adjustmentType': 'PERCENTAGE',
                        'adjustmentValue': {'percentage': plan.discount_percentage},
                    }
                }] if plan.discount_percentage else [],
            })

        group_input = {
            'name': self.name,
            'merchantCode': self.merchant_code or self.name,
            'options': ['Delivery frequency'],
            'sellingPlansToCreate': plans,
        }
        data = self.instance_id._graphql_request(
            MUTATION_SELLING_PLAN_GROUP_CREATE, variables={'input': group_input}
        )
        result = data.get('sellingPlanGroupCreate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Selling plan error: %s') % errors[0]['message'])
        node = result.get('sellingPlanGroup', {})
        gid = node.get('id', '')
        self.write({
            'shopify_group_gid': gid,
            'shopify_group_id': self.instance_id._gid_to_id(gid),
        })
        self.message_post(body=_('Subscription plan group created on Shopify.'))

    def action_delete_from_shopify(self):
        self.ensure_one()
        if self.shopify_group_gid:
            self.instance_id._graphql_request(
                MUTATION_SELLING_PLAN_GROUP_DELETE, variables={'id': self.shopify_group_gid}
            )
        self.unlink()

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            data = instance._graphql_request(
                QUERY_SELLING_PLAN_GROUPS, variables={'first': 50, 'after': cursor}
            )
            connection = data.get('sellingPlanGroups', {})
            for edge in connection.get('edges', []):
                self._upsert(instance, edge['node'])
                imported += 1
            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        return imported

    def _upsert(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_group_id', '=', numeric_id),
        ], limit=1)
        vals = {
            'name': node.get('name', ''),
            'instance_id': instance.id,
            'shopify_group_id': numeric_id,
            'shopify_group_gid': gid,
            'merchant_code': node.get('merchantCode', ''),
            'products_count': (node.get('productsCount') or {}).get('count', 0),
        }
        group = existing or self.create(vals)
        if existing:
            existing.write(vals)
        # Sync plans
        group.plan_ids.unlink()
        for edge in node.get('sellingPlans', {}).get('edges', []):
            p = edge['node']
            billing = p.get('billingPolicy') or {}
            self.env['shopify.selling.plan'].create({
                'group_id': group.id,
                'name': p.get('name', ''),
                'interval': billing.get('interval', 'MONTH'),
                'interval_count': billing.get('intervalCount', 1),
            })
        return group


class ShopifySellingPlan(models.Model):
    _name = 'shopify.selling.plan'
    _description = 'Shopify Selling Plan'

    group_id = fields.Many2one('shopify.selling.plan.group', ondelete='cascade')
    name = fields.Char(string='Plan Name', required=True)
    option_label = fields.Char(string='Option Label', default='Delivery every')
    interval = fields.Selection([
        ('DAY',   'Day'),
        ('WEEK',  'Week'),
        ('MONTH', 'Month'),
        ('YEAR',  'Year'),
    ], string='Interval', default='MONTH', required=True)
    interval_count = fields.Integer(string='Every N Intervals', default=1)
    discount_percentage = fields.Float(string='Subscription Discount %')
