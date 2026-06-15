import json
import hmac as _hmac
import hashlib
import logging
from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

_MAX_WEBHOOK_BYTES = 5 * 1024 * 1024  # 5 MB


class ShopifyWebhookController(http.Controller):

    # ------------------------------------------------------------------
    # OAuth callback
    # ------------------------------------------------------------------

    @http.route('/shopify/oauth/callback', type='http', auth='user', methods=['GET'])
    def oauth_callback(self, **kwargs):
        code  = kwargs.get('code')
        state = kwargs.get('state')

        if not code or not state:
            return request.make_response('Missing OAuth parameters', status=400)

        try:
            # state is "instance_id:csrf_token" (set in _get_oauth_url)
            parts = state.split(':', 1)
            if len(parts) != 2:
                return request.make_response('Invalid state parameter', status=400)

            instance_id   = int(parts[0])
            provided_csrf = parts[1]

            # Verify CSRF token (derived from DB secret + instance_id, server-side only)
            db_secret = request.env['ir.config_parameter'].sudo().get_param(
                'database.secret', default=''
            )
            expected_csrf = _hmac.new(
                f'{db_secret}:{instance_id}'.encode(),
                digestmod=hashlib.sha256,
            ).hexdigest()[:24]

            if not _hmac.compare_digest(provided_csrf, expected_csrf):
                _logger.warning('OAuth callback: invalid CSRF token for instance %s', instance_id)
                return request.make_response('Invalid state token', status=400)

            # Verify calling user has write access to this specific instance (no sudo bypass)
            instance = request.env['shopify.instance'].browse(instance_id)
            if not instance.exists():
                return request.make_response('Instance not found', status=404)
            instance.check_access('write')   # raises AccessError if user lacks rights

            # Verify Shopify's own hmac + shop params (signed with API secret)
            if not instance.sudo()._verify_oauth_callback(kwargs):
                return request.make_response('OAuth verification failed', status=400)

            instance.sudo()._exchange_code_for_token(code)
            return request.redirect('/odoo/shopify/instances/%d' % instance_id)

        except Exception as e:
            _logger.error('OAuth callback error: %s', e)
            # Do NOT return str(e) — hides internal details from the client
            return request.make_response(
                'OAuth error — check server logs', status=500
            )

    # ------------------------------------------------------------------
    # Webhook helpers
    # ------------------------------------------------------------------

    def _get_instance(self, token):
        """Look up a Shopify instance by its opaque webhook_token UUID."""
        if not token:
            return request.env['shopify.instance'].sudo().browse()
        return request.env['shopify.instance'].sudo().search(
            [('webhook_token', '=', token), ('active', '=', True)], limit=1
        )

    def _verify_and_parse(self, token):
        """
        Verify webhook_token routing, enforce 5 MB payload limit,
        require webhook_secret to be configured, and validate HMAC.

        Returns (instance, data) on success, (None, None) on any failure.
        Always returns HTTP 200 to Shopify — errors are logged only.
        """
        instance = self._get_instance(token)
        if not instance or not instance.exists():
            _logger.warning('Webhook received for unknown token: %.8s...', token or '')
            return None, None

        # Guard: read body with size limit before any processing
        raw_body = request.httprequest.get_data(cache=False)
        if len(raw_body) > _MAX_WEBHOOK_BYTES:
            _logger.warning(
                'Oversized webhook payload (%d bytes) rejected for instance %s',
                len(raw_body), instance.id,
            )
            return None, None

        hmac_header = request.httprequest.headers.get('X-Shopify-Hmac-Sha256', '')

        # Require webhook_secret — never skip verification silently
        if not instance.webhook_secret:
            _logger.error(
                'Webhook rejected: instance %s (id=%s) has no webhook_secret configured. '
                'Set one before registering webhooks.',
                instance.name, instance.id,
            )
            return None, None

        if not instance.verify_webhook_signature(raw_body, hmac_header):
            _logger.warning(
                'Webhook HMAC verification failed for instance %s', instance.id
            )
            return None, None

        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            return instance, {}

        return instance, data

    # ------------------------------------------------------------------
    # Webhook endpoints  (auth='none', routed by opaque token UUID)
    # ------------------------------------------------------------------

    @http.route('/shopify/webhook/orders/create',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_order_create(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.order'].sudo()._upsert_order(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/orders/updated',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_order_updated(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.order'].sudo()._upsert_order(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/orders/cancelled',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_order_cancelled(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            shopify_id = str(data.get('id', ''))
            order = request.env['shopify.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_order_id', '=', shopify_id),
            ], limit=1)
            if order:
                order.write({'financial_status': 'voided', 'fulfillment_status': 'restocked'})
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/products/update',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_product_update(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.product'].sudo()._upsert_shopify_product(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/customers/create',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_customer_create(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.customer'].sudo()._upsert_customer(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/customers/update',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_customer_update(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.customer'].sudo()._upsert_customer(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/inventory/update',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_inventory_update(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            inv_item_id    = str(data.get('inventory_item_id', ''))
            location_id_raw = str(data.get('location_id', ''))
            available      = data.get('available') or 0

            location = request.env['shopify.location'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_location_id', '=', location_id_raw),
            ], limit=1)
            if location:
                level = request.env['shopify.inventory.level'].sudo().search([
                    ('location_id', '=', location.id),
                    ('shopify_inventory_item_id', '=', inv_item_id),
                ], limit=1)
                if level:
                    level.available_quantity = available
                    _logger.info(
                        'Inventory webhook: item %s at %s → %s',
                        inv_item_id, location.name, available,
                    )
            # Multi-store fan-out: re-broadcast Odoo's authoritative stock for
            # this product to every connected store (keeps all stores identical).
            if instance.broadcast_inventory_multistore:
                variant = request.env['shopify.product.variant'].sudo().search([
                    ('shopify_inventory_item_id', '=', inv_item_id),
                    ('shopify_product_id.instance_id', '=', instance.id),
                ], limit=1)
                if variant.shopify_product_id:
                    try:
                        variant.shopify_product_id.broadcast_inventory()
                    except Exception as e:
                        _logger.warning('Inventory broadcast failed: %s', e)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/refunds/create',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_refund_create(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            order_id = str(data.get('order_id', ''))
            order = request.env['shopify.order'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_order_id', '=', order_id),
            ], limit=1)
            if order:
                order.write({'financial_status': 'partially_refunded'})
            request.env['shopify.refund'].sudo().create_from_webhook(instance, data)
        return request.make_response('OK', status=200)

    # ── Orders: paid / fulfilled ──────────────────────────────────────
    def _set_order_field(self, instance, data, vals):
        order = request.env['shopify.order'].sudo().search([
            ('instance_id', '=', instance.id),
            ('shopify_order_id', '=', str(data.get('id', ''))),
        ], limit=1)
        if order:
            order.write(vals)
        return order

    @http.route('/shopify/webhook/orders/paid',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_order_paid(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            self._set_order_field(instance, data, {'financial_status': 'paid'})
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/orders/fulfilled',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_order_fulfilled(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            self._set_order_field(instance, data, {'fulfillment_status': 'fulfilled'})
        return request.make_response('OK', status=200)

    # ── Products: create / delete ─────────────────────────────────────
    @http.route('/shopify/webhook/products/create',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_product_create(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.product'].sudo()._upsert_shopify_product(instance, data)
        return request.make_response('OK', status=200)

    @http.route('/shopify/webhook/products/delete',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_product_delete(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            product = request.env['shopify.product'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_product_id', '=', str(data.get('id', ''))),
            ], limit=1)
            if product:
                product.write({'shopify_status': 'ARCHIVED', 'sync_status': 'error'})
                _logger.info('Product %s archived (deleted on Shopify).', product.id)
        return request.make_response('OK', status=200)

    # ── Customers: delete (GDPR) ──────────────────────────────────────
    @http.route('/shopify/webhook/customers/delete',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_customer_delete(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            customer = request.env['shopify.customer'].sudo().search([
                ('instance_id', '=', instance.id),
                ('shopify_customer_id', '=', str(data.get('id', ''))),
            ], limit=1)
            if customer:
                customer.unlink()
                _logger.info('Customer mapping removed (deleted on Shopify).')
        return request.make_response('OK', status=200)

    # ── Collections: update ───────────────────────────────────────────
    @http.route('/shopify/webhook/collections/update',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_collection_update(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance and data:
            request.env['shopify.collection'].sudo()._upsert_collection(instance, data)
        return request.make_response('OK', status=200)

    # ── App uninstalled ───────────────────────────────────────────────
    @http.route('/shopify/webhook/app/uninstalled',
                type='http', auth='none', methods=['POST'], csrf=False)
    def webhook_app_uninstalled(self, token=None, **kwargs):
        instance, data = self._verify_and_parse(token)
        if instance:
            instance.sudo().handle_app_uninstalled()
        return request.make_response('OK', status=200)
