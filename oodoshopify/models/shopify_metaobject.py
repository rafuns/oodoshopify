import json
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

QUERY_METAOBJECT_DEFS = '''
    query defs($first: Int!, $after: String) {
        metaobjectDefinitions(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node { type name } }
        }
    }
'''

QUERY_METAOBJECTS = '''
    query metaobjects($type: String!, $first: Int!, $after: String) {
        metaobjects(type: $type, first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges { node { id handle displayName type fields { key value } } }
        }
    }
'''

MUTATION_METAOBJECT_UPSERT = '''
    mutation upsert($handle: MetaobjectHandleInput!, $metaobject: MetaobjectUpsertInput!) {
        metaobjectUpsert(handle: $handle, metaobject: $metaobject) {
            metaobject { id handle }
            userErrors { field message }
        }
    }
'''


class ShopifyMetaobject(models.Model):
    """Shopify metaobjects — custom structured content types (size charts, specs,
    custom records). Imported per definition; editable JSON pushed back via upsert."""
    _name = 'shopify.metaobject'
    _description = 'Shopify Metaobject'
    _order = 'object_type, handle'

    instance_id = fields.Many2one('shopify.instance', required=True, ondelete='cascade', index=True)
    shopify_gid = fields.Char(string='GID', readonly=True)
    object_type = fields.Char(string='Type', required=True, index=True)
    handle = fields.Char(string='Handle')
    display_name = fields.Char(string='Display Name')
    fields_json = fields.Text(
        string='Fields (JSON)',
        help='List of {"key","value"} objects. Edit then Push to update on Shopify.')

    _unique_metaobject = models.Constraint(
        'UNIQUE(instance_id, shopify_gid)',
        'This metaobject already exists for the instance.',
    )

    @api.model
    def import_from_shopify(self, instance):
        """Import all metaobjects for every definition type on the store."""
        types, cursor = [], None
        while True:
            data = instance._graphql_request(QUERY_METAOBJECT_DEFS,
                                             variables={'first': 50, 'after': cursor})
            conn = data.get('metaobjectDefinitions', {})
            types += [e['node']['type'] for e in conn.get('edges', [])]
            page = conn.get('pageInfo', {})
            if not page.get('hasNextPage'):
                break
            cursor = page.get('endCursor')

        imported = 0
        for otype in types:
            cursor = None
            while True:
                data = instance._graphql_request(
                    QUERY_METAOBJECTS, variables={'type': otype, 'first': 50, 'after': cursor})
                conn = data.get('metaobjects', {})
                for edge in conn.get('edges', []):
                    self._upsert(instance, edge['node'])
                    imported += 1
                page = conn.get('pageInfo', {})
                if not page.get('hasNextPage'):
                    break
                cursor = page.get('endCursor')
        _logger.info('Imported %d metaobjects from %s', imported, instance.name)
        return imported

    def _upsert(self, instance, node):
        existing = self.search([
            ('instance_id', '=', instance.id), ('shopify_gid', '=', node['id'])], limit=1)
        vals = {
            'instance_id': instance.id,
            'shopify_gid': node['id'],
            'object_type': node.get('type', ''),
            'handle': node.get('handle', ''),
            'display_name': node.get('displayName', ''),
            'fields_json': json.dumps(node.get('fields', []), indent=2),
        }
        if existing:
            existing.write(vals)
        else:
            self.create(vals)

    def action_push_to_shopify(self):
        for rec in self:
            try:
                fields_list = json.loads(rec.fields_json or '[]')
            except Exception:
                raise UserError(_('Fields JSON is invalid for %s.') % rec.display_name)
            data = rec.instance_id._graphql_request(
                MUTATION_METAOBJECT_UPSERT,
                variables={
                    'handle': {'type': rec.object_type, 'handle': rec.handle},
                    'metaobject': {'fields': fields_list},
                })
            errors = (data.get('metaobjectUpsert') or {}).get('userErrors', [])
            if errors:
                raise UserError(_('Metaobject push failed: %s') % errors[0].get('message'))
            node = (data.get('metaobjectUpsert') or {}).get('metaobject') or {}
            if node.get('id'):
                rec.shopify_gid = node['id']
        return True

    @api.model
    def cron_import_metaobjects(self):
        for instance in self.env['shopify.instance'].search([('state', '=', 'connected')]):
            try:
                self.import_from_shopify(instance)
            except Exception as e:
                _logger.error('Metaobject import failed for %s: %s', instance.name, e)
