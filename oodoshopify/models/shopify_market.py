import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

QUERY_MARKETS = '''
    query GetMarkets($first: Int!, $after: String) {
        markets(first: $first, after: $after) {
            pageInfo { hasNextPage endCursor }
            edges {
                node {
                    id
                    name
                    handle
                    enabled
                    primary
                    currencySettings { baseCurrency { currencyCode } }
                    regions(first: 50) {
                        edges { node { name ... on MarketRegionCountry { code } } }
                    }
                }
            }
        }
    }
'''

MUTATION_MARKET_CREATE = '''
    mutation marketCreate($input: MarketCreateInput!) {
        marketCreate(input: $input) {
            market { id name handle }
            userErrors { field message }
        }
    }
'''

MUTATION_MARKET_UPDATE = '''
    mutation marketUpdate($id: ID!, $input: MarketUpdateInput!) {
        marketUpdate(id: $id, input: $input) {
            market { id name }
            userErrors { field message }
        }
    }
'''


class ShopifyMarket(models.Model):
    _name = 'shopify.market'
    _description = 'Shopify Market (Region)'
    _inherit = ['mail.thread']
    _order = 'is_primary desc, name'

    name = fields.Char(string='Market Name', required=True)
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_market_id  = fields.Char(string='Market ID', readonly=True)
    shopify_market_gid = fields.Char(string='Market GID', readonly=True)

    handle = fields.Char(string='Handle', readonly=True)
    enabled = fields.Boolean(string='Enabled', default=True)
    is_primary = fields.Boolean(string='Primary Market', readonly=True)
    base_currency = fields.Char(string='Base Currency', readonly=True)
    region_codes = fields.Char(string='Region Codes', readonly=True,
                               help='Comma-separated ISO country codes in this market.')

    # Odoo mapping for region-specific operations
    odoo_pricelist_id = fields.Many2one('product.pricelist', string='Odoo Pricelist')
    odoo_company_id = fields.Many2one('res.company', string='Odoo Company / Branch')
    odoo_warehouse_id = fields.Many2one('stock.warehouse', string='Odoo Warehouse')
    shopify_price_list_gid = fields.Char(
        string='Price List GID',
        help='Shopify price list to push per-market prices into. Create a price list '
             'for this market in Shopify, then paste its GID here.')
    last_price_push = fields.Datetime(string='Last Price Push', readonly=True)

    _unique_market_per_instance = models.Constraint(
        'UNIQUE(instance_id, shopify_market_id)',
        'This market already exists for this instance.',
    )

    @api.model
    def import_from_shopify(self, instance):
        cursor = None
        imported = 0
        while True:
            data = instance._graphql_request(
                QUERY_MARKETS, variables={'first': 50, 'after': cursor}
            )
            connection = data.get('markets', {})
            for edge in connection.get('edges', []):
                self._upsert(instance, edge['node'])
                imported += 1
            page_info = connection.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        _logger.info('Imported %d markets from %s', imported, instance.name)
        return imported

    def _upsert(self, instance, node):
        gid = node['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('shopify_market_id', '=', numeric_id),
        ], limit=1)
        regions = [
            r['node'].get('code', '')
            for r in node.get('regions', {}).get('edges', [])
            if r['node'].get('code')
        ]
        vals = {
            'name': node.get('name', ''),
            'instance_id': instance.id,
            'shopify_market_id': numeric_id,
            'shopify_market_gid': gid,
            'handle': node.get('handle', ''),
            'enabled': node.get('enabled', True),
            'is_primary': node.get('primary', False),
            'base_currency': (node.get('currencySettings') or {}).get('baseCurrency', {}).get('currencyCode', ''),
            'region_codes': ', '.join(regions),
        }
        if existing:
            existing.write(vals)
        else:
            self.create(vals)

    @api.model
    def cron_sync_markets(self):
        for instance in self.env['shopify.instance'].search([('state', '=', 'connected')]):
            try:
                self.import_from_shopify(instance)
            except Exception as e:
                _logger.error('Market sync failed for %s: %s', instance.name, e)

    # ------------------------------------------------------------------
    # Push per-market prices from the mapped Odoo pricelist
    # ------------------------------------------------------------------
    MUTATION_PRICE_LIST_FIXED = '''
        mutation priceListFixedPricesAdd($priceListId: ID!, $prices: [PriceListPriceInput!]!) {
            priceListFixedPricesAdd(priceListId: $priceListId, prices: $prices) {
                prices { variant { id } }
                userErrors { field message }
            }
        }
    '''

    def action_push_market_prices(self):
        """Push fixed prices from the mapped Odoo pricelist into this market's
        Shopify price list (in the market's currency)."""
        self.ensure_one()
        if not self.shopify_price_list_gid:
            raise UserError(_(
                'Set the Price List GID first. In Shopify, create a price list for '
                'this market, then paste its GID here.'))
        if not self.odoo_pricelist_id:
            raise UserError(_('Map an Odoo pricelist to this market first.'))

        instance = self.instance_id
        pricelist = self.odoo_pricelist_id
        currency = (self.base_currency or pricelist.currency_id.name or '').upper()
        from datetime import date as _date
        today = _date.today()

        prices = []
        products = self.env['shopify.product'].search([
            ('instance_id', '=', instance.id), ('shopify_gid', '!=', False)])
        for product in products:
            for variant in product.variant_ids:
                if not variant.shopify_variant_gid or not variant.odoo_variant_id:
                    continue
                amount = pricelist._get_product_price(
                    variant.odoo_variant_id, 1.0, currency=pricelist.currency_id, date=today)
                prices.append({
                    'variantId': variant.shopify_variant_gid,
                    'price': {'amount': '{:.2f}'.format(amount), 'currencyCode': currency},
                })
        if not prices:
            raise UserError(_('No mapped variants to price.'))

        # Shopify caps batch size; send in chunks of 250
        pushed = 0
        for i in range(0, len(prices), 250):
            batch = prices[i:i + 250]
            data = instance._graphql_request(
                self.MUTATION_PRICE_LIST_FIXED,
                variables={'priceListId': self.shopify_price_list_gid, 'prices': batch})
            errors = (data.get('priceListFixedPricesAdd') or {}).get('userErrors', [])
            if errors:
                raise UserError(_('Price list push failed: %s') % errors[0].get('message'))
            pushed += len(batch)

        self.last_price_push = fields.Datetime.now()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Market Prices Pushed'),
                       'message': _('%d prices pushed to %s.') % (pushed, self.name),
                       'type': 'success'},
        }
