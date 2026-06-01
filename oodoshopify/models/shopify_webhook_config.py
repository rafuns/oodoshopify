import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

MUTATION_WEBHOOK_CREATE = '''
    mutation webhookSubscriptionCreate($topic: WebhookSubscriptionTopic!, $callbackUrl: URL!) {
        webhookSubscriptionCreate(
            topic: $topic
            webhookSubscription: { callbackUrl: $callbackUrl, format: JSON }
        ) {
            webhookSubscription { id topic callbackUrl }
            userErrors { field message }
        }
    }
'''

MUTATION_WEBHOOK_DELETE = '''
    mutation webhookSubscriptionDelete($id: ID!) {
        webhookSubscriptionDelete(id: $id) {
            deletedWebhookSubscriptionId
            userErrors { field message }
        }
    }
'''

QUERY_WEBHOOKS = '''
    query GetWebhooks($first: Int!) {
        webhookSubscriptions(first: $first) {
            edges {
                node {
                    id
                    topic
                    callbackUrl
                    format
                    createdAt
                }
            }
        }
    }
'''

# All supported webhook topics with their Odoo callback paths
WEBHOOK_TOPIC_PATHS = [
    ('ORDERS_CREATE',           '/shopify/webhook/orders/create',       'Orders — Create'),
    ('ORDERS_UPDATED',          '/shopify/webhook/orders/updated',      'Orders — Updated'),
    ('ORDERS_PAID',             '/shopify/webhook/orders/paid',         'Orders — Paid'),
    ('ORDERS_FULFILLED',        '/shopify/webhook/orders/fulfilled',    'Orders — Fulfilled'),
    ('ORDERS_CANCELLED',        '/shopify/webhook/orders/cancelled',    'Orders — Cancelled'),
    ('PRODUCTS_CREATE',         '/shopify/webhook/products/create',     'Products — Create'),
    ('PRODUCTS_UPDATE',         '/shopify/webhook/products/update',     'Products — Update'),
    ('PRODUCTS_DELETE',         '/shopify/webhook/products/delete',     'Products — Delete'),
    ('CUSTOMERS_CREATE',        '/shopify/webhook/customers/create',    'Customers — Create'),
    ('CUSTOMERS_UPDATE',        '/shopify/webhook/customers/update',    'Customers — Update'),
    ('CUSTOMERS_DELETE',        '/shopify/webhook/customers/delete',    'Customers — Delete'),
    ('COLLECTIONS_UPDATE',      '/shopify/webhook/collections/update',  'Collections — Update'),
    ('INVENTORY_LEVELS_UPDATE', '/shopify/webhook/inventory/update',    'Inventory Levels — Update'),
    ('REFUNDS_CREATE',          '/shopify/webhook/refunds/create',      'Refunds — Create'),
    ('APP_UNINSTALLED',         '/shopify/webhook/app/uninstalled',     'App — Uninstalled'),
]

TOPIC_SELECTION = [(t[0], t[2]) for t in WEBHOOK_TOPIC_PATHS]
TOPIC_PATH_MAP  = {t[0]: t[1] for t in WEBHOOK_TOPIC_PATHS}


class ShopifyWebhookConfig(models.Model):
    """Per-instance webhook configuration with individual enable/disable toggles."""
    _name = 'shopify.webhook.config'
    _description = 'Shopify Webhook Configuration'
    _order = 'instance_id, topic'

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance',
        required=True, ondelete='cascade', index=True,
    )
    topic = fields.Selection(
        TOPIC_SELECTION, string='Topic', required=True,
    )
    topic_label = fields.Char(
        string='Webhook Event', compute='_compute_topic_label', store=True,
    )
    callback_path = fields.Char(
        string='Callback Path', readonly=True,
        help='The Odoo route that receives this webhook.',
    )
    callback_url = fields.Char(
        string='Full Callback URL', compute='_compute_callback_url',
    )

    is_enabled = fields.Boolean(
        string='Enabled', default=True,
        help='When enabled, this webhook is registered on Shopify. '
             'Disabling will delete the subscription from Shopify.',
    )

    # Shopify registration status
    shopify_webhook_id  = fields.Char(string='Shopify Webhook ID',  readonly=True)
    shopify_webhook_gid = fields.Char(string='Shopify Webhook GID', readonly=True)

    state = fields.Selection([
        ('draft',      'Not Registered'),
        ('active',     'Active'),
        ('disabled',   'Disabled'),
        ('error',      'Error'),
    ], string='Status', default='draft', readonly=True)

    last_error = fields.Char(string='Last Error', readonly=True)

    _unique_topic_per_instance = models.Constraint(
        'UNIQUE(instance_id, topic)',
        'Each webhook topic can only be configured once per instance.',
    )

    @api.depends('topic')
    def _compute_topic_label(self):
        label_map = {t[0]: t[2] for t in WEBHOOK_TOPIC_PATHS}
        for rec in self:
            rec.topic_label = label_map.get(rec.topic, rec.topic)

    @api.depends('instance_id', 'instance_id.webhook_token', 'callback_path')
    def _compute_callback_url(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        for rec in self:
            token = rec.instance_id.webhook_token if rec.instance_id else ''
            if base_url and rec.callback_path and token:
                rec.callback_url = f'{base_url}{rec.callback_path}?token={token}'
            else:
                rec.callback_url = ''

    # ------------------------------------------------------------------
    # Register / delete on Shopify
    # ------------------------------------------------------------------

    def action_register(self):
        """Register this webhook on Shopify (if enabled and not yet registered)."""
        for rec in self:
            if not rec.is_enabled:
                continue
            if rec.shopify_webhook_gid:
                continue  # already registered
            rec._do_register()

    def action_delete(self):
        """Delete this webhook from Shopify and clear registration info."""
        for rec in self:
            if not rec.shopify_webhook_gid:
                rec.write({'state': 'draft', 'last_error': False})
                continue
            rec._do_delete()

    def _do_register(self):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        token = self.instance_id.webhook_token
        callback_url = f'{base_url}{self.callback_path}?token={token}'
        try:
            result = self.instance_id._graphql_request(
                MUTATION_WEBHOOK_CREATE,
                variables={'topic': self.topic, 'callbackUrl': callback_url},
            )
            sub = result.get('webhookSubscriptionCreate', {})
            user_errors = sub.get('userErrors', [])
            if user_errors:
                self.write({'state': 'error', 'last_error': user_errors[0]['message']})
            else:
                webhook = sub.get('webhookSubscription', {})
                gid = webhook.get('id', '')
                self.write({
                    'shopify_webhook_gid': gid,
                    'shopify_webhook_id':  self.instance_id._gid_to_id(gid),
                    'state': 'active',
                    'last_error': False,
                })
                _logger.info('Registered webhook %s for instance %s', self.topic, self.instance_id.name)
        except UserError as e:
            self.write({'state': 'error', 'last_error': str(e)})

    def _do_delete(self):
        self.ensure_one()
        try:
            result = self.instance_id._graphql_request(
                MUTATION_WEBHOOK_DELETE,
                variables={'id': self.shopify_webhook_gid},
            )
            sub = result.get('webhookSubscriptionDelete', {})
            user_errors = sub.get('userErrors', [])
            if user_errors:
                self.write({'state': 'error', 'last_error': user_errors[0]['message']})
            else:
                self.write({
                    'shopify_webhook_id':  False,
                    'shopify_webhook_gid': False,
                    'state': 'disabled',
                    'last_error': False,
                })
                _logger.info('Deleted webhook %s for instance %s', self.topic, self.instance_id.name)
        except UserError as e:
            self.write({'state': 'error', 'last_error': str(e)})

    # ------------------------------------------------------------------
    # Toggle: called when user flips is_enabled in the list
    # ------------------------------------------------------------------

    def write(self, vals):
        res = super().write(vals)
        if 'is_enabled' in vals:
            for rec in self:
                if vals['is_enabled'] and not rec.shopify_webhook_gid:
                    rec._do_register()
                elif not vals['is_enabled'] and rec.shopify_webhook_gid:
                    rec._do_delete()
        return res

    # ------------------------------------------------------------------
    # Sync status from Shopify (fetch all registered webhooks)
    # ------------------------------------------------------------------

    @api.model
    def sync_from_shopify(self, instance):
        """Fetch registered webhooks from Shopify and update config records."""
        data = instance._graphql_request(QUERY_WEBHOOKS, variables={'first': 50})
        edges = data.get('webhookSubscriptions', {}).get('edges', [])

        # Match registered webhooks by the opaque token (not the sequential instance ID)
        token_param = f'token={instance.webhook_token}'

        # Build a map of topic → shopify webhook node for this instance
        instance_webhooks = {}
        for edge in edges:
            node = edge['node']
            cb = node.get('callbackUrl', '')
            if token_param in cb:
                instance_webhooks[node['topic']] = node

        for config in instance.webhook_config_ids:
            node = instance_webhooks.get(config.topic)
            if node:
                gid = node['id']
                config.write({
                    'shopify_webhook_gid': gid,
                    'shopify_webhook_id':  instance._gid_to_id(gid),
                    'state': 'active' if config.is_enabled else 'disabled',
                    'last_error': False,
                })
            else:
                if config.shopify_webhook_gid:
                    # Was registered but no longer found on Shopify
                    config.write({
                        'shopify_webhook_gid': False,
                        'shopify_webhook_id':  False,
                        'state': 'draft',
                    })
