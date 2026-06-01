import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyImportJob(models.Model):
    """One logical historical import (groups the chained page-jobs).

    The chunked importer creates one of these up front, then every page-level
    queue job updates it — giving the user a single record with a live progress
    bar instead of dozens of disconnected queue rows.
    """
    _name = 'shopify.import.job'
    _description = 'Shopify Import Job'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Import', required=True, default='Import')
    instance_id = fields.Many2one(
        'shopify.instance', string='Store', required=True, ondelete='cascade')
    kind = fields.Selection([
        ('orders', 'Orders'),
        ('products', 'Products'),
        ('customers', 'Customers'),
        ('collections', 'Collections'),
        ('payouts', 'Payouts'),
        ('abandoned', 'Abandoned Checkouts'),
        ('discounts', 'Discounts'),
    ], string='Type', required=True)

    state = fields.Selection([
        ('running', 'Running'),
        ('done', 'Completed'),
        ('failed', 'Failed'),
    ], string='Status', default='running', tracking=True)

    records_done = fields.Integer(string='Records Imported', readonly=True)
    records_total = fields.Integer(
        string='Estimated Total', readonly=True,
        help='Best-effort estimate from Shopify; 0 means unknown.')
    pages_done = fields.Integer(string='Pages Processed', readonly=True)
    progress = fields.Float(string='Progress', compute='_compute_progress')
    date_start = fields.Datetime(string='Started', readonly=True)
    date_done = fields.Datetime(string='Finished', readonly=True)
    error_message = fields.Text(string='Error', readonly=True)

    @api.depends('records_done', 'records_total', 'state')
    def _compute_progress(self):
        for job in self:
            if job.state == 'done':
                job.progress = 100.0
            elif job.records_total:
                job.progress = min(99.0, (job.records_done / job.records_total) * 100.0)
            else:
                # Unknown total — show indeterminate "almost there" while running.
                job.progress = 0.0 if job.state == 'failed' else 50.0

    @api.depends('kind', 'instance_id')
    def _compute_display_name(self):
        labels = dict(self._fields['kind'].selection)
        for job in self:
            job.display_name = _('%s import — %s') % (
                labels.get(job.kind, job.kind or ''), job.instance_id.name or '')

    # ------------------------------------------------------------------
    # Progress updates (called by the queue chunk handler)
    # ------------------------------------------------------------------
    def add_progress(self, count):
        for job in self:
            job.write({
                'records_done': job.records_done + (count or 0),
                'pages_done': job.pages_done + 1,
            })

    def mark_done(self):
        self.write({'state': 'done', 'date_done': fields.Datetime.now()})
        for job in self:
            job._notify(success=True)

    def mark_failed(self, message):
        self.write({
            'state': 'failed',
            'error_message': message,
            'date_done': fields.Datetime.now(),
        })
        for job in self:
            job._notify(success=False)

    def _notify(self, success):
        """Send an inbox/email notification to whoever launched the import."""
        self.ensure_one()
        user = self.create_uid or self.env.user
        if not user or not user.partner_id:
            return
        labels = dict(self._fields['kind'].selection)
        label = labels.get(self.kind, self.kind)
        if success:
            subject = _('✅ %s import finished') % label
            body = _('Imported %d records from %s.') % (self.records_done, self.instance_id.name)
        else:
            subject = _('❌ %s import failed') % label
            body = _('The %s import for %s failed after %d records.<br/>%s') % (
                label, self.instance_id.name, self.records_done, self.error_message or '')
        try:
            self.message_notify(
                partner_ids=user.partner_id.ids, subject=subject, body=body)
        except Exception as e:
            _logger.warning('Import job notification failed: %s', e)

    # ------------------------------------------------------------------
    # Best-effort total estimate for a real progress bar
    # ------------------------------------------------------------------
    @api.model
    def estimate_total(self, instance, kind, query_filter=''):
        """Return an approximate record count so the bar can show real %.
        Returns 0 (unknown) on any error or for kinds without a count query."""
        try:
            if kind == 'products':
                data = instance._graphql_request('{ productsCount { count } }')
                return (data.get('productsCount') or {}).get('count', 0)
            if kind == 'customers':
                data = instance._graphql_request('{ customersCount { count } }')
                return (data.get('customersCount') or {}).get('count', 0)
            if kind == 'orders':
                data = instance._graphql_request(
                    'query($q: String){ ordersCount(query: $q) { count } }',
                    variables={'q': query_filter or None})
                return (data.get('ordersCount') or {}).get('count', 0)
        except Exception as e:
            _logger.debug('Total estimate unavailable for %s: %s', kind, e)
        return 0

    def action_view_queue(self):
        """Open the page-level queue jobs for this import."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Queue'),
            'res_model': 'shopify.queue',
            'view_mode': 'list,form',
            'domain': [('operation', '=', 'historical_import'),
                       ('instance_id', '=', self.instance_id.id)],
        }
