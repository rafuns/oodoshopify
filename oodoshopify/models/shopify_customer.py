import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL queries and mutations
# ---------------------------------------------------------------------------

QUERY_CUSTOMERS = '''
    query GetCustomers($first: Int!, $after: String) {
        customers(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    firstName
                    lastName
                    email
                    phone
                    numberOfOrders
                    amountSpent { amount currencyCode }
                    tags
                    verifiedEmail
                    taxExempt
                    emailMarketingConsent { marketingState }
                    defaultAddress {
                        address1
                        address2
                        city
                        zip
                        countryCodeV2
                        company
                    }
                }
            }
        }
    }
'''

MUTATION_CUSTOMER_CREATE = '''
    mutation customerCreate($input: CustomerInput!) {
        customerCreate(input: $input) {
            customer { id email }
            userErrors { field message }
        }
    }
'''

MUTATION_CUSTOMER_UPDATE = '''
    mutation customerUpdate($input: CustomerInput!) {
        customerUpdate(input: $input) {
            customer { id email }
            userErrors { field message }
        }
    }
'''


class ShopifyCustomer(models.Model):
    _name = 'shopify.customer'
    _description = 'Shopify Customer'
    _inherit = ['mail.thread']

    name = fields.Char(string='Name', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')
    shopify_customer_id = fields.Char(string='Shopify Customer ID (numeric)', readonly=True)
    shopify_customer_gid = fields.Char(string='Shopify GID', readonly=True)
    odoo_partner_id = fields.Many2one('res.partner', string='Odoo Contact')

    email = fields.Char(string='Email')
    phone = fields.Char(string='Phone')
    first_name = fields.Char(string='First Name')
    last_name = fields.Char(string='Last Name')
    orders_count = fields.Integer(string='Total Orders', readonly=True)
    total_spent = fields.Float(string='Total Spent', readonly=True)
    tags = fields.Char(string='Tags')
    accepts_marketing = fields.Boolean(string='Accepts Marketing')
    verified_email = fields.Boolean(string='Verified Email')
    tax_exempt = fields.Boolean(string='Tax Exempt')
    is_company = fields.Boolean(
        string='Is Company (B2B)',
        help='Auto-detected from Shopify company name or address company field.',
    )
    company_name = fields.Char(string='Company Name')

    # Store credit
    store_credit_balance = fields.Float(string='Store Credit Balance', readonly=True)
    store_credit_account_gid = fields.Char(string='Store Credit Account GID', readonly=True)

    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('synced', 'Synced'),
        ('error', 'Error'),
    ], default='pending', tracking=True)
    last_synced = fields.Datetime(readonly=True)
    metafield_ids = fields.One2many('shopify.metafield', 'shopify_customer_id', string='Metafields')

    # ------------------------------------------------------------------
    # Import: Shopify → Odoo  (GraphQL cursor pagination)
    # ------------------------------------------------------------------

    @api.model
    def import_customers_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            count, cursor, has_next = self.import_customers_page(instance, cursor)
            imported += count
            if not has_next:
                break
        _logger.info('Imported %d customers from %s', imported, instance.name)
        return imported

    def import_customers_page(self, instance, cursor=None):
        """Import one page of customers → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small."""
        variables = {'first': 50, 'after': cursor}
        data = instance._graphql_request(QUERY_CUSTOMERS, variables=variables)
        connection = data.get('customers', {})
        edges = connection.get('edges', [])
        for edge in edges:
            self._upsert_customer(instance, edge['node'])
        page_info = connection.get('pageInfo', {})
        return len(edges), page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_customer(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_customer_id', '=', numeric_id),
        ], limit=1)

        first_name = node.get('firstName', '') or ''
        last_name = node.get('lastName', '') or ''
        full_name = f'{first_name} {last_name}'.strip() or node.get('email', numeric_id)

        marketing_state = (node.get('emailMarketingConsent') or {}).get('marketingState', '')
        accepts_marketing = marketing_state in ('SUBSCRIBED', 'PENDING')

        amount_spent = node.get('amountSpent') or {}

        vals = {
            'name': full_name,
            'shopify_customer_id': numeric_id,
            'shopify_customer_gid': gid,
            'instance_id': instance.id,
            'email': node.get('email', ''),
            'phone': node.get('phone', ''),
            'first_name': first_name,
            'last_name': last_name,
            'orders_count': node.get('numberOfOrders', 0),
            'total_spent': float(amount_spent.get('amount', 0)),
            'tags': ', '.join(node.get('tags', [])) if isinstance(node.get('tags'), list) else node.get('tags', ''),
            'accepts_marketing': accepts_marketing,
            'verified_email': node.get('verifiedEmail', False),
            'tax_exempt': node.get('taxExempt', False),
            'sync_status': 'synced',
            'last_synced': fields.Datetime.now(),
        }

        # B2B detection: company name in default address
        default_address = node.get('defaultAddress') or {}
        company_name = default_address.get('company', '') or ''
        if company_name:
            vals['is_company'] = True
            vals['company_name'] = company_name

        if existing:
            existing.write(vals)
            rec = existing
        else:
            rec = self.create(vals)

        rec._match_or_create_odoo_partner(node.get('defaultAddress') or {})
        return rec

    def _match_or_create_odoo_partner(self, address):
        if self.odoo_partner_id:
            return self.odoo_partner_id

        partner = False
        if self.email:
            partner = self.env['res.partner'].search([('email', '=', self.email)], limit=1)
        if not partner and self.phone:
            partner = self.env['res.partner'].search([('phone', '=', self.phone)], limit=1)

        if not partner:
            if self.is_company and self.company_name:
                # B2B: create company then contact under it
                company = self.env['res.partner'].search(
                    [('name', '=ilike', self.company_name), ('is_company', '=', True)], limit=1
                )
                if not company:
                    company = self.env['res.partner'].create({
                        'name': self.company_name,
                        'is_company': True,
                        'country_id': self._get_country_id(address.get('countryCodeV2', '')),
                    })
                partner = self.env['res.partner'].create({
                    'name': self.name,
                    'email': self.email or False,
                    'phone': self.phone or False,
                    'parent_id': company.id,
                    'type': 'contact',
                })
            else:
                partner = self.env['res.partner'].create({
                    'name': self.name,
                    'email': self.email or False,
                    'phone': self.phone or False,
                    'street': address.get('address1', ''),
                    'street2': address.get('address2', ''),
                    'city': address.get('city', ''),
                    'zip': address.get('zip', ''),
                    'country_id': self._get_country_id(address.get('countryCodeV2', '')),
                })

        self.write({'odoo_partner_id': partner.id})
        return partner

    def _get_country_id(self, code):
        if not code:
            return False
        country = self.env['res.country'].search([('code', '=', code.upper())], limit=1)
        return country.id if country else False

    # ------------------------------------------------------------------
    # Export: Odoo → Shopify  (GraphQL customerCreate / customerUpdate)
    # ------------------------------------------------------------------

    def _build_customer_input(self):
        self.ensure_one()
        partner = self.odoo_partner_id
        if not partner:
            raise UserError(_('No Odoo contact linked to customer %s') % self.name)

        name_parts = (partner.name or '').split(' ', 1)
        customer_input = {
            'firstName': name_parts[0],
            'lastName': name_parts[1] if len(name_parts) > 1 else '',
            'email': partner.email or '',
            'phone': partner.phone or '',
            'addresses': [{
                'address1': partner.street or '',
                'address2': partner.street2 or '',
                'city': partner.city or '',
                'zip': partner.zip or '',
                'countryCode': partner.country_id.code if partner.country_id else '',
            }],
        }
        if self.shopify_customer_gid:
            customer_input['id'] = self.shopify_customer_gid
        return customer_input

    def merge_into_on_shopify(self, keeper):
        """Merge this customer (duplicate) into `keeper` on Shopify via
        customerMerge, then drop the local duplicate mapping."""
        self.ensure_one()
        if not self.shopify_customer_gid or not keeper.shopify_customer_gid:
            raise UserError(_('Both customers must exist on Shopify to merge.'))
        if self == keeper:
            raise UserError(_('Pick two different customers.'))
        mutation = '''
            mutation customerMerge($customerOneId: ID!, $customerTwoId: ID!) {
                customerMerge(customerOneId: $customerOneId, customerTwoId: $customerTwoId) {
                    job { id done }
                    userErrors { field message }
                }
            }
        '''
        data = self.instance_id._graphql_request(mutation, variables={
            'customerOneId': keeper.shopify_customer_gid,
            'customerTwoId': self.shopify_customer_gid,
        })
        errors = (data.get('customerMerge') or {}).get('userErrors', [])
        if errors:
            raise UserError(_('Merge failed: %s') % errors[0].get('message'))
        self.unlink()
        return keeper

    def action_export_to_shopify(self):
        for rec in self:
            if not rec.odoo_partner_id:
                continue
            try:
                customer_input = rec._build_customer_input()
                if rec.shopify_customer_gid:
                    data = rec.instance_id._graphql_request(
                        MUTATION_CUSTOMER_UPDATE, variables={'input': customer_input}
                    )
                    result_key = 'customerUpdate'
                else:
                    data = rec.instance_id._graphql_request(
                        MUTATION_CUSTOMER_CREATE, variables={'input': customer_input}
                    )
                    result_key = 'customerCreate'

                result = data.get(result_key, {})
                user_errors = result.get('userErrors', [])
                if user_errors:
                    raise UserError(_('Shopify error: %s') % user_errors[0]['message'])

                if result_key == 'customerCreate':
                    gid = result.get('customer', {}).get('id', '')
                    rec.write({
                        'shopify_customer_gid': gid,
                        'shopify_customer_id': rec.instance_id._gid_to_id(gid),
                    })

                rec.write({'sync_status': 'synced', 'last_synced': fields.Datetime.now()})
            except UserError as e:
                rec.write({'sync_status': 'error'})
                _logger.error('Customer export failed for %s: %s', rec.name, e)

    # ------------------------------------------------------------------
    # Metafields
    # ------------------------------------------------------------------

    def action_fetch_metafields(self):
        """Fetch all metafields for this customer from Shopify."""
        from .shopify_metafield import QUERY_CUSTOMER_METAFIELDS
        for rec in self:
            if not rec.shopify_customer_gid:
                continue
            cursor = None
            while True:
                variables = {'customerId': rec.shopify_customer_gid, 'first': 50, 'after': cursor}
                data = rec.instance_id._graphql_request(QUERY_CUSTOMER_METAFIELDS, variables=variables)
                connection = (data.get('customer') or {}).get('metafields', {})
                edges = connection.get('edges', [])
                self.env['shopify.metafield']._upsert_from_nodes(
                    edges, rec.instance_id, shopify_customer_id=rec.id
                )
                page_info = connection.get('pageInfo', {})
                if not page_info.get('hasNextPage'):
                    break
                cursor = page_info.get('endCursor')

    def action_push_metafields(self):
        """Push all locally-modified customer metafields to Shopify."""
        for rec in self:
            modified = rec.metafield_ids.filtered('is_modified')
            if modified:
                modified.action_push_to_shopify()

    # ------------------------------------------------------------------
    # Store Credit
    # ------------------------------------------------------------------

    def action_credit_store_credit(self):
        """Open a wizard-less quick credit — credits a fixed prompt amount."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Add Store Credit'),
            'res_model': 'shopify.store.credit.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_customer_id': self.id},
        }

    def _adjust_store_credit(self, amount, credit=True):
        """Credit (credit=True) or debit (credit=False) the customer's store credit."""
        self.ensure_one()
        if not self.shopify_customer_gid:
            raise UserError(_('Customer not synced to Shopify.'))

        mutation_credit = '''
            mutation storeCreditAccountCredit($id: ID!, $creditInput: StoreCreditAccountCreditInput!) {
                storeCreditAccountCredit(id: $id, creditInput: $creditInput) {
                    storeCreditAccountTransaction {
                        account { id balance { amount currencyCode } }
                    }
                    userErrors { field message }
                }
            }
        '''
        mutation_debit = '''
            mutation storeCreditAccountDebit($id: ID!, $debitInput: StoreCreditAccountDebitInput!) {
                storeCreditAccountDebit(id: $id, debitInput: $debitInput) {
                    storeCreditAccountTransaction {
                        account { id balance { amount currencyCode } }
                    }
                    userErrors { field message }
                }
            }
        '''
        currency = self.total_spent and self.instance_id.currency_id.name or 'USD'
        amount_input = {'amount': {'amount': str(amount), 'currencyCode': self.instance_id.currency_id.name or 'USD'}}

        if credit:
            data = self.instance_id._graphql_request(
                mutation_credit,
                variables={'id': self.shopify_customer_gid, 'creditInput': {'creditAmount': amount_input['amount']}},
            )
            result = data.get('storeCreditAccountCredit', {})
        else:
            data = self.instance_id._graphql_request(
                mutation_debit,
                variables={'id': self.shopify_customer_gid, 'debitInput': {'debitAmount': amount_input['amount']}},
            )
            result = data.get('storeCreditAccountDebit', {})

        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Store credit error: %s') % errors[0]['message'])

        account = result.get('storeCreditAccountTransaction', {}).get('account', {})
        balance = account.get('balance', {})
        self.write({
            'store_credit_account_gid': account.get('id', self.store_credit_account_gid),
            'store_credit_balance': float(balance.get('amount', self.store_credit_balance)),
        })
        self.message_post(body=_('Store credit %s: %s %s') % (
            _('credited') if credit else _('debited'), amount, balance.get('currencyCode', '')
        ))

    @api.model
    def cron_import_customers(self):
        instances = self.env['shopify.instance'].search([('state', '=', 'connected'), ('sync_customers', '=', True)])
        for instance in instances:
            try:
                self.import_customers_from_shopify(instance)
            except Exception as e:
                _logger.error('Customer import cron failed for %s: %s', instance.name, e)
