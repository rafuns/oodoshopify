import base64
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# FILES / MEDIA LIBRARY
# ═══════════════════════════════════════════════════════════════════════════

MUTATION_FILE_CREATE = '''
    mutation fileCreate($files: [FileCreateInput!]!) {
        fileCreate(files: $files) {
            files { id alt fileStatus ... on GenericFile { url } ... on MediaImage { image { url } } }
            userErrors { field message }
        }
    }
'''

QUERY_FILES = '''
    query GetFiles($first: Int!, $after: String) {
        files(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id alt fileStatus createdAt
                    ... on GenericFile { url }
                    ... on MediaImage { image { url } }
                }
            }
        }
    }
'''


class ShopifyFile(models.Model):
    _name = 'shopify.file'
    _description = 'Shopify File / Media'
    _order = 'create_date desc'

    name = fields.Char(string='Name', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    shopify_file_id = fields.Char(string='File ID', readonly=True)
    shopify_file_gid = fields.Char(string='File GID', readonly=True)
    alt_text = fields.Char(string='Alt Text')
    file_url = fields.Char(string='URL', readonly=True)
    source_url = fields.Char(string='Source URL',
                             help='Public URL Shopify will fetch the file from on upload.')
    file_status = fields.Char(string='Status', readonly=True)

    _unique_file = models.Constraint(
        'UNIQUE(instance_id, shopify_file_id)',
        'File already exists.',
    )

    def action_upload(self):
        self.ensure_one()
        if not self.source_url:
            raise UserError(_('Provide a Source URL for Shopify to fetch.'))
        file_input = {
            'originalSource': self.source_url,
            'alt': self.alt_text or '',
            'contentType': 'IMAGE',
        }
        data = self.instance_id._graphql_request(
            MUTATION_FILE_CREATE, variables={'files': [file_input]}
        )
        result = data.get('fileCreate', {})
        if result.get('userErrors'):
            raise UserError(_('File upload error: %s') % result['userErrors'][0]['message'])
        files = result.get('files', [])
        if files:
            node = files[0]
            gid = node.get('id', '')
            self.write({
                'shopify_file_gid': gid,
                'shopify_file_id': self.instance_id._gid_to_id(gid),
                'file_status': node.get('fileStatus', ''),
                'file_url': node.get('url') or (node.get('image') or {}).get('url', ''),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_FILES, variables={'first': 50, 'after': cursor})
            conn = data.get('files', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_file_id', '=', numeric)
                ], limit=1)
                vals = {
                    'name': node.get('alt') or f'File {numeric}',
                    'instance_id': instance.id,
                    'shopify_file_id': numeric, 'shopify_file_gid': node['id'],
                    'alt_text': node.get('alt', ''),
                    'file_status': node.get('fileStatus', ''),
                    'file_url': node.get('url') or (node.get('image') or {}).get('url', ''),
                }
                (existing.write(vals) if existing else self.create(vals))
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count


# ═══════════════════════════════════════════════════════════════════════════
# TRANSLATIONS (Translate & Adapt)
# ═══════════════════════════════════════════════════════════════════════════

MUTATION_TRANSLATIONS_REGISTER = '''
    mutation translationsRegister($resourceId: ID!, $translations: [TranslationInput!]!) {
        translationsRegister(resourceId: $resourceId, translations: $translations) {
            translations { key locale value }
            userErrors { field message }
        }
    }
'''


class ShopifyTranslation(models.Model):
    _name = 'shopify.translation'
    _description = 'Shopify Translation'
    _order = 'resource_type, locale'

    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    resource_type = fields.Selection([
        ('PRODUCT',    'Product'),
        ('COLLECTION', 'Collection'),
        ('PAGE',       'Page'),
        ('ARTICLE',    'Article'),
        ('BLOG',       'Blog'),
    ], string='Resource Type', required=True, default='PRODUCT')
    resource_gid = fields.Char(string='Resource GID', required=True,
                               help='The Shopify GID of the object to translate.')
    locale = fields.Char(string='Locale', required=True, help='e.g. fr, de, es')
    key = fields.Char(string='Field Key', required=True, default='title',
                      help='e.g. title, body_html, meta_title')
    value = fields.Text(string='Translated Value', required=True)
    translatable_content_digest = fields.Char(
        string='Content Digest',
        help='Digest of the source content (required by Shopify for registration).',
    )

    def action_push_translation(self):
        for rec in self:
            translation = {
                'locale': rec.locale,
                'key': rec.key,
                'value': rec.value,
            }
            if rec.translatable_content_digest:
                translation['translatableContentDigest'] = rec.translatable_content_digest
            data = rec.instance_id._graphql_request(
                MUTATION_TRANSLATIONS_REGISTER,
                variables={'resourceId': rec.resource_gid, 'translations': [translation]},
            )
            result = data.get('translationsRegister', {})
            if result.get('userErrors'):
                raise UserError(_('Translation error: %s') % result['userErrors'][0]['message'])


# ═══════════════════════════════════════════════════════════════════════════
# CUSTOMER SEGMENTS
# ═══════════════════════════════════════════════════════════════════════════

QUERY_SEGMENTS = '''
    query GetSegments($first: Int!, $after: String) {
        segments(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node { id name query creationDate } }
        }
    }
'''

MUTATION_SEGMENT_CREATE = '''
    mutation segmentCreate($name: String!, $query: String!) {
        segmentCreate(name: $name, query: $query) {
            segment { id name query }
            userErrors { field message }
        }
    }
'''


class ShopifySegment(models.Model):
    _name = 'shopify.segment'
    _description = 'Shopify Customer Segment'
    _order = 'name'

    name = fields.Char(string='Segment Name', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    shopify_segment_id = fields.Char(string='Segment ID', readonly=True)
    shopify_segment_gid = fields.Char(string='Segment GID', readonly=True)
    query = fields.Char(string='Segment Query',
                        help='Shopify segment query, e.g. "number_of_orders > 5"')

    _unique_segment = models.Constraint(
        'UNIQUE(instance_id, shopify_segment_id)',
        'Segment already exists.',
    )

    def action_push_to_shopify(self):
        for rec in self:
            if rec.shopify_segment_gid:
                continue
            data = rec.instance_id._graphql_request(
                MUTATION_SEGMENT_CREATE, variables={'name': rec.name, 'query': rec.query or 'number_of_orders > 0'}
            )
            result = data.get('segmentCreate', {})
            if result.get('userErrors'):
                raise UserError(_('Segment error: %s') % result['userErrors'][0]['message'])
            node = result.get('segment', {})
            gid = node.get('id', '')
            rec.write({
                'shopify_segment_gid': gid,
                'shopify_segment_id': rec.instance_id._gid_to_id(gid),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_SEGMENTS, variables={'first': 50, 'after': cursor})
            conn = data.get('segments', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_segment_id', '=', numeric)
                ], limit=1)
                vals = {
                    'name': node.get('name', ''), 'instance_id': instance.id,
                    'shopify_segment_id': numeric, 'shopify_segment_gid': node['id'],
                    'query': node.get('query', ''),
                }
                (existing.write(vals) if existing else self.create(vals))
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count


# ═══════════════════════════════════════════════════════════════════════════
# B2B COMPANIES (Shopify Plus)
# ═══════════════════════════════════════════════════════════════════════════

QUERY_COMPANIES = '''
    query GetCompanies($first: Int!, $after: String) {
        companies(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id name externalId createdAt
                    totalSpent { amount currencyCode }
                    locationsCount { count }
                    contactCount
                }
            }
        }
    }
'''

MUTATION_COMPANY_CREATE = '''
    mutation companyCreate($input: CompanyCreateInput!) {
        companyCreate(input: $input) {
            company { id name }
            userErrors { field message }
        }
    }
'''


class ShopifyCompany(models.Model):
    _name = 'shopify.company'
    _description = 'Shopify B2B Company'
    _order = 'name'

    name = fields.Char(string='Company Name', required=True)
    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade')
    shopify_company_id = fields.Char(string='Company ID', readonly=True)
    shopify_company_gid = fields.Char(string='Company GID', readonly=True)
    external_id = fields.Char(string='External ID')
    total_spent = fields.Float(string='Total Spent', readonly=True)
    currency = fields.Char(string='Currency', readonly=True)
    locations_count = fields.Integer(string='Locations', readonly=True)
    contact_count = fields.Integer(string='Contacts', readonly=True)
    odoo_partner_id = fields.Many2one('res.partner', string='Odoo Company',
                                      domain="[('is_company', '=', True)]")

    _unique_company = models.Constraint(
        'UNIQUE(instance_id, shopify_company_id)',
        'Company already exists.',
    )

    def action_push_to_shopify(self):
        for rec in self:
            if rec.shopify_company_gid:
                continue
            company_input = {'company': {'name': rec.name}}
            if rec.external_id:
                company_input['company']['externalId'] = rec.external_id
            data = rec.instance_id._graphql_request(
                MUTATION_COMPANY_CREATE, variables={'input': company_input}
            )
            result = data.get('companyCreate', {})
            if result.get('userErrors'):
                raise UserError(_('Company error: %s') % result['userErrors'][0]['message'])
            node = result.get('company', {})
            gid = node.get('id', '')
            rec.write({
                'shopify_company_gid': gid,
                'shopify_company_id': rec.instance_id._gid_to_id(gid),
            })

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        count = 0
        while True:
            data = instance._graphql_request(QUERY_COMPANIES, variables={'first': 50, 'after': cursor})
            conn = data.get('companies', {})
            for edge in conn.get('edges', []):
                node = edge['node']
                numeric = instance._gid_to_id(node['id'])
                existing = self.search([
                    ('instance_id', '=', instance.id), ('shopify_company_id', '=', numeric)
                ], limit=1)
                spent = node.get('totalSpent') or {}
                vals = {
                    'name': node.get('name', ''), 'instance_id': instance.id,
                    'shopify_company_id': numeric, 'shopify_company_gid': node['id'],
                    'external_id': node.get('externalId', ''),
                    'total_spent': float(spent.get('amount', 0)),
                    'currency': spent.get('currencyCode', ''),
                    'locations_count': (node.get('locationsCount') or {}).get('count', 0),
                    'contact_count': node.get('contactCount', 0),
                }
                company = existing or self.create(vals)
                if existing:
                    existing.write(vals)
                # Auto-link / create Odoo company partner
                if not company.odoo_partner_id:
                    partner = self.env['res.partner'].search(
                        [('name', '=ilike', company.name), ('is_company', '=', True)], limit=1
                    )
                    if not partner:
                        partner = self.env['res.partner'].create({
                            'name': company.name, 'is_company': True,
                        })
                    company.odoo_partner_id = partner.id
                count += 1
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')
        return count
