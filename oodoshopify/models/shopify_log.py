import csv
import io
import base64
import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)


class ShopifyLog(models.Model):
    _name = 'shopify.log'
    _description = 'Shopify Activity Log'
    _order = 'create_date desc'

    instance_id = fields.Many2one('shopify.instance', string='Instance', ondelete='cascade')
    log_type = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
        ('success', 'Success'),
    ], string='Type', default='info')
    operation = fields.Char(string='Operation')
    message = fields.Text(string='Message')
    status_code = fields.Char(string='HTTP Status')
    model_name = fields.Char(string='Model')
    record_id = fields.Integer(string='Record ID')
    create_date = fields.Datetime(string='Timestamp', readonly=True)

    def action_export_csv(self):
        """Download the current log selection as a CSV file."""
        records = self if self else self.search([])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['Timestamp', 'Instance', 'Type', 'Operation', 'Status', 'Message'])
        for rec in records:
            writer.writerow([
                rec.create_date or '',
                rec.instance_id.name if rec.instance_id else '',
                rec.log_type or '',
                rec.operation or '',
                rec.status_code or '',
                (rec.message or '').replace('\n', ' '),
            ])
        csv_data = buf.getvalue().encode('utf-8')
        attachment = self.env['ir.attachment'].create({
            'name': 'shopify_logs.csv',
            'type': 'binary',
            'datas': base64.b64encode(csv_data),
            'mimetype': 'text/csv',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    @api.model
    def cron_purge_old_logs(self):
        """Keep only last 30 days of logs."""
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(days=30)
        old = self.search([('create_date', '<', cutoff.strftime('%Y-%m-%d %H:%M:%S'))])
        old.unlink()
        _logger.info('Purged %d old Shopify log entries', len(old))
