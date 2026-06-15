import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL fragments and queries
# ---------------------------------------------------------------------------

_PRODUCT_FIELDS = '''
    id
    title
    descriptionHtml
    vendor
    productType
    status
    tags
    seo { title description }
    variants(first: 100) {
        edges {
            node {
                id
                title
                price
                compareAtPrice
                sku
                inventoryQuantity
                inventoryItem {
                    id
                    countryCodeOfOrigin
                    harmonizedSystemCode
                }
            }
        }
    }
    images(first: 20) {
        edges { node { id url altText } }
    }
'''

QUERY_PRODUCTS = f'''
    query GetProducts($first: Int!, $after: String) {{
        products(first: $first, after: $after) {{
            pageInfo {{ hasNextPage endCursor }}
            edges {{
                node {{ {_PRODUCT_FIELDS} }}
            }}
        }}
    }}
'''

MUTATION_PRODUCT_CREATE = '''
    mutation productCreate($input: ProductInput!) {
        productCreate(input: $input) {
            product {
                id
                tags
                status
                seo { title description }
                variants(first: 100) {
                    edges { node { id title sku price } }
                }
            }
            userErrors { field message }
        }
    }
'''

MUTATION_PRODUCT_UPDATE = '''
    mutation productUpdate($input: ProductInput!) {
        productUpdate(input: $input) {
            product {
                id
                tags
                status
                seo { title description }
            }
            userErrors { field message }
        }
    }
'''

MUTATION_VARIANTS_BULK_UPDATE = '''
    mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            productVariants {
                id
                price
                compareAtPrice
            }
            userErrors { field message }
        }
    }
'''

MUTATION_INVENTORY_SET = '''
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
        inventorySetQuantities(input: $input) {
            inventoryAdjustmentGroup {
                reason
                changes { name delta quantityAfterChange }
            }
            userErrors { field message }
        }
    }
'''


class ShopifyProduct(models.Model):
    _name = 'shopify.product'
    _description = 'Shopify Product Mapping'
    _inherit = ['mail.thread']

    name = fields.Char(string='Product Name', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')
    odoo_product_id = fields.Many2one('product.template', string='Odoo Product')
    shopify_product_id = fields.Char(string='Shopify Product ID (numeric)', readonly=True)
    shopify_gid = fields.Char(string='Shopify GID', readonly=True)
    shopify_product_url = fields.Char(string='Shopify URL')

    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('synced', 'Synced'),
        ('error', 'Error'),
    ], default='pending', tracking=True)
    last_synced = fields.Datetime(string='Last Synced', readonly=True)

    # ── Tags, SEO, Status ────────────────────────────────────────────────────
    shopify_status = fields.Selection([
        ('ACTIVE',   'Active'),
        ('DRAFT',    'Draft'),
        ('ARCHIVED', 'Archived'),
    ], string='Shopify Status', default='ACTIVE', tracking=True,
       help='Controls product visibility on Shopify.')

    tags = fields.Char(
        string='Tags (Shopify)',
        help='Comma-separated Shopify tags. Synced to/from Odoo product tags.',
    )
    seo_title = fields.Char(
        string='SEO Title',
        help='Overrides the product title in search engine results.',
    )
    seo_description = fields.Text(
        string='SEO Description',
        help='Overrides the product description in search engine results (max 320 chars).',
    )

    variant_ids = fields.One2many('shopify.product.variant', 'shopify_product_id', string='Variants')
    metafield_ids = fields.One2many('shopify.metafield', 'shopify_product_id', string='Metafields')
    image_ids = fields.One2many('shopify.product.image', 'shopify_product_id', string='Images')

    # ── Sync control ─────────────────────────────────────────────────────────
    exclude_from_sync = fields.Boolean(
        string='Exclude from Sync',
        default=False,
        help='When checked, this product is skipped during import and export sync.',
    )
    track_inventory = fields.Boolean(
        string='Track Inventory on Shopify',
        default=True,
        help='Unchecked: sets inventoryManagement to NOT_MANAGED on Shopify '
             '(product is never out of stock).',
    )

    # ── BoM / Kit stock ───────────────────────────────────────────────────────
    is_kit = fields.Boolean(
        string='Is Kit / Bundle',
        default=False,
        help='When enabled, available inventory is calculated from the Bill of Materials '
             'component stocks rather than the product\'s own stock.',
    )
    bom_id = fields.Many2one(
        'mrp.bom', string='Bill of Materials',
        domain="[('product_tmpl_id', '=', odoo_product_id)]",
        help='The BoM used to calculate available kit stock from component quantities.',
    )

    # Quick Jump — direct link to Shopify admin
    shopify_admin_url = fields.Char(
        string='View on Shopify',
        compute='_compute_shopify_admin_url',
    )

    @api.depends('instance_id', 'shopify_product_id')
    def _compute_shopify_admin_url(self):
        for rec in self:
            if rec.instance_id and rec.shopify_product_id:
                rec.shopify_admin_url = (
                    f'https://{rec.instance_id.shop_domain}'
                    f'/admin/products/{rec.shopify_product_id}'
                )
            else:
                rec.shopify_admin_url = ''

    # ------------------------------------------------------------------
    # Import: Shopify → Odoo  (GraphQL cursor pagination)
    # ------------------------------------------------------------------

    @api.model
    def import_products_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            count, cursor, has_next = self.import_products_page(instance, cursor)
            imported += count
            if not has_next:
                break
        _logger.info('Imported %d products from %s', imported, instance.name)
        return imported

    def import_products_page(self, instance, cursor=None):
        """Import one page of products → (count, next_cursor, has_next).
        Used by the chunked background importer so each job stays small."""
        variables = {'first': 50, 'after': cursor}
        data = instance._graphql_request(QUERY_PRODUCTS, variables=variables)
        connection = data.get('products', {})
        edges = connection.get('edges', [])
        for edge in edges:
            self._upsert_shopify_product(instance, edge['node'])
        page_info = connection.get('pageInfo', {})
        return len(edges), page_info.get('endCursor'), page_info.get('hasNextPage', False)

    def _upsert_shopify_product(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_product_id', '=', numeric_id),
        ], limit=1)

        # Tags: Shopify returns a list of strings
        tags_list = node.get('tags') or []
        tags_str  = ', '.join(tags_list) if isinstance(tags_list, list) else (tags_list or '')

        seo = node.get('seo') or {}

        vals = {
            'name':              node.get('title', ''),
            'shopify_product_id': numeric_id,
            'shopify_gid':       gid,
            'instance_id':       instance.id,
            'sync_status':       'synced',
            'last_synced':       fields.Datetime.now(),
            'shopify_status':    node.get('status', 'ACTIVE'),
            'tags':              tags_str,
            'seo_title':         seo.get('title', ''),
            'seo_description':   seo.get('description', ''),
        }
        rec = existing.write(vals) and existing or self.create(vals)
        rec._sync_variants_from_nodes(node.get('variants', {}).get('edges', []), instance)
        rec._sync_images_from_nodes(node.get('images', {}).get('edges', []), instance)
        # Sync tags to Odoo product if linked
        if tags_list and rec.odoo_product_id:
            rec._sync_tags_to_odoo(tags_list)
        return rec

    def _sync_tags_to_odoo(self, tags_list):
        """Create/find Odoo product.tag records and assign to the linked product.template."""
        self.ensure_one()
        if not self.odoo_product_id:
            return
        tag_ids = []
        for tag_name in tags_list:
            tag_name = (tag_name or '').strip()
            if not tag_name:
                continue
            tag = self.env['product.tag'].search([('name', '=', tag_name)], limit=1)
            if not tag:
                tag = self.env['product.tag'].create({'name': tag_name})
            tag_ids.append(tag.id)
        if tag_ids:
            self.odoo_product_id.write({'product_tag_ids': [(6, 0, tag_ids)]})

    def _sync_images_from_nodes(self, image_edges, instance):
        """Upsert product image records from GraphQL edges."""
        existing_urls = {img.image_url for img in self.image_ids}
        incoming_urls = set()
        position = 1
        for edge in image_edges:
            node = edge['node']
            url = node.get('url', '') or ''
            if not url:
                continue
            incoming_urls.add(url)
            image_id = instance._gid_to_id(node.get('id', ''))
            existing = self.env['shopify.product.image'].search([
                ('shopify_product_id', '=', self.id),
                ('image_url', '=', url),
            ], limit=1)
            vals = {
                'shopify_product_id': self.id,
                'shopify_image_id': image_id,
                'image_url': url,
                'alt_text': node.get('altText', '') or '',
                'position': position,
            }
            if existing:
                existing.write(vals)
            else:
                self.env['shopify.product.image'].create(vals)
            position += 1
        # Remove images no longer on Shopify
        stale = self.image_ids.filtered(lambda i: i.image_url not in incoming_urls)
        stale.unlink()

    def _sync_variants_from_nodes(self, variant_edges, instance):
        for edge in variant_edges:
            v = edge['node']
            variant_gid = v['id']
            variant_numeric = instance._gid_to_id(variant_gid)
            inv_item = v.get('inventoryItem') or {}
            inv_gid = inv_item.get('id', '')
            inv_numeric = instance._gid_to_id(inv_gid) if inv_gid else ''
            hs_code = inv_item.get('harmonizedSystemCode', '') or ''
            country_of_origin = inv_item.get('countryCodeOfOrigin', '') or ''

            existing = self.env['shopify.product.variant'].search([
                ('shopify_product_id', '=', self.id),
                ('shopify_variant_id', '=', variant_numeric),
            ], limit=1)
            vals = {
                'shopify_product_id': self.id,
                'shopify_variant_gid': variant_gid,
                'shopify_variant_id': variant_numeric,
                'title': v.get('title', ''),
                'price': float(v.get('price') or 0),
                'compare_at_price': float(v.get('compareAtPrice') or 0),
                'sku': v.get('sku', ''),
                'inventory_quantity': v.get('inventoryQuantity') or 0,
                'shopify_inventory_item_id': inv_numeric,
                'shopify_inventory_item_gid': inv_gid,
                'hs_code': hs_code,
                'country_of_origin': country_of_origin,
            }
            if existing:
                existing.write(vals)
            else:
                self.env['shopify.product.variant'].create(vals)

    # ------------------------------------------------------------------
    # Export: Odoo → Shopify  (GraphQL productCreate / productUpdate)
    # ------------------------------------------------------------------

    def _build_product_input(self):
        self.ensure_one()
        tmpl = self.odoo_product_id
        if not tmpl:
            raise UserError(_('No Odoo product linked to %s') % self.name)

        variants_input = []
        for variant in tmpl.product_variant_ids:
            v_input = {
                'price': str(variant.lst_price),
                'sku': variant.default_code or '',
                'inventoryManagement': 'SHOPIFY' if self.track_inventory else 'NOT_MANAGED',
                'weight': variant.weight,
                'weightUnit': 'KILOGRAMS',
            }
            # If variant already exists on Shopify include its GID so it's updated not duplicated
            mapped = self.env['shopify.product.variant'].search([
                ('shopify_product_id', '=', self.id),
                ('odoo_variant_id', '=', variant.id),
            ], limit=1)
            if mapped and mapped.shopify_variant_gid:
                v_input['id'] = mapped.shopify_variant_gid

            variants_input.append(v_input)

        # Use instance default status for new products; keep existing status for updates
        if self.shopify_gid:
            status = self.shopify_status or 'ACTIVE'
        else:
            status = self.instance_id.new_product_status or self.shopify_status or 'ACTIVE'

        product_input = {
            'title':         tmpl.name,
            'descriptionHtml': tmpl.description_sale or '',
            'vendor':        tmpl.categ_id.name or '',
            'productType':   tmpl.categ_id.name or '',
            'variants':      variants_input,
            'status':        status,
        }

        # Tags — merge Shopify tags field with Odoo product tags
        shopify_tags = [t.strip() for t in (self.tags or '').split(',') if t.strip()]
        odoo_tags    = [t.name for t in tmpl.product_tag_ids if t.name]
        merged_tags  = list(dict.fromkeys(shopify_tags + odoo_tags))  # dedup, preserve order
        if merged_tags:
            product_input['tags'] = merged_tags

        # SEO
        seo_title = self.seo_title or tmpl.name
        seo_desc  = self.seo_description or (tmpl.description_sale or '')[:320]
        if seo_title or seo_desc:
            product_input['seo'] = {
                'title':       seo_title,
                'description': seo_desc,
            }

        if self.shopify_gid:
            product_input['id'] = self.shopify_gid

        return product_input

    def action_export_to_shopify(self):
        for rec in self.filtered(lambda r: not r.exclude_from_sync):
            try:
                product_input = rec._build_product_input()
                if rec.shopify_gid:
                    data = rec.instance_id._graphql_request(MUTATION_PRODUCT_UPDATE, variables={'input': product_input})
                    result_key = 'productUpdate'
                else:
                    data = rec.instance_id._graphql_request(MUTATION_PRODUCT_CREATE, variables={'input': product_input})
                    result_key = 'productCreate'

                result = data.get(result_key, {})
                user_errors = result.get('userErrors', [])
                if user_errors:
                    raise UserError(_('Shopify error: %s') % user_errors[0]['message'])

                product_node = result.get('product', {})

                if result_key == 'productCreate':
                    gid = product_node.get('id', '')
                    rec.write({
                        'shopify_gid':        gid,
                        'shopify_product_id': rec.instance_id._gid_to_id(gid),
                    })
                    rec._sync_variants_from_nodes(
                        product_node.get('variants', {}).get('edges', []),
                        rec.instance_id,
                    )

                # Save confirmed tags/SEO/status back from Shopify response
                rec._update_from_product_node(product_node)
                rec.write({'sync_status': 'synced', 'last_synced': fields.Datetime.now()})
            except UserError as e:
                rec.write({'sync_status': 'error'})
                _logger.error('Product export failed for %s: %s', rec.name, e)

    def _update_from_product_node(self, product_node):
        """Update tags/SEO/status from a Shopify product node (returned by mutations)."""
        if not product_node:
            return
        tags_list = product_node.get('tags') or []
        tags_str  = ', '.join(tags_list) if isinstance(tags_list, list) else ''
        seo       = product_node.get('seo') or {}
        write_vals = {}
        if tags_str:
            write_vals['tags'] = tags_str
        if seo.get('title'):
            write_vals['seo_title'] = seo['title']
        if seo.get('description'):
            write_vals['seo_description'] = seo['description']
        if product_node.get('status'):
            write_vals['shopify_status'] = product_node['status']
        if write_vals:
            self.write(write_vals)

    # ------------------------------------------------------------------
    # Publish / Unpublish
    # ------------------------------------------------------------------

    def action_publish(self):
        """Set product status to ACTIVE on Shopify."""
        self._set_shopify_status('ACTIVE')

    def action_unpublish(self):
        """Set product status to DRAFT on Shopify."""
        self._set_shopify_status('DRAFT')

    def _set_shopify_status(self, status):
        for rec in self:
            if not rec.shopify_gid:
                continue
            data = rec.instance_id._graphql_request(
                MUTATION_PRODUCT_UPDATE,
                variables={'input': {'id': rec.shopify_gid, 'status': status}},
            )
            result = data.get('productUpdate', {})
            user_errors = result.get('userErrors', [])
            if user_errors:
                _logger.error('Status update failed for %s: %s', rec.name, user_errors)
            else:
                rec.shopify_status = status

    # ------------------------------------------------------------------
    # Tags + SEO push (lightweight — no variant changes)
    # ------------------------------------------------------------------

    def action_push_seo_tags(self):
        """Push only tags, SEO title/description, and status to Shopify (no variant changes)."""
        for rec in self:
            if not rec.shopify_gid:
                continue
            tmpl = rec.odoo_product_id

            shopify_tags = [t.strip() for t in (rec.tags or '').split(',') if t.strip()]
            odoo_tags    = [t.name for t in tmpl.product_tag_ids] if tmpl else []
            merged_tags  = list(dict.fromkeys(shopify_tags + odoo_tags))

            product_input = {'id': rec.shopify_gid}
            if merged_tags:
                product_input['tags'] = merged_tags
            if rec.seo_title or rec.seo_description:
                product_input['seo'] = {
                    'title':       rec.seo_title or '',
                    'description': (rec.seo_description or '')[:320],
                }
            if rec.shopify_status:
                product_input['status'] = rec.shopify_status

            try:
                data = rec.instance_id._graphql_request(
                    MUTATION_PRODUCT_UPDATE,
                    variables={'input': product_input},
                )
                result = data.get('productUpdate', {})
                user_errors = result.get('userErrors', [])
                if user_errors:
                    _logger.error('SEO/tags push failed for %s: %s', rec.name, user_errors[0]['message'])
                else:
                    rec._update_from_product_node(result.get('product', {}))
                    _logger.info('SEO/tags pushed for %s', rec.name)
            except UserError as e:
                _logger.error('SEO/tags push error for %s: %s', rec.name, e)

    # ------------------------------------------------------------------
    # Price push  (GraphQL productVariantsBulkUpdate)
    # ------------------------------------------------------------------

    def action_push_prices(self):
        """Push Odoo pricelist prices to Shopify variants using bulk mutation.

        Prices are converted from the pricelist's currency to the Shopify store's
        primary currency (instance.currency_id) before being pushed.
        """
        from datetime import date as date_cls
        for rec in self:
            if not rec.shopify_gid or not rec.odoo_product_id:
                continue

            instance = rec.instance_id
            shopify_currency = instance.currency_id  # target currency for Shopify
            pricelist = instance.pricelist_id
            pricelist_currency = pricelist.currency_id if pricelist else None
            company = instance.company_id or self.env.company
            today = date_cls.today()

            variants_input = []
            for variant in rec.variant_ids:
                if not variant.odoo_variant_id or not variant.shopify_variant_gid:
                    continue

                odoo_variant = variant.odoo_variant_id

                # Get price from pricelist if available, else lst_price
                if pricelist:
                    price = pricelist._get_product_price(
                        odoo_variant, 1.0, currency=pricelist_currency, date=today
                    )
                    compare_at = odoo_variant.lst_price
                else:
                    price = odoo_variant.lst_price
                    compare_at = odoo_variant.list_price

                # Convert to Shopify store currency if different from pricelist currency
                if shopify_currency and pricelist_currency and shopify_currency != pricelist_currency:
                    price = pricelist_currency._convert(
                        price, shopify_currency, company, today
                    )
                    compare_at = pricelist_currency._convert(
                        compare_at, shopify_currency, company, today
                    )

                entry = {
                    'id': variant.shopify_variant_gid,
                    'price': '{:.2f}'.format(price),
                }
                if compare_at and compare_at > price:
                    entry['compareAtPrice'] = '{:.2f}'.format(compare_at)
                variants_input.append(entry)

            if not variants_input:
                continue

            try:
                data = rec.instance_id._graphql_request(
                    MUTATION_VARIANTS_BULK_UPDATE,
                    variables={'productId': rec.shopify_gid, 'variants': variants_input},
                )
                result = data.get('productVariantsBulkUpdate', {})
                user_errors = result.get('userErrors', [])
                if user_errors:
                    _logger.error('Bulk price update errors: %s', user_errors)
                else:
                    for pv in result.get('productVariants', []):
                        variant = rec.variant_ids.filtered(
                            lambda v, gid=pv['id']: v.shopify_variant_gid == gid
                        )
                        if variant:
                            variant.write({'price': float(pv.get('price') or 0)})
            except UserError as e:
                _logger.error('Price push failed for %s: %s', rec.name, e)

    def push_variant_price_map(self, price_map):
        """Push explicit per-variant prices to Shopify (used by scheduled sales).

        :param price_map: {variant_gid: {'price': '12.00',
                                         'compareAtPrice': '20.00' or None}}
            A None compareAtPrice clears the strikethrough price.
        Updates the local variant cache to match.
        """
        self.ensure_one()
        if not self.shopify_gid:
            return
        variants_input = []
        for v in self.variant_ids:
            spec = price_map.get(v.shopify_variant_gid)
            if not spec:
                continue
            entry = {'id': v.shopify_variant_gid, 'price': spec['price']}
            # Include the key even when None so Shopify clears compareAtPrice.
            entry['compareAtPrice'] = spec.get('compareAtPrice')
            variants_input.append(entry)
        if not variants_input:
            return
        data = self.instance_id._graphql_request(
            MUTATION_VARIANTS_BULK_UPDATE,
            variables={'productId': self.shopify_gid, 'variants': variants_input},
        )
        result = data.get('productVariantsBulkUpdate', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Sale price update failed: %s') % errors[0].get('message'))
        for pv in result.get('productVariants', []):
            variant = self.variant_ids.filtered(
                lambda v, gid=pv['id']: v.shopify_variant_gid == gid)
            if variant:
                variant.write({
                    'price': float(pv.get('price') or 0),
                    'compare_at_price': float(pv.get('compareAtPrice') or 0),
                })

    # ------------------------------------------------------------------
    # Metafields
    # ------------------------------------------------------------------

    def action_fetch_metafields(self):
        """Fetch all metafields for this product from Shopify."""
        from .shopify_metafield import QUERY_PRODUCT_METAFIELDS
        for rec in self:
            if not rec.shopify_gid:
                continue
            cursor = None
            while True:
                variables = {'productId': rec.shopify_gid, 'first': 50, 'after': cursor}
                data = rec.instance_id._graphql_request(QUERY_PRODUCT_METAFIELDS, variables=variables)
                connection = (data.get('product') or {}).get('metafields', {})
                edges = connection.get('edges', [])
                self.env['shopify.metafield']._upsert_from_nodes(
                    edges, rec.instance_id, shopify_product_id=rec.id
                )
                page_info = connection.get('pageInfo', {})
                if not page_info.get('hasNextPage'):
                    break
                cursor = page_info.get('endCursor')

    def action_push_metafields(self):
        """Push all locally-modified metafields to Shopify."""
        for rec in self:
            modified = rec.metafield_ids.filtered('is_modified')
            if modified:
                modified.action_push_to_shopify()

    def action_export_metafields_from_odoo(self):
        """Read mapped Odoo fields and push them as metafields to Shopify."""
        for rec in self:
            if not rec.shopify_gid or not rec.odoo_product_id:
                continue
            mappings = self.env['shopify.metafield.mapping'].search([
                ('instance_id', '=', rec.instance_id.id),
                ('resource_type', 'in', ('product', 'variant')),
                ('direction', 'in', ('export', 'both')),
                ('odoo_field_id', '!=', False),
            ])
            metafields_input = []
            for mapping in mappings:
                odoo_record = rec.odoo_product_id
                field_name = mapping.odoo_field_id.name
                if not hasattr(odoo_record, field_name):
                    continue
                value = getattr(odoo_record, field_name)
                if value is False or value is None:
                    continue
                metafields_input.append({
                    'ownerId':   rec.shopify_gid,
                    'namespace': mapping.namespace,
                    'key':       mapping.key,
                    'value':     str(value),
                    'type':      mapping.value_type or 'single_line_text_field',
                })
            if metafields_input:
                from .shopify_metafield import MUTATION_METAFIELDS_SET
                rec.instance_id._graphql_request(
                    MUTATION_METAFIELDS_SET,
                    variables={'metafields': metafields_input},
                )

    @api.model
    def cron_push_prices(self):
        instances = self.env['shopify.instance'].search([('state', '=', 'connected'), ('sync_products', '=', True)])
        for instance in instances:
            products = self.search([('instance_id', '=', instance.id), ('sync_status', '=', 'synced')])
            products.action_push_prices()

    @api.model
    def cron_check_low_stock(self):
        """Check all synced products and fire low stock alerts when threshold is breached."""
        instances = self.env['shopify.instance'].search([('state', '=', 'connected')])
        for instance in instances:
            workflow = self.env['shopify.workflow'].search([
                ('instance_id', '=', instance.id),
                ('send_low_stock_alert', '=', True),
            ], limit=1)
            if not workflow:
                continue
            threshold = workflow.low_stock_threshold or 5
            products = self.search([('instance_id', '=', instance.id), ('sync_status', '=', 'synced')])
            for product in products:
                if any(v.inventory_quantity <= threshold for v in product.variant_ids):
                    workflow.notify_low_stock(product)

    # ------------------------------------------------------------------
    # Inventory sync  (GraphQL inventorySetQuantities)
    # ------------------------------------------------------------------

    def action_sync_inventory(self):
        for rec in self:
            if not rec.instance_id.sync_inventory:
                continue

            # Resolve the location to push to
            default_loc = rec.instance_id.default_location_id
            if not default_loc:
                _logger.warning('No default location set for instance %s — skipping inventory push', rec.instance_id.name)
                continue
            shopify_location_gid = default_loc.shopify_location_gid
            odoo_stock_location = default_loc.odoo_location_id

            quantities = []
            for variant in rec.variant_ids:
                if not variant.shopify_inventory_item_gid:
                    continue
                qty = 0
                if variant.odoo_variant_id and odoo_stock_location:
                    if rec.is_kit and rec.bom_id:
                        # Kit: calculate available qty from component stocks
                        qty = int(rec._get_kit_available_qty(odoo_stock_location))
                    else:
                        qty_type = rec.instance_id.inventory_qty_type or 'free'
                        if qty_type == 'on_hand':
                            quants = self.env['stock.quant'].search([
                                ('product_id', '=', variant.odoo_variant_id.id),
                                ('location_id', '=', odoo_stock_location.id),
                            ])
                            qty = int(sum(quants.mapped('quantity')))
                        elif qty_type == 'forecasted':
                            qty = int(variant.odoo_variant_id.with_context(
                                location=odoo_stock_location.id
                            ).virtual_available)
                        else:  # 'free' (default)
                            qty = int(self.env['stock.quant']._get_available_quantity(
                                variant.odoo_variant_id, odoo_stock_location
                            ))
                quantities.append({
                    'inventoryItemId': variant.shopify_inventory_item_gid,
                    'locationId': shopify_location_gid,
                    'quantity': qty,
                })

            if not quantities:
                continue

            try:
                data = rec.instance_id._graphql_request(
                    MUTATION_INVENTORY_SET,
                    variables={'input': {
                        'reason': 'correction',
                        'referenceDocumentUri': f'odoo://stock/instance/{rec.instance_id.id}',
                        'quantities': quantities,
                    }},
                )
                user_errors = data.get('inventorySetQuantities', {}).get('userErrors', [])
                if user_errors:
                    _logger.error('Inventory set errors: %s', user_errors)
            except UserError as e:
                _logger.error('Inventory sync failed for %s: %s', rec.name, e)

    # ------------------------------------------------------------------
    # Multi-store inventory broadcast (Odoo = single source of truth)
    # ------------------------------------------------------------------
    def broadcast_inventory(self):
        """Push Odoo's current stock for each product to EVERY connected store
        that sells it (matched by Odoo product template), so all Shopify stores
        stay identical to Odoo. Loop-safe: it pushes Odoo's value, not a delta."""
        templates = self.mapped('odoo_product_id')
        if not templates:
            return
        siblings = self.search([
            ('odoo_product_id', 'in', templates.ids),
            ('shopify_gid', '!=', False),
            ('instance_id.state', '=', 'connected'),
            ('instance_id.sync_inventory', '=', True),
        ])
        siblings.action_sync_inventory()
        return True

    @api.model
    def cron_broadcast_inventory(self):
        """Periodic reconciliation: push Odoo stock to all stores for every
        product on instances that have multi-store broadcast enabled."""
        instances = self.env['shopify.instance'].search([
            ('state', '=', 'connected'),
            ('broadcast_inventory_multistore', '=', True),
            ('sync_inventory', '=', True),
        ])
        if not instances:
            return
        products = self.search([
            ('instance_id', 'in', instances.ids), ('shopify_gid', '!=', False)])
        # Broadcast unique templates once (broadcast_inventory fans out to siblings)
        seen = set()
        for product in products:
            tmpl = product.odoo_product_id.id
            if tmpl in seen:
                continue
            seen.add(tmpl)
            try:
                product.broadcast_inventory()
            except Exception as e:
                _logger.error('Inventory broadcast failed for %s: %s', product.name, e)

    def _get_kit_available_qty(self, location):
        """
        Calculate how many complete kits can be assembled from component stock.

        For each BoM line: available_component_qty / qty_per_kit
        Returns the minimum (the limiting component).

        Requires `mrp` module. Falls back to 0 if mrp is not installed.
        """
        self.ensure_one()
        if not self.bom_id:
            return 0

        # Check mrp module is installed
        if not self.env['ir.model'].search([('model', '=', 'mrp.bom')], limit=1):
            _logger.warning('mrp module not installed — cannot calculate kit stock for %s', self.name)
            return 0

        bom = self.bom_id
        min_kits = float('inf')

        for line in bom.bom_line_ids:
            component = line.product_id
            component_qty_per_kit = line.product_qty

            if component_qty_per_kit <= 0:
                continue

            available = self.env['stock.quant']._get_available_quantity(
                component, location
            )
            kits_from_this = available / component_qty_per_kit
            if kits_from_this < min_kits:
                min_kits = kits_from_this

        return max(0, int(min_kits) if min_kits != float('inf') else 0)


class ShopifyProductVariant(models.Model):
    _name = 'shopify.product.variant'
    _description = 'Shopify Product Variant'

    shopify_product_id = fields.Many2one('shopify.product', string='Shopify Product', ondelete='cascade')
    odoo_variant_id = fields.Many2one('product.product', string='Odoo Variant')

    # Store both GID and numeric ID for flexibility
    shopify_variant_gid = fields.Char(string='Variant GID')
    shopify_variant_id = fields.Char(string='Variant ID (numeric)')
    shopify_inventory_item_gid = fields.Char(string='Inventory Item GID')
    shopify_inventory_item_id = fields.Char(string='Inventory Item ID (numeric)')
    shopify_location_gid = fields.Char(string='Location GID')
    shopify_location_id = fields.Char(string='Location ID (numeric)')

    title = fields.Char(string='Title')
    price = fields.Float(string='Price')
    compare_at_price = fields.Float(string='Compare At Price')
    sku = fields.Char(string='SKU')
    inventory_quantity = fields.Integer(string='Inventory Qty', readonly=True)
    hs_code = fields.Char(string='HS Code', help='Harmonized System tariff code.')
    country_of_origin = fields.Char(string='Country of Origin', help='ISO 2-letter country code.')


class ShopifyProductImage(models.Model):
    """Shopify product image gallery record."""
    _name = 'shopify.product.image'
    _description = 'Shopify Product Image'
    _order = 'shopify_product_id, position'

    shopify_product_id = fields.Many2one(
        'shopify.product', string='Product', required=True, ondelete='cascade',
    )
    shopify_image_id = fields.Char(string='Image ID', readonly=True)
    image_url = fields.Char(string='Image URL', readonly=True)
    alt_text = fields.Char(string='Alt Text')
    position = fields.Integer(string='Position', default=1, readonly=True)
    is_primary = fields.Boolean(
        string='Primary', compute='_compute_is_primary', store=True,
    )

    @api.depends('position')
    def _compute_is_primary(self):
        for rec in self:
            rec.is_primary = rec.position == 1
