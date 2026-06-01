import json
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifyNLConsoleWizard(models.TransientModel):
    _name = 'shopify.nl.console.wizard'
    _description = 'Natural-Language Sync Console'

    instance_id = fields.Many2one('shopify.instance', string='Store', required=True)
    request = fields.Text(
        string='What do you want to sync?', required=True,
        placeholder='e.g. "import paid orders from the last 30 days" '
                    'or "pull all products"')
    parsed = fields.Text(string='Interpreted As', readonly=True)

    _SCHEMA_HINT = (
        'Return ONLY JSON: {"kind": one of '
        '["orders","products","customers","collections","payouts","abandoned","discounts"], '
        '"days_back": integer or null, "status": one of '
        '["any","open","closed","cancelled"] or null}. '
        'Infer sensible values from the request.'
    )

    def action_run(self):
        self.ensure_one()
        if not self.instance_id.ai_api_key:
            raise UserError(_('Set an AI API Key on the store to use the console.'))
        raw = self.instance_id.ai_complete(
            'You convert a merchant request into a Shopify import command. ' + self._SCHEMA_HINT,
            self.request, max_tokens=150)
        try:
            # tolerate code fences
            cleaned = raw.strip().strip('`')
            cleaned = cleaned[cleaned.find('{'):cleaned.rfind('}') + 1]
            spec = json.loads(cleaned)
        except Exception:
            raise UserError(_('Could not interpret the request. Try rephrasing.\nAI said: %s') % raw)

        kind = spec.get('kind')
        valid = {'orders', 'products', 'customers', 'collections', 'payouts', 'abandoned', 'discounts'}
        if kind not in valid:
            raise UserError(_('Unsupported request type: %s') % kind)

        params = {'chunked': True}
        if kind == 'orders':
            params['query_filter'] = self.env['shopify.order'].build_orders_query_filter(
                status=spec.get('status') or 'any',
                days_back=spec.get('days_back') or 30)
        elif kind == 'abandoned':
            params['days_back'] = spec.get('days_back') or 30

        self.parsed = json.dumps(spec, indent=2)
        self.env['shopify.queue'].enqueue_historical_import(self.instance_id, kind, params)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Import Started'),
                'message': _('Interpreted as a %s import — running in the background. '
                             'See Monitoring → Import Jobs.') % kind,
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
