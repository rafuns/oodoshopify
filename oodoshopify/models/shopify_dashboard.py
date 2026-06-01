from odoo import models, fields, api, _


class ShopifyDashboard(models.Model):
    """Read-only dashboard model — aggregates per-instance stats for the kanban view."""
    _name = 'shopify.dashboard'
    _description = 'Shopify Dashboard'
    _auto = False  # no database table — data computed on the fly
    _order = 'instance_name'

    instance_id = fields.Many2one('shopify.instance', string='Instance', readonly=True)
    instance_name = fields.Char(string='Store', readonly=True)
    shop_domain = fields.Char(string='Domain', readonly=True)
    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Error'),
    ], string='Status', readonly=True)

    # Order stats
    total_orders = fields.Integer(string='Total Orders', readonly=True)
    orders_today = fields.Integer(string='Orders Today', readonly=True)
    paid_orders = fields.Integer(string='Paid Orders', readonly=True)
    pending_orders = fields.Integer(string='Pending Orders', readonly=True)
    revenue_total = fields.Float(string='Total Revenue', readonly=True)

    # Sync stats
    products_synced = fields.Integer(string='Products Synced', readonly=True)
    products_pending = fields.Integer(string='Products Pending', readonly=True)
    customers_total = fields.Integer(string='Customers', readonly=True)
    queue_failed = fields.Integer(string='Queue Failures', readonly=True)
    refunds_pending = fields.Integer(string='Refunds Pending', readonly=True)
    running_imports = fields.Integer(string='Imports Running', readonly=True)
    open_drift = fields.Integer(string='Open Drift', readonly=True)

    # Last activity
    last_order_date = fields.Datetime(string='Last Order', readonly=True)

    def init(self):
        """Create the SQL view."""
        self.env.cr.execute('DROP VIEW IF EXISTS shopify_dashboard')
        self.env.cr.execute('''
            CREATE VIEW shopify_dashboard AS
            SELECT
                si.id                                           AS id,
                si.id                                           AS instance_id,
                si.name                                         AS instance_name,
                si.shop_domain                                  AS shop_domain,
                si.state                                        AS state,
                COALESCE(ord.total_orders, 0)                   AS total_orders,
                COALESCE(ord.orders_today, 0)                   AS orders_today,
                COALESCE(ord.paid_orders, 0)                    AS paid_orders,
                COALESCE(ord.pending_orders, 0)                 AS pending_orders,
                COALESCE(ord.revenue_total, 0.0)                AS revenue_total,
                COALESCE(ord.last_order_date, NULL)             AS last_order_date,
                COALESCE(prod.products_synced, 0)               AS products_synced,
                COALESCE(prod.products_pending, 0)              AS products_pending,
                COALESCE(cust.customers_total, 0)               AS customers_total,
                COALESCE(q.queue_failed, 0)                     AS queue_failed,
                COALESCE(ref.refunds_pending, 0)                AS refunds_pending,
                COALESCE(imp.running_imports, 0)                AS running_imports,
                COALESCE(dr.open_drift, 0)                      AS open_drift
            FROM shopify_instance si
            LEFT JOIN (
                SELECT
                    instance_id,
                    COUNT(*)                                                    AS total_orders,
                    SUM(CASE WHEN shopify_order_date::date = CURRENT_DATE
                             THEN 1 ELSE 0 END)                                 AS orders_today,
                    SUM(CASE WHEN financial_status = 'paid' THEN 1 ELSE 0 END) AS paid_orders,
                    SUM(CASE WHEN financial_status = 'pending' THEN 1 ELSE 0 END) AS pending_orders,
                    SUM(total_price)                                             AS revenue_total,
                    MAX(shopify_order_date)                                      AS last_order_date
                FROM shopify_order
                GROUP BY instance_id
            ) ord ON ord.instance_id = si.id
            LEFT JOIN (
                SELECT
                    instance_id,
                    SUM(CASE WHEN sync_status = 'synced'  THEN 1 ELSE 0 END) AS products_synced,
                    SUM(CASE WHEN sync_status = 'pending' THEN 1 ELSE 0 END) AS products_pending
                FROM shopify_product
                GROUP BY instance_id
            ) prod ON prod.instance_id = si.id
            LEFT JOIN (
                SELECT instance_id, COUNT(*) AS customers_total
                FROM shopify_customer
                GROUP BY instance_id
            ) cust ON cust.instance_id = si.id
            LEFT JOIN (
                SELECT instance_id, COUNT(*) AS queue_failed
                FROM shopify_queue
                WHERE state = 'failed'
                GROUP BY instance_id
            ) q ON q.instance_id = si.id
            LEFT JOIN (
                SELECT instance_id, COUNT(*) AS refunds_pending
                FROM shopify_refund
                WHERE state = 'pending'
                GROUP BY instance_id
            ) ref ON ref.instance_id = si.id
            LEFT JOIN (
                SELECT instance_id, COUNT(*) AS running_imports
                FROM shopify_import_job
                WHERE state = 'running'
                GROUP BY instance_id
            ) imp ON imp.instance_id = si.id
            LEFT JOIN (
                SELECT instance_id, COUNT(*) AS open_drift
                FROM shopify_drift
                WHERE state = 'open'
                GROUP BY instance_id
            ) dr ON dr.instance_id = si.id
            WHERE si.active = TRUE
        ''')

    # ------------------------------------------------------------------
    # Actions from dashboard cards
    # ------------------------------------------------------------------

    def action_view_orders(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Orders — %s') % self.instance_name,
            'res_model': 'shopify.order',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.instance_id.id)],
        }

    def action_view_pending_queue(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Failed Queue Jobs — %s') % self.instance_name,
            'res_model': 'shopify.queue',
            'view_mode': 'list',
            'domain': [('instance_id', '=', self.instance_id.id), ('state', '=', 'failed')],
        }

    def action_view_pending_refunds(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Pending Refunds — %s') % self.instance_name,
            'res_model': 'shopify.refund',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.instance_id.id), ('state', '=', 'pending')],
        }

    # ── Quick sync actions ────────────────────────────────────────────
    def action_sync_products(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Products'),
            'res_model': 'shopify.sync.products.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_instance_id': self.instance_id.id},
        }

    def action_sync_orders(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Sync Orders'),
            'res_model': 'shopify.sync.orders.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_instance_id': self.instance_id.id},
        }

    def action_view_running_imports(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Import Jobs — %s') % self.instance_name,
            'res_model': 'shopify.import.job',
            'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.instance_id.id)],
        }

    def action_open_instance(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'shopify.instance',
            'res_id': self.instance_id.id,
            'view_mode': 'form',
        }
