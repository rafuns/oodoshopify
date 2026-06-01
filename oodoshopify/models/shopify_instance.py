import re
import time
import uuid
import requests
import hashlib
import hmac
import base64
import logging
from urllib.parse import urlencode
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

# Allowlist: only *.myshopify.com domains permitted (prevents SSRF)
_SHOPIFY_DOMAIN_RE = re.compile(
    r'^[a-zA-Z0-9][a-zA-Z0-9\-]{0,61}[a-zA-Z0-9]\.myshopify\.com$'
)

_logger = logging.getLogger(__name__)

SHOPIFY_API_VERSION = '2025-10'


class ShopifyInstance(models.Model):
    _name = 'shopify.instance'
    _description = 'Shopify Store Instance'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(string='Instance Name', required=True, tracking=True)
    shop_domain = fields.Char(
        string='Shop Domain',
        required=True,
        help='e.g. my-store.myshopify.com',
        tracking=True,
    )
    api_key = fields.Char(string='API Key', required=True)
    api_secret = fields.Char(string='API Secret', required=True)
    access_token = fields.Char(string='Access Token', readonly=True)
    webhook_secret = fields.Char(
        string='Webhook Secret',
        help='Used to verify incoming webhook HMAC signatures from Shopify',
    )
    webhook_token = fields.Char(
        string='Webhook Routing Token',
        default=lambda self: str(uuid.uuid4()),
        readonly=True,
        copy=False,
        help='Opaque UUID used to route inbound webhooks. '
             'Recreate instance or reset manually to rotate.',
    )

    @api.constrains('shop_domain')
    def _validate_shop_domain(self):
        """Prevent SSRF: only allow *.myshopify.com domains."""
        for rec in self:
            if rec.shop_domain and not _SHOPIFY_DOMAIN_RE.match(rec.shop_domain):
                raise ValidationError(_(
                    'Shop domain must be a valid *.myshopify.com domain '
                    '(e.g. my-store.myshopify.com). Got: %s'
                ) % rec.shop_domain)

    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Error'),
    ], string='Status', default='draft', tracking=True)

    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string='Company', default=lambda self: self.env.company)

    # ── AI content generation (OpenAI-compatible) ────────────────────────────
    ai_api_key = fields.Char(string='AI API Key', password=True,
                             help='OpenAI-compatible API key for AI content generation.')
    ai_base_url = fields.Char(string='AI Base URL', default='https://api.openai.com/v1',
                              help='OpenAI-compatible API base (change for Azure/OpenRouter/local).')
    ai_model = fields.Char(string='AI Model', default='gpt-4o-mini')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse')
    pricelist_id = fields.Many2one('product.pricelist', string='Default Pricelist')

    # ── Order import settings ─────────────────────────────────────────────────
    fallback_customer_id = fields.Many2one(
        'res.partner', string='Fallback Customer',
        help='Used for guest/anonymous orders that have no email address.',
    )
    order_import_delay_minutes = fields.Integer(
        string='Order Import Delay (minutes)',
        default=0,
        help='Wait this many minutes after order creation before importing. '
             '0 = import immediately. Useful to avoid importing orders that are '
             'still being edited by the customer.',
    )

    # ── Product defaults ──────────────────────────────────────────────────────
    new_product_status = fields.Selection([
        ('ACTIVE', 'Active — publish immediately'),
        ('DRAFT',  'Draft — review before publishing'),
    ], string='New Product Default Status', default='ACTIVE',
       help='Status applied when exporting a new product to Shopify for the first time.')

    # ── Inventory ─────────────────────────────────────────────────────────────
    inventory_qty_type = fields.Selection([
        ('free',       'Free to Use (available qty)'),
        ('on_hand',    'On Hand (total physical qty)'),
        ('forecasted', 'Forecasted (virtual available)'),
    ], string='Inventory Quantity Source', default='free',
       help='Which Odoo quantity to push to Shopify stock levels.')

    # ── Multi-currency ────────────────────────────────────────────────────────
    currency_id = fields.Many2one(
        'res.currency',
        string='Shopify Store Currency',
        help='The primary currency of this Shopify store. Auto-detected on Test Connection.',
    )
    pricelist_map_ids = fields.One2many(
        'shopify.pricelist.map', 'instance_id',
        string='Currency → Pricelist Map',
        help='Map a currency code to a specific Odoo pricelist. '
             'Used when importing orders in a non-default currency.',
    )

    sync_orders = fields.Boolean(string='Sync Orders', default=True)
    sync_products = fields.Boolean(string='Sync Products', default=True)
    sync_customers = fields.Boolean(string='Sync Customers', default=True)
    sync_inventory = fields.Boolean(string='Sync Inventory', default=True)

    # ── Smart Mapping ─────────────────────────────────────────────────────────
    product_matching_strategy = fields.Selection([
        ('sku',             'SKU only'),
        ('barcode',         'Barcode only'),
        ('sku_then_barcode','SKU first, then Barcode'),
    ], string='Product Matching Strategy', default='sku',
       help='How to match Shopify order lines to Odoo product.product records.')

    # ── Real-Time Inventory Push ──────────────────────────────────────────────
    realtime_inventory_push = fields.Boolean(
        string='Real-Time Inventory Push',
        default=False,
        help='Push inventory to Shopify instantly when stock changes in Odoo '
             '(via stock.quant override). Use with caution on high-volume warehouses.',
    )

    default_location_id = fields.Many2one(
        'shopify.location',
        string='Default Inventory Location',
        domain="[('instance_id', '=', id), ('is_active', '=', True)]",
        help='Shopify location used for inventory push. Sync locations first.',
    )

    # ── Accounting / Payouts ──────────────────────────────────────────────────
    payout_journal_id = fields.Many2one(
        'account.journal',
        string='Payout Bank Journal',
        domain=[('type', 'in', ['bank', 'cash'])],
        help='Bank journal that receives Shopify payouts.',
    )
    shopify_clearing_account_id = fields.Many2one(
        'account.account',
        string='Shopify Clearing Account',
        help='Intermediate account credited when a payout is received. '
             'Debit this account when posting sales invoices.',
    )
    shopify_fee_account_id = fields.Many2one(
        'account.account',
        string='Shopify Fees Expense Account',
        help='Expense account for Shopify transaction fees.',
    )

    # ── Webhooks ──────────────────────────────────────────────────────────────
    webhook_config_ids = fields.One2many(
        'shopify.webhook.config', 'instance_id',
        string='Webhook Subscriptions',
    )

    # ── Abandoned Checkout / CRM ──────────────────────────────────────────────
    abandoned_checkout_days_back = fields.Integer(
        string='Abandoned Checkout Lookback (days)',
        default=30,
        help='How many days back to fetch abandoned checkouts.',
    )
    crm_team_id = fields.Many2one(
        'crm.team', string='Sales Team for Leads',
        help='Assign abandoned checkout leads to this team.',
    )
    crm_stage_id = fields.Many2one(
        'crm.stage', string='Initial Lead Stage',
        help='Stage assigned when an abandoned checkout lead is created.',
    )
    crm_won_stage_id = fields.Many2one(
        'crm.stage', string='Won Lead Stage',
        domain=[('is_won', '=', True)],
        help='Stage to move the lead to when the checkout is recovered.',
    )

    product_count = fields.Integer(compute='_compute_counts')
    order_count = fields.Integer(compute='_compute_counts')
    customer_count = fields.Integer(compute='_compute_counts')
    location_count = fields.Integer(compute='_compute_counts')

    @api.depends('name')
    def _compute_counts(self):
        for rec in self:
            rec.product_count = self.env['shopify.product'].search_count([('instance_id', '=', rec.id)])
            rec.order_count = self.env['shopify.order'].search_count([('instance_id', '=', rec.id)])
            rec.customer_count = self.env['shopify.customer'].search_count([('instance_id', '=', rec.id)])
            rec.location_count = self.env['shopify.location'].search_count([('instance_id', '=', rec.id)])

    # ------------------------------------------------------------------
    # GID helpers  (Shopify GraphQL uses global IDs like gid://shopify/Product/123)
    # ------------------------------------------------------------------

    @staticmethod
    def _gid_to_id(gid):
        """Extract numeric ID string from a Shopify global ID."""
        if gid and '/' in str(gid):
            return str(gid).rsplit('/', 1)[-1]
        return str(gid)

    @staticmethod
    def _build_gid(resource, numeric_id):
        """Build a Shopify global ID from a resource type and numeric ID."""
        return f'gid://shopify/{resource}/{numeric_id}'

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _get_oauth_url(self, redirect_uri):
        self.ensure_one()
        scopes = ','.join([
            'read_products', 'write_products',
            'read_orders', 'write_orders',
            'read_customers', 'write_customers',
            'read_inventory', 'write_inventory',
            'read_themes', 'write_themes',
            'read_fulfillments', 'write_fulfillments',
        ])
        # CSRF token: signed with DB secret + instance ID, unguessable by outsiders
        db_secret = self.env['ir.config_parameter'].sudo().get_param(
            'database.secret', default=''
        )
        csrf = hmac.new(
            f'{db_secret}:{self.id}'.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()[:24]
        params = {
            'client_id': self.api_key,
            'scope': scopes,
            'redirect_uri': redirect_uri,
            'state': f'{self.id}:{csrf}',
        }
        return f'https://{self.shop_domain}/admin/oauth/authorize?{urlencode(params)}'

    def action_connect_oauth(self):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        redirect_uri = f'{base_url}/shopify/oauth/callback'
        return {
            'type': 'ir.actions.act_url',
            'url': self._get_oauth_url(redirect_uri),
            'target': 'self',
        }

    def _verify_oauth_callback(self, params):
        """Validate the OAuth callback per Shopify's security requirements.

        Checks:
        1. The `shop` param matches this instance's shop_domain (no shop swapping).
        2. The `hmac` param is a valid HMAC-SHA256 of the remaining query params,
           signed with the app's API secret.

        Returns True on success, False otherwise. `params` is the raw query dict.
        """
        self.ensure_one()
        # 1. Shop must match the instance we're connecting
        shop = params.get('shop', '')
        if shop and self.shop_domain and shop.lower() != self.shop_domain.lower():
            _logger.warning('OAuth callback: shop mismatch %s != %s', shop, self.shop_domain)
            return False

        # 2. HMAC over all params except `hmac`/`signature`, sorted & urlencoded
        provided = params.get('hmac', '')
        if not provided or not self.api_secret:
            return False
        message = '&'.join(
            f'{k}={v}' for k, v in sorted(params.items())
            if k not in ('hmac', 'signature')
        )
        digest = hmac.new(
            self.api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(digest, provided):
            _logger.warning('OAuth callback: HMAC verification failed for instance %s', self.id)
            return False
        return True

    def _exchange_code_for_token(self, code):
        """Exchange OAuth code for a permanent access token (REST — required by OAuth spec)."""
        self.ensure_one()
        url = f'https://{self.shop_domain}/admin/oauth/access_token'
        response = requests.post(url, json={
            'client_id': self.api_key,
            'client_secret': self.api_secret,
            'code': code,
        }, timeout=30)
        if response.status_code == 200:
            self.sudo().write({
                'access_token': response.json().get('access_token'),
                'state': 'connected',
            })
        else:
            self.sudo().write({'state': 'error'})
            raise UserError(_('OAuth token exchange failed: %s') % response.text)

    # ------------------------------------------------------------------
    # GraphQL request (primary method)
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_idempotency(query, key):
        """Add the @idempotent directive to a mutation operation so Shopify
        de-duplicates retries of financial mutations (required from 2026-04)."""
        return re.sub(
            r'(mutation\b[^{]*?)\{',
            lambda m: '%s@idempotent(key: "%s") {' % (m.group(1), key),
            query, count=1)

    def _graphql_request(self, query, variables=None, idempotency_key=None):
        """Execute a Shopify GraphQL query or mutation. Returns the full response dict.

        :param idempotency_key: when set, adds the @idempotent directive so a
            retried mutation is applied at most once (use for refunds/returns).
        """
        self.ensure_one()
        if not self.access_token:
            raise UserError(_('Instance "%s" is not connected.') % self.name)

        if idempotency_key:
            query = self._inject_idempotency(query, idempotency_key)

        url = f'https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json'
        headers = {
            'X-Shopify-Access-Token': self.access_token,
            'Content-Type': 'application/json',
        }
        payload = {'query': query}
        if variables:
            payload['variables'] = variables

        response = self._request_with_retry(
            'POST', url, json=payload, headers=headers
        )
        self._handle_rate_limit(response)

        if response.status_code != 200:
            _logger.error('Shopify GraphQL HTTP error %s: %s', response.status_code, response.text)
            self._log_error('GraphQL', response.text, response.status_code)
            raise UserError(_('Shopify API error %s.') % response.status_code)

        data = response.json()
        errors = data.get('errors')
        if errors:
            msg = '; '.join(e.get('message', str(e)) for e in errors)
            _logger.error('Shopify GraphQL errors: %s', msg)
            self._log_error('GraphQL', msg)
            raise UserError(_('Shopify GraphQL error: %s') % msg)

        # Cost-based throttling: if the query-cost bucket is running low, pause
        # briefly so the next call doesn't get throttled (Shopify uses a leaky
        # bucket, not request counts).
        self._throttle_on_cost(data)

        return data.get('data', {})

    def _throttle_on_cost(self, data):
        """Pre-emptively back off when the GraphQL cost bucket is nearly empty."""
        cost = (data.get('extensions') or {}).get('cost') or {}
        status = cost.get('throttleStatus') or {}
        available = status.get('currentlyAvailable')
        if available is None:
            return
        restore_rate = status.get('restoreRate') or 50
        requested = cost.get('requestedQueryCost') or 0
        needed = max(100, requested * 2)
        if available < needed:
            wait = min(5.0, (needed - available) / max(restore_rate, 1))
            if wait > 0:
                _logger.info(
                    'Shopify cost throttle: %s pts available, waiting %.1fs.',
                    available, wait)
                time.sleep(wait)

    # ------------------------------------------------------------------
    # REST request (used only for Theme API and OAuth)
    # ------------------------------------------------------------------

    def _rest_request(self, method, endpoint, payload=None, params=None):
        """REST API call — only use for endpoints with no GraphQL equivalent (themes, OAuth)."""
        self.ensure_one()
        if not self.access_token:
            raise UserError(_('Instance "%s" is not connected.') % self.name)

        url = f'https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/{endpoint}'
        headers = {
            'X-Shopify-Access-Token': self.access_token,
            'Content-Type': 'application/json',
        }
        response = self._request_with_retry(
            method.upper(), url, json=payload, params=params, headers=headers
        )
        self._handle_rate_limit(response)

        if response.status_code in (200, 201):
            return response.json()
        elif response.status_code == 204:
            return {}
        else:
            _logger.error('Shopify REST error [%s] %s: %s', response.status_code, url, response.text)
            self._log_error(f'REST {method.upper()} {endpoint}', response.text, response.status_code)
            raise UserError(_('Shopify REST error %s.') % response.status_code)

    def _request_with_retry(self, method, url, max_retries=3, **kwargs):
        """
        Execute an HTTP request with automatic retry on 429 (rate limit) and 5xx errors.
        Uses exponential back-off: 1s, 2s, 4s.
        Respects Shopify's Retry-After header on 429.
        """
        kwargs.setdefault('timeout', 60)
        last_response = None
        for attempt in range(max_retries):
            last_response = requests.request(method, url, **kwargs)
            status = last_response.status_code

            if status == 429:
                # Shopify rate-limit — honour Retry-After if present, else back-off
                retry_after = float(last_response.headers.get('Retry-After', 2 ** attempt))
                _logger.warning(
                    'Shopify 429 rate-limit (attempt %d/%d). Waiting %.1fs.',
                    attempt + 1, max_retries, retry_after,
                )
                time.sleep(retry_after)
                continue

            if status >= 500:
                wait = 2 ** attempt  # 1s, 2s, 4s
                _logger.warning(
                    'Shopify %s server error (attempt %d/%d). Retrying in %ds.',
                    status, attempt + 1, max_retries, wait,
                )
                time.sleep(wait)
                continue

            return last_response  # success or client error (4xx) — no retry

        return last_response  # return last response after exhausting retries

    def _handle_rate_limit(self, response):
        limit_header = response.headers.get('X-Shopify-Shop-Api-Call-Limit', '')
        if limit_header:
            try:
                used, total = limit_header.split('/')
                if int(used) >= int(total) - 5:
                    _logger.warning('Shopify rate limit close: %s/%s', used, total)
            except ValueError:
                pass

    def _log_error(self, operation, message, status_code=None):
        self.env['shopify.log'].sudo().create({
            'instance_id': self.id,
            'log_type': 'error',
            'operation': operation,
            'message': message,
            'status_code': str(status_code) if status_code else '',
        })

    # ------------------------------------------------------------------
    # Webhook HMAC verification
    # ------------------------------------------------------------------

    def verify_webhook_signature(self, raw_body, hmac_header):
        """Return True if the webhook payload matches Shopify's HMAC-SHA256 signature."""
        self.ensure_one()
        if not self.webhook_secret:
            return False
        digest = hmac.new(
            self.webhook_secret.encode('utf-8'),
            raw_body,
            digestmod=hashlib.sha256,
        ).digest()
        computed = base64.b64encode(digest).decode('utf-8')
        return hmac.compare_digest(computed, hmac_header)

    # ------------------------------------------------------------------
    # Test connection  (GraphQL)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Connected-state menu gating
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._sync_connected_menu_group()
        return records

    def write(self, vals):
        res = super().write(vals)
        # Only the fields that affect "is anything connected?" need a resync.
        if 'state' in vals or 'active' in vals:
            self._sync_connected_menu_group()
        return res

    def unlink(self):
        res = super().unlink()
        self._sync_connected_menu_group()
        return res

    @api.model
    def _sync_connected_menu_group(self):
        """Grant/revoke the auto-managed 'Shopify / Connected' group so that the
        operational menus appear only once at least one instance is connected.

        The group is given to every Shopify user (members of shopify_group_user)
        when a connected instance exists, and cleared otherwise.
        """
        connected_group = self.env.ref(
            'oodoshopify.shopify_group_connected', raise_if_not_found=False)
        base_group = self.env.ref(
            'oodoshopify.shopify_group_user', raise_if_not_found=False)
        if not connected_group or not base_group:
            return

        has_connection = self.sudo().search_count([('state', '=', 'connected')]) > 0
        if has_connection:
            # Everyone who is a Shopify user (directly or via implied groups)
            shopify_users = self.env['res.users'].sudo().search(
                [('all_group_ids', 'in', base_group.id)])
            target_ids = shopify_users.ids
        else:
            target_ids = []

        if set(connected_group.user_ids.ids) != set(target_ids):
            connected_group.sudo().write({'user_ids': [(6, 0, target_ids)]})
            # Group membership feeds menu visibility, which Odoo caches both
            # server-side (ormcache) and in the browser. Invalidate the server
            # cache so the next page load / re-login serves the right menus.
            self.env.registry.clear_cache()

    def ai_generate_product_content(self, product, want, tone='professional', language='English'):
        """Generate marketing copy for a product via an OpenAI-compatible API.

        :param want: set/list of 'title','description','seo_title','seo_description','tags'
        :returns: dict with the requested keys.
        """
        self.ensure_one()
        if not self.ai_api_key:
            raise UserError(_('Set an AI API Key on the store (Connection tab) first.'))
        import json as _json
        tmpl = product.odoo_product_id
        context_bits = [f'Product name: {product.name}']
        if tmpl:
            if tmpl.description_sale:
                context_bits.append(f'Current description: {tmpl.description_sale}')
            if getattr(tmpl, 'default_code', None):
                context_bits.append(f'SKU: {tmpl.default_code}')
        if product.tags:
            context_bits.append(f'Existing tags: {product.tags}')
        wanted = ', '.join(sorted(want))
        system = (
            'You are an expert e-commerce copywriter. Return ONLY valid JSON with the '
            'requested keys. "description" must be clean HTML. "tags" must be a comma-'
            'separated string. Keep seo_title under 60 chars and seo_description under 155.'
        )
        user = (
            f'Write {tone} marketing copy in {language} for this product. '
            f'Return JSON with exactly these keys: {wanted}.\n\n' + '\n'.join(context_bits)
        )
        payload = {
            'model': self.ai_model or 'gpt-4o-mini',
            'messages': [{'role': 'system', 'content': system},
                         {'role': 'user', 'content': user}],
            'temperature': 0.7,
            'response_format': {'type': 'json_object'},
        }
        base = (self.ai_base_url or 'https://api.openai.com/v1').rstrip('/')
        resp = requests.post(
            f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {self.ai_api_key}',
                     'Content-Type': 'application/json'},
            json=payload, timeout=90)
        if resp.status_code != 200:
            raise UserError(_('AI request failed (%s): %s') % (resp.status_code, resp.text[:300]))
        content = resp.json()['choices'][0]['message']['content']
        try:
            return _json.loads(content)
        except Exception:
            raise UserError(_('AI returned malformed content. Try again.'))

    def ai_complete(self, system, user, max_tokens=300):
        """Generic OpenAI-compatible text completion. Returns plain text ('' if no key)."""
        self.ensure_one()
        if not self.ai_api_key:
            return ''
        base = (self.ai_base_url or 'https://api.openai.com/v1').rstrip('/')
        resp = requests.post(
            f'{base}/chat/completions',
            headers={'Authorization': f'Bearer {self.ai_api_key}',
                     'Content-Type': 'application/json'},
            json={'model': self.ai_model or 'gpt-4o-mini',
                  'messages': [{'role': 'system', 'content': system},
                               {'role': 'user', 'content': user}],
                  'max_tokens': max_tokens, 'temperature': 0.3},
            timeout=60)
        if resp.status_code != 200:
            raise UserError(_('AI request failed (%s).') % resp.status_code)
        return resp.json()['choices'][0]['message']['content'].strip()

    def handle_app_uninstalled(self):
        """Called from the app/uninstalled webhook: the merchant removed the app,
        so the access token is dead. Disconnect cleanly."""
        for rec in self:
            rec.sudo().write({
                'state': 'draft',
                'access_token': False,
            })
            # Mark all webhook configs as disabled — they no longer exist on Shopify.
            rec.webhook_config_ids.sudo().write({
                'state': 'disabled', 'shopify_webhook_gid': False})
            rec._log_error('App Uninstalled',
                           'The Shopify app was uninstalled; instance disconnected.', 0)
        return True

    def action_test_connection(self):
        self.ensure_one()
        was_connected = self.state == 'connected'
        query = '''
        {
            shop {
                name
                myshopifyDomain
                currencyCode
                plan { displayName }
            }
        }
        '''
        try:
            data = self._graphql_request(query)
            shop = data.get('shop', {})
            write_vals = {'state': 'connected'}

            # Auto-detect and set store currency
            currency_code = shop.get('currencyCode', '')
            if currency_code:
                currency = self.env['res.currency'].search(
                    [('name', '=', currency_code.upper())], limit=1
                )
                if currency:
                    write_vals['currency_id'] = currency.id

            self.write(write_vals)

            # First successful connection unlocks the operational menus. Those
            # are gated on a group whose membership just changed — the browser's
            # cached menu tree won't reflect that until a full reload, so we
            # trigger one (it also serves as the success confirmation).
            if not was_connected:
                return {'type': 'ir.actions.client', 'tag': 'reload'}

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('Connected to: %s (%s) — Currency: %s') % (
                        shop.get('name'), shop.get('myshopifyDomain'), currency_code or '?'
                    ),
                    'type': 'success',
                },
            }
        except UserError:
            self.write({'state': 'error'})
            raise

    def _get_pricelist_for_currency(self, currency_code):
        """
        Return the best Odoo pricelist to use for a given currency code.
        Priority: pricelist_map_ids match → default pricelist if currency matches → default pricelist.
        """
        self.ensure_one()
        if currency_code:
            # 1. Check explicit map
            for mapping in self.pricelist_map_ids:
                if mapping.currency_id.name.upper() == currency_code.upper():
                    return mapping.pricelist_id
            # 2. Default pricelist has the right currency
            if self.pricelist_id and self.pricelist_id.currency_id.name.upper() == currency_code.upper():
                return self.pricelist_id
        # 3. Fallback to default pricelist regardless of currency
        return self.pricelist_id

    # ------------------------------------------------------------------
    # Webhook registration  (config-driven, per-topic toggles)
    # ------------------------------------------------------------------

    def _init_webhook_configs(self):
        """Create default webhook config records for all supported topics."""
        self.ensure_one()
        from .shopify_webhook_config import WEBHOOK_TOPIC_PATHS
        existing_topics = self.webhook_config_ids.mapped('topic')
        to_create = []
        for topic, path, _label in WEBHOOK_TOPIC_PATHS:
            if topic not in existing_topics:
                to_create.append({
                    'instance_id':   self.id,
                    'topic':         topic,
                    'callback_path': path,
                    'is_enabled':    True,
                    'state':         'draft',
                })
        if to_create:
            self.env['shopify.webhook.config'].create(to_create)

    def action_register_webhooks(self):
        """Register all enabled webhooks; delete disabled ones that were previously registered."""
        self.ensure_one()

        # Shopify only accepts public HTTPS callback URLs. On localhost / http
        # the registration fails with a cryptic "protocol http:// not supported"
        # for every topic — catch it early with an actionable message.
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        if not base_url.startswith('https://') or 'localhost' in base_url or '127.0.0.1' in base_url:
            raise UserError(_(
                "Webhooks require a public HTTPS URL that Shopify can reach.\n\n"
                "Your current Odoo address is:\n    %s\n\n"
                "Shopify rejects http:// and localhost addresses, so real-time "
                "webhooks can't be registered on a local install.\n\n"
                "To enable them:\n"
                "  1. Expose Odoo publicly, e.g.  ngrok http 8069\n"
                "  2. Settings → Technical → System Parameters → set "
                "'web.base.url' to the https://… tunnel address\n"
                "  3. Click 'Register Webhooks' again.\n\n"
                "You don't need webhooks to test syncing — use the Sync menu or "
                "the scheduled jobs to import/export data on a timer instead."
            ) % (base_url or '(not set)'))

        # Create any missing topic configs (idempotent — also picks up newly
        # added topics for instances that were set up before this version).
        self._init_webhook_configs()

        registered, deleted, failed = [], [], []
        for config in self.webhook_config_ids:
            if config.is_enabled and not config.shopify_webhook_gid:
                config._do_register()
                if config.state == 'active':
                    registered.append(config.topic_label)
                else:
                    failed.append(f'{config.topic_label}: {config.last_error}')
            elif not config.is_enabled and config.shopify_webhook_gid:
                config._do_delete()
                deleted.append(config.topic_label)

        parts = []
        if registered:
            parts.append(_('Registered: %s') % ', '.join(registered))
        if deleted:
            parts.append(_('Removed: %s') % ', '.join(deleted))
        if failed:
            parts.append(_('Failed: %s') % ', '.join(failed))
        if not parts:
            parts = [_('All enabled webhooks already registered.')]

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhooks'),
                'message': ' | '.join(parts),
                'type': 'success' if not failed else 'warning',
            },
        }

    def action_sync_webhook_status(self):
        """Fetch registered webhooks from Shopify and update config status."""
        self.ensure_one()
        if not self.webhook_config_ids:
            self._init_webhook_configs()
        self.env['shopify.webhook.config'].sync_from_shopify(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Webhook Status Synced'),
                'message': _('Webhook registrations refreshed from Shopify.'),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Location sync
    # ------------------------------------------------------------------

    def action_sync_locations(self):
        self.ensure_one()
        locations = self.env['shopify.location'].sync_locations(self)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Locations Synced'),
                'message': _('Found %d active locations. Set a Default Inventory Location below.') % len(locations),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------

    def action_view_products(self):
        return {'type': 'ir.actions.act_window', 'name': _('Shopify Products'),
                'res_model': 'shopify.product', 'view_mode': 'list,form',
                'domain': [('instance_id', '=', self.id)]}

    def action_view_orders(self):
        return {'type': 'ir.actions.act_window', 'name': _('Shopify Orders'),
                'res_model': 'shopify.order', 'view_mode': 'list,form',
                'domain': [('instance_id', '=', self.id)]}

    def action_view_customers(self):
        return {'type': 'ir.actions.act_window', 'name': _('Shopify Customers'),
                'res_model': 'shopify.customer', 'view_mode': 'list,form',
                'domain': [('instance_id', '=', self.id)]}

    def action_view_locations(self):
        return {'type': 'ir.actions.act_window', 'name': _('Shopify Locations'),
                'res_model': 'shopify.location', 'view_mode': 'list,form',
                'domain': [('instance_id', '=', self.id)]}
