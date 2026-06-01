import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

QUERY_PRODUCT_METAFIELDS = '''
    query GetProductMetafields($productId: ID!, $first: Int!, $after: String) {
        product(id: $productId) {
            metafields(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        namespace
                        key
                        value
                        type
                        updatedAt
                    }
                }
            }
        }
    }
'''

QUERY_CUSTOMER_METAFIELDS = '''
    query GetCustomerMetafields($customerId: ID!, $first: Int!, $after: String) {
        customer(id: $customerId) {
            metafields(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        namespace
                        key
                        value
                        type
                        updatedAt
                    }
                }
            }
        }
    }
'''

QUERY_ORDER_METAFIELDS = '''
    query GetOrderMetafields($orderId: ID!, $first: Int!, $after: String) {
        order(id: $orderId) {
            metafields(first: $first, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                    node {
                        id
                        namespace
                        key
                        value
                        type
                        updatedAt
                    }
                }
            }
        }
    }
'''

MUTATION_METAFIELDS_SET = '''
    mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields {
                id
                namespace
                key
                value
                type
                ownerType
            }
            userErrors { field message code }
        }
    }
'''

MUTATION_METAFIELD_DELETE = '''
    mutation metafieldDelete($input: MetafieldDeleteInput!) {
        metafieldDelete(input: $input) {
            deletedId
            userErrors { field message }
        }
    }
'''

# Shopify value type → Python cast
_TYPE_CAST = {
    'number_integer':       int,
    'number_decimal':       float,
    'boolean':              lambda v: v.lower() == 'true',
    'single_line_text_field': str,
    'multi_line_text_field':  str,
    'json':                 str,
    'color':                str,
    'url':                  str,
    'date':                 str,
    'date_time':            str,
}


class ShopifyMetafield(models.Model):
    """Stores a single Shopify metafield for any resource."""
    _name = 'shopify.metafield'
    _description = 'Shopify Metafield'
    _order = 'namespace, key'

    # Resource links (only one is set per record)
    shopify_product_id  = fields.Many2one('shopify.product',  string='Product',  ondelete='cascade', index=True)
    shopify_customer_id = fields.Many2one('shopify.customer', string='Customer', ondelete='cascade', index=True)
    shopify_order_id    = fields.Many2one('shopify.order',    string='Order',    ondelete='cascade', index=True)

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', compute='_compute_instance_id', store=True,
    )

    # Shopify identity
    shopify_metafield_id  = fields.Char(string='Metafield ID (numeric)', readonly=True)
    shopify_metafield_gid = fields.Char(string='Metafield GID', readonly=True)

    # Content
    namespace  = fields.Char(string='Namespace', required=True)
    key        = fields.Char(string='Key',       required=True)
    value      = fields.Text(string='Value')
    value_type = fields.Char(string='Type', readonly=True,
                              help='Shopify value type, e.g. single_line_text_field, number_integer')

    updated_at = fields.Datetime(string='Updated at Shopify', readonly=True)
    is_modified = fields.Boolean(
        string='Modified Locally',
        help='Set when you edit the value locally. Cleared after pushing to Shopify.',
    )

    # Mapping result (populated when a mapping auto-syncs to an Odoo field)
    mapped_odoo_field = fields.Char(string='Synced to Odoo Field', readonly=True)

    @api.depends('shopify_product_id', 'shopify_customer_id', 'shopify_order_id')
    def _compute_instance_id(self):
        for rec in self:
            if rec.shopify_product_id:
                rec.instance_id = rec.shopify_product_id.instance_id
            elif rec.shopify_customer_id:
                rec.instance_id = rec.shopify_customer_id.instance_id
            elif rec.shopify_order_id:
                rec.instance_id = rec.shopify_order_id.instance_id
            else:
                rec.instance_id = False

    # ------------------------------------------------------------------
    # Generic upsert helper (called from product/customer/order models)
    # ------------------------------------------------------------------

    @api.model
    def _upsert_from_nodes(self, edges, instance, **resource_link):
        """
        Upsert metafield records from a list of GraphQL edges.
        resource_link: e.g. shopify_product_id=product.id
        """
        for edge in edges:
            node = edge['node']
            gid = node['id']
            numeric_id = instance._gid_to_id(gid)

            domain = [('shopify_metafield_id', '=', numeric_id)]
            for field, val in resource_link.items():
                domain.append((field, '=', val))

            existing = self.search(domain, limit=1)
            updated_raw = (node.get('updatedAt') or '').replace('T', ' ').replace('Z', '')

            vals = {
                **resource_link,
                'shopify_metafield_id': numeric_id,
                'shopify_metafield_gid': gid,
                'namespace': node.get('namespace', ''),
                'key': node.get('key', ''),
                'value': str(node.get('value', '') or ''),
                'value_type': node.get('type', ''),
                'updated_at': updated_raw or False,
                'is_modified': False,
            }

            if existing:
                existing.write(vals)
                mf = existing
            else:
                mf = self.create(vals)

            # Auto-apply mapping if one exists
            mapping = self.env['shopify.metafield.mapping'].search([
                ('instance_id', '=', instance.id),
                ('namespace', '=', mf.namespace),
                ('key', '=', mf.key),
                ('direction', 'in', ('import', 'both')),
            ], limit=1)
            if mapping:
                mf._apply_mapping_to_odoo(mapping)

    # ------------------------------------------------------------------
    # Apply mapping: write metafield value → Odoo field
    # ------------------------------------------------------------------

    def _apply_mapping_to_odoo(self, mapping):
        """Write the metafield value to the mapped Odoo field on the parent record."""
        self.ensure_one()
        if not mapping.odoo_field_id:
            return

        # Identify the parent Odoo record
        if self.shopify_product_id and self.shopify_product_id.odoo_product_id:
            record = self.shopify_product_id.odoo_product_id
        elif self.shopify_customer_id and self.shopify_customer_id.odoo_partner_id:
            record = self.shopify_customer_id.odoo_partner_id
        elif self.shopify_order_id and self.shopify_order_id.odoo_sale_order_id:
            record = self.shopify_order_id.odoo_sale_order_id
        else:
            return

        field_name = mapping.odoo_field_id.name
        if not hasattr(record, field_name):
            return

        cast = _TYPE_CAST.get(self.value_type, str)
        try:
            cast_value = cast(self.value) if self.value else False
            record.write({field_name: cast_value})
            self.mapped_odoo_field = f'{record._name}.{field_name}'
            _logger.info('Metafield mapped: %s.%s → %s=%s', self.namespace, self.key, field_name, cast_value)
        except Exception as e:
            _logger.warning('Metafield mapping failed for %s.%s: %s', self.namespace, self.key, e)

    # ------------------------------------------------------------------
    # Push modified metafields to Shopify
    # ------------------------------------------------------------------

    def action_push_to_shopify(self):
        """Push this metafield (or selection) back to Shopify."""
        records_by_instance = {}
        for rec in self.filtered('is_modified'):
            key = (rec.instance_id.id, rec._get_owner_gid())
            records_by_instance.setdefault(key, []).append(rec)

        for (instance_id, owner_gid), recs in records_by_instance.items():
            if not owner_gid:
                continue
            instance = self.env['shopify.instance'].browse(instance_id)
            metafields_input = []
            for rec in recs:
                if not rec.value_type:
                    continue
                metafields_input.append({
                    'ownerId':   owner_gid,
                    'namespace': rec.namespace,
                    'key':       rec.key,
                    'value':     str(rec.value or ''),
                    'type':      rec.value_type,
                })
            if not metafields_input:
                continue

            data = instance._graphql_request(
                MUTATION_METAFIELDS_SET,
                variables={'metafields': metafields_input},
            )
            result = data.get('metafieldsSet', {})
            user_errors = result.get('userErrors', [])
            if user_errors:
                raise UserError(_('Metafield push error: %s') % user_errors[0]['message'])

            # Update GIDs from response + clear modified flag
            for pushed in result.get('metafields', []):
                gid = pushed['id']
                numeric = instance._gid_to_id(gid)
                match = recs.filtered(
                    lambda r, ns=pushed['namespace'], k=pushed['key']: r.namespace == ns and r.key == k
                )
                if match:
                    match[0].write({
                        'shopify_metafield_gid': gid,
                        'shopify_metafield_id': numeric,
                        'is_modified': False,
                    })

    def _get_owner_gid(self):
        self.ensure_one()
        if self.shopify_product_id:
            return self.shopify_product_id.shopify_gid
        if self.shopify_customer_id:
            return self.shopify_customer_id.shopify_customer_gid
        if self.shopify_order_id:
            return self.shopify_order_id.shopify_order_gid
        return False

    def action_delete_from_shopify(self):
        """Delete this metafield from Shopify."""
        self.ensure_one()
        if not self.shopify_metafield_gid:
            self.unlink()
            return
        self.instance_id._graphql_request(
            MUTATION_METAFIELD_DELETE,
            variables={'input': {'id': self.shopify_metafield_gid}},
        )
        self.unlink()

    @api.onchange('value')
    def _onchange_value(self):
        if self.shopify_metafield_id:
            self.is_modified = True


class ShopifyMetafieldMapping(models.Model):
    """
    Defines how a Shopify metafield (namespace + key) maps to an Odoo field.

    When direction = 'import' or 'both': on metafield import, the value is
    written to the mapped Odoo field on the linked record.

    When direction = 'export' or 'both': on push, the Odoo field value is
    read and pushed to Shopify as a metafield.
    """
    _name = 'shopify.metafield.mapping'
    _description = 'Shopify Metafield → Odoo Field Mapping'
    _order = 'resource_type, namespace, key'

    instance_id = fields.Many2one(
        'shopify.instance', string='Instance',
        required=True, ondelete='cascade',
    )
    resource_type = fields.Selection([
        ('product',  'Product (product.template)'),
        ('variant',  'Variant (product.product)'),
        ('customer', 'Customer (res.partner)'),
        ('order',    'Order (sale.order)'),
    ], string='Resource', required=True, default='product')

    namespace = fields.Char(string='Namespace', required=True,
                             help='e.g. "custom", "my_app", "global"')
    key       = fields.Char(string='Key',       required=True,
                             help='e.g. "material", "care_instructions"')
    value_type = fields.Selection([
        ('single_line_text_field', 'Single-line text'),
        ('multi_line_text_field',  'Multi-line text'),
        ('number_integer',         'Integer'),
        ('number_decimal',         'Decimal'),
        ('boolean',                'Boolean'),
        ('date',                   'Date'),
        ('date_time',              'Date & Time'),
        ('url',                    'URL'),
        ('color',                  'Color'),
        ('json',                   'JSON'),
    ], string='Value Type', default='single_line_text_field',
       help='Shopify type used when creating/pushing this metafield.')

    odoo_model_id = fields.Many2one(
        'ir.model', string='Odoo Model',
        help='Leave empty to skip Odoo field sync.',
        ondelete='set null',
    )
    odoo_field_id = fields.Many2one(
        'ir.model.fields', string='Odoo Field',
        domain="[('model_id', '=', odoo_model_id), ('ttype', 'in', ['char','text','integer','float','boolean','date','datetime'])]",
        ondelete='set null',
    )
    direction = fields.Selection([
        ('import', 'Shopify → Odoo only'),
        ('export', 'Odoo → Shopify only'),
        ('both',   'Both directions'),
    ], string='Sync Direction', default='both', required=True)

    transform = fields.Selection([
        ('none', 'None'),
        ('upper', 'UPPERCASE'),
        ('lower', 'lowercase'),
        ('title', 'Title Case'),
        ('strip', 'Trim spaces'),
    ], string='Transform', default='none',
       help='Optional transform applied to the value when exporting Odoo → Shopify.')

    note = fields.Char(string='Notes')

    _unique_mapping = models.Constraint(
        'UNIQUE(instance_id, resource_type, namespace, key)',
        'A mapping for this instance / resource / namespace / key already exists.',
    )

    def _transform_value(self, value):
        self.ensure_one()
        s = str(value if value not in (None, False) else '')
        return {
            'upper': s.upper, 'lower': s.lower, 'title': s.title, 'strip': s.strip,
        }.get(self.transform, lambda: s)()

    @api.model
    def generate_metafields(self, shopify_records):
        """Build/refresh shopify.metafield rows from export mappings for the given
        shopify.product / shopify.customer records, ready to push. Returns count."""
        Metafield = self.env['shopify.metafield']
        created = 0
        for rec in shopify_records:
            res_type = 'product' if rec._name == 'shopify.product' else (
                'customer' if rec._name == 'shopify.customer' else None)
            if not res_type:
                continue
            odoo_record = rec.odoo_product_id if res_type == 'product' else rec.odoo_partner_id
            if not odoo_record:
                continue
            mappings = self.search([
                ('instance_id', '=', rec.instance_id.id),
                ('resource_type', '=', res_type),
                ('direction', 'in', ('export', 'both')),
                ('odoo_field_id', '!=', False),
            ])
            owner_field = 'shopify_product_id' if res_type == 'product' else 'shopify_customer_id'
            for mapping in mappings:
                fname = mapping.odoo_field_id.name
                if not hasattr(odoo_record, fname):
                    continue
                value = mapping._transform_value(getattr(odoo_record, fname))
                existing = Metafield.search([
                    ('instance_id', '=', rec.instance_id.id),
                    (owner_field, '=', rec.id),
                    ('namespace', '=', mapping.namespace),
                    ('key', '=', mapping.key),
                ], limit=1)
                vals = {
                    'instance_id': rec.instance_id.id,
                    owner_field: rec.id,
                    'namespace': mapping.namespace,
                    'key': mapping.key,
                    'value': value,
                    'value_type': mapping.value_type,
                    'is_modified': True,
                }
                if existing:
                    existing.write(vals)
                else:
                    Metafield.create(vals)
                created += 1
        return created
