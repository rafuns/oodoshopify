import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

QUERY_GIFT_CARDS = '''
    query GetGiftCards($first: Int!, $after: String) {
        giftCards(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    lastCharacters
                    enabled
                    balance { amount currencyCode }
                    initialValue { amount currencyCode }
                    createdAt
                    expiresOn
                    note
                    customer { id email }
                }
            }
        }
    }
'''

MUTATION_GIFT_CARD_CREATE = '''
    mutation giftCardCreate($input: GiftCardCreateInput!) {
        giftCardCreate(input: $input) {
            giftCard {
                id
                lastCharacters
                balance { amount currencyCode }
            }
            giftCardCode
            userErrors { field message }
        }
    }
'''

MUTATION_GIFT_CARD_UPDATE = '''
    mutation giftCardUpdate($id: ID!, $input: GiftCardUpdateInput!) {
        giftCardUpdate(id: $id, input: $input) {
            giftCard { id }
            userErrors { field message }
        }
    }
'''

MUTATION_GIFT_CARD_DEACTIVATE = '''
    mutation giftCardDeactivate($id: ID!) {
        giftCardDeactivate(id: $id) {
            giftCard { id enabled }
            userErrors { field message }
        }
    }
'''


class ShopifyGiftCard(models.Model):
    _name = 'shopify.gift.card'
    _description = 'Shopify Gift Card'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', required=True, default='Gift Card')
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_gift_card_id  = fields.Char(string='Gift Card ID', readonly=True)
    shopify_gift_card_gid = fields.Char(string='Gift Card GID', readonly=True)

    last_characters = fields.Char(string='Last 4 Chars', readonly=True)
    generated_code = fields.Char(string='Full Code', readonly=True,
                                 help='Only shown once at creation time.')
    initial_value = fields.Float(string='Initial Value')
    balance = fields.Float(string='Current Balance', readonly=True)
    currency = fields.Char(string='Currency')
    enabled = fields.Boolean(string='Enabled', default=True)
    expires_on = fields.Date(string='Expires On')
    note = fields.Char(string='Note')

    customer_email = fields.Char(string='Customer Email')
    shopify_customer_id = fields.Many2one('shopify.customer', string='Customer')

    _unique_gift_card_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_gift_card_id)',
        'This gift card already exists for this instance.',
    )

    def action_create_on_shopify(self):
        self.ensure_one()
        if self.shopify_gift_card_gid:
            raise UserError(_('Gift card already created on Shopify.'))
        if self.initial_value <= 0:
            raise UserError(_('Initial value must be greater than zero.'))

        gc_input = {'initialValue': str(self.initial_value)}
        if self.note:
            gc_input['note'] = self.note
        if self.expires_on:
            gc_input['expiresOn'] = self.expires_on.isoformat()
        # Link to a Shopify customer if mapped
        if self.shopify_customer_id and self.shopify_customer_id.shopify_customer_gid:
            gc_input['customerId'] = self.shopify_customer_id.shopify_customer_gid

        data = self.instance_id._graphql_request(
            MUTATION_GIFT_CARD_CREATE, variables={'input': gc_input}
        )
        result = data.get('giftCardCreate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Gift card error: %s') % errors[0]['message'])

        node = result.get('giftCard', {})
        gid = node.get('id', '')
        balance = node.get('balance', {})
        self.write({
            'shopify_gift_card_gid': gid,
            'shopify_gift_card_id': self.instance_id._gid_to_id(gid),
            'generated_code': result.get('giftCardCode', ''),
            'last_characters': node.get('lastCharacters', ''),
            'balance': float(balance.get('amount', self.initial_value)),
            'currency': balance.get('currencyCode', self.currency),
        })
        self.message_post(body=_('Gift card created on Shopify. Code: %s') % (result.get('giftCardCode') or '—'))

    def action_deactivate(self):
        self.ensure_one()
        if not self.shopify_gift_card_gid:
            raise UserError(_('Not yet created on Shopify.'))
        data = self.instance_id._graphql_request(
            MUTATION_GIFT_CARD_DEACTIVATE, variables={'id': self.shopify_gift_card_gid}
        )
        errors = data.get('giftCardDeactivate', {}).get('userErrors', [])
        if errors:
            raise UserError(_('Deactivate error: %s') % errors[0]['message'])
        self.write({'enabled': False})

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            data = instance._graphql_request(
                QUERY_GIFT_CARDS, variables={'first': 50, 'after': cursor}
            )
            connection = data.get('giftCards', {})
            for edge in connection.get('edges', []):
                self._upsert(instance, edge['node'])
                imported += 1
            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        _logger.info('Imported %d gift cards from %s', imported, instance.name)
        return imported

    def _upsert(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_gift_card_id', '=', numeric_id),
        ], limit=1)
        balance = node.get('balance', {})
        initial = node.get('initialValue', {})
        vals = {
            'name': f"Gift Card ****{node.get('lastCharacters', '')}",
            'instance_id': instance.id,
            'shopify_gift_card_id': numeric_id,
            'shopify_gift_card_gid': gid,
            'last_characters': node.get('lastCharacters', ''),
            'balance': float(balance.get('amount', 0)),
            'initial_value': float(initial.get('amount', 0)),
            'currency': balance.get('currencyCode', ''),
            'enabled': node.get('enabled', True),
            'note': node.get('note', ''),
            'customer_email': (node.get('customer') or {}).get('email', ''),
        }
        expires = (node.get('expiresOn') or '')
        if expires:
            vals['expires_on'] = expires
        if existing:
            existing.write(vals)
        else:
            self.create(vals)
