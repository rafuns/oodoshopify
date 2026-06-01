from odoo import models, fields, tools, api, _


class ShopifySalesReport(models.Model):
    """
    Read-only analytical view over Shopify orders + order lines.
    Powers best-seller, sales-by-channel, and sales-by-period reporting
    via pivot and graph views.
    """
    _name = 'shopify.sales.report'
    _description = 'Shopify Sales Analysis'
    _auto = False
    _order = 'order_date desc'

    instance_id   = fields.Many2one('shopify.instance', string='Store', readonly=True)
    order_id      = fields.Many2one('shopify.order', string='Order', readonly=True)
    order_date    = fields.Datetime(string='Order Date', readonly=True)
    product_name  = fields.Char(string='Product', readonly=True)
    sku           = fields.Char(string='SKU', readonly=True)
    source_name   = fields.Char(string='Sales Channel', readonly=True)
    currency      = fields.Char(string='Currency', readonly=True)
    financial_status = fields.Char(string='Payment Status', readonly=True)
    quantity      = fields.Integer(string='Qty Sold', readonly=True)
    line_total    = fields.Float(string='Line Revenue', readonly=True)
    order_count   = fields.Integer(string='# Orders', readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, 'shopify_sales_report')
        self.env.cr.execute('''
            CREATE VIEW shopify_sales_report AS (
                SELECT
                    ol.id                                   AS id,
                    o.instance_id                           AS instance_id,
                    o.id                                    AS order_id,
                    o.shopify_order_date                    AS order_date,
                    ol.product_name                         AS product_name,
                    ol.sku                                  AS sku,
                    o.source_name                           AS source_name,
                    o.currency                              AS currency,
                    o.financial_status                      AS financial_status,
                    ol.quantity                             AS quantity,
                    (ol.price * ol.quantity - COALESCE(ol.total_discount, 0)) AS line_total,
                    1                                       AS order_count
                FROM shopify_order_line ol
                JOIN shopify_order o ON ol.order_id = o.id
            )
        ''')

    # ------------------------------------------------------------------
    # Best sellers helper (used by scheduled report)
    # ------------------------------------------------------------------

    @api.model
    def get_best_sellers(self, instance_id, limit=10):
        """Return top-selling products for an instance by quantity."""
        self.env.cr.execute('''
            SELECT product_name, SUM(quantity) AS qty, SUM(line_total) AS revenue
            FROM shopify_sales_report
            WHERE instance_id = %s
            GROUP BY product_name
            ORDER BY qty DESC
            LIMIT %s
        ''', (instance_id, limit))
        return self.env.cr.dictfetchall()


class ShopifyScheduledReport(models.Model):
    """Configurable scheduled sales report — emailed to recipients on a cadence."""
    _name = 'shopify.scheduled.report'
    _description = 'Shopify Scheduled Report'

    name = fields.Char(string='Report Name', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Store', required=True, ondelete='cascade')
    active = fields.Boolean(default=True)
    frequency = fields.Selection([
        ('daily',   'Daily'),
        ('weekly',  'Weekly'),
        ('monthly', 'Monthly'),
    ], string='Frequency', default='weekly', required=True)
    recipient_ids = fields.Many2many('res.users', string='Recipients')
    report_type = fields.Selection([
        ('best_sellers',   'Best Sellers'),
        ('sales_summary',  'Sales Summary'),
    ], string='Report Type', default='best_sellers')
    last_sent = fields.Datetime(string='Last Sent', readonly=True)

    def _build_report_body(self):
        self.ensure_one()
        if self.report_type == 'best_sellers':
            rows = self.env['shopify.sales.report'].get_best_sellers(self.instance_id.id, limit=10)
            lines = ''.join(
                f'<tr><td style="padding:6px">{r["product_name"]}</td>'
                f'<td style="padding:6px;text-align:right">{int(r["qty"] or 0)}</td>'
                f'<td style="padding:6px;text-align:right">{round(r["revenue"] or 0, 2)}</td></tr>'
                for r in rows
            )
            return (
                f'<h2>Best Sellers — {self.instance_id.name}</h2>'
                f'<table style="width:100%;border-collapse:collapse">'
                f'<tr style="background:#1a7a6e;color:#fff">'
                f'<th style="padding:8px;text-align:left">Product</th>'
                f'<th style="padding:8px;text-align:right">Qty</th>'
                f'<th style="padding:8px;text-align:right">Revenue</th></tr>'
                f'{lines}</table>'
            )
        else:
            self.env.cr.execute('''
                SELECT COUNT(DISTINCT order_id) AS orders, SUM(line_total) AS revenue
                FROM shopify_sales_report WHERE instance_id = %s
            ''', (self.instance_id.id,))
            row = self.env.cr.dictfetchone() or {}
            return (
                f'<h2>Sales Summary — {self.instance_id.name}</h2>'
                f'<p>Total orders: <strong>{row.get("orders", 0)}</strong></p>'
                f'<p>Total revenue: <strong>{round(row.get("revenue", 0) or 0, 2)}</strong></p>'
            )

    def action_send_now(self):
        for rec in self:
            body = rec._build_report_body()
            recipients = rec.recipient_ids or rec.env.user
            rec.env['mail.mail'].create({
                'subject': f'Shopify Report: {rec.name}',
                'body_html': body,
                'email_to': ','.join(recipients.mapped('email')),
            }).send()
            rec.last_sent = fields.Datetime.now()

    @api.model
    def cron_send_scheduled_reports(self):
        """Send due reports based on their frequency."""
        from datetime import datetime, timedelta
        now = datetime.utcnow()
        intervals = {'daily': 1, 'weekly': 7, 'monthly': 30}
        for report in self.search([('active', '=', True)]):
            due_days = intervals.get(report.frequency, 7)
            if not report.last_sent or (now - report.last_sent).days >= due_days:
                try:
                    report.action_send_now()
                except Exception as e:
                    _logger = __import__('logging').getLogger(__name__)
                    _logger.error('Scheduled report failed for %s: %s', report.name, e)
