import json
import logging
import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL — Bulk Operations (async JSONL export for large catalogs)
# ---------------------------------------------------------------------------

MUTATION_BULK_QUERY_RUN = '''
    mutation bulkOperationRunQuery($query: String!) {
        bulkOperationRunQuery(query: $query) {
            bulkOperation { id status }
            userErrors { field message }
        }
    }
'''

QUERY_CURRENT_BULK_OPERATION = '''
    query {
        currentBulkOperation {
            id
            status
            errorCode
            objectCount
            fileSize
            url
            partialDataUrl
        }
    }
'''

MUTATION_BULK_CANCEL = '''
    mutation bulkOperationCancel($id: ID!) {
        bulkOperationCancel(id: $id) {
            bulkOperation { id status }
            userErrors { field message }
        }
    }
'''

# Pre-built bulk export queries
BULK_QUERY_PRODUCTS = '''
{
  products {
    edges {
      node {
        id
        title
        status
        vendor
        productType
        tags
        variants {
          edges {
            node {
              id
              sku
              price
              inventoryQuantity
            }
          }
        }
      }
    }
  }
}
'''

BULK_QUERY_ORDERS = '''
{
  orders(query: "created_at:>2024-01-01") {
    edges {
      node {
        id
        name
        email
        createdAt
        displayFinancialStatus
        totalPriceSet { shopMoney { amount currencyCode } }
      }
    }
  }
}
'''

_STATUS_MAP = {
    'CREATED':   'running',
    'RUNNING':   'running',
    'COMPLETED': 'completed',
    'CANCELED':  'canceled',
    'CANCELING': 'canceling',
    'FAILED':    'failed',
    'EXPIRED':   'expired',
}


class ShopifyBulkOperation(models.Model):
    _name = 'shopify.bulk.operation'
    _description = 'Shopify Bulk Operation'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(string='Reference', required=True, default='Bulk Operation')
    instance_id = fields.Many2one(
        'shopify.instance', string='Instance', required=True, ondelete='cascade',
    )
    shopify_bulk_gid = fields.Char(string='Bulk Operation GID', readonly=True)

    operation_type = fields.Selection([
        ('products', 'Export Products'),
        ('orders',   'Export Orders'),
        ('custom',   'Custom Query'),
    ], string='Operation', default='products', required=True)

    custom_query = fields.Text(
        string='Custom GraphQL Query',
        help='Used when Operation = Custom. Must be a valid bulk-compatible query.',
    )

    status = fields.Selection([
        ('draft',     'Draft'),
        ('running',   'Running'),
        ('completed', 'Completed'),
        ('canceling', 'Canceling'),
        ('canceled',  'Canceled'),
        ('failed',    'Failed'),
        ('expired',   'Expired'),
    ], string='Status', default='draft', tracking=True)

    object_count = fields.Integer(string='Records', readonly=True)
    file_size = fields.Integer(string='File Size (bytes)', readonly=True)
    result_url = fields.Char(string='Result URL (JSONL)', readonly=True)
    error_code = fields.Char(string='Error Code', readonly=True)
    processed_count = fields.Integer(string='Processed into Odoo', readonly=True)

    _unique_bulk_gid = models.Constraint(
        'UNIQUE(shopify_bulk_gid)',
        'This bulk operation is already tracked.',
    )

    # ------------------------------------------------------------------
    # Start a bulk operation
    # ------------------------------------------------------------------

    def action_start(self):
        """Kick off the bulk export on Shopify."""
        self.ensure_one()

        # Shopify allows only ONE bulk query operation at a time per shop
        running = self.search([
            ('instance_id', '=', self.instance_id.id),
            ('status', '=', 'running'),
            ('id', '!=', self.id),
        ], limit=1)
        if running:
            raise UserError(_(
                'A bulk operation is already running for this instance (%s). '
                'Wait for it to finish or cancel it first.'
            ) % running.name)

        query_map = {
            'products': BULK_QUERY_PRODUCTS,
            'orders':   BULK_QUERY_ORDERS,
            'custom':   self.custom_query or '',
        }
        query = query_map.get(self.operation_type, '')
        if not query.strip():
            raise UserError(_('No query defined for this operation.'))

        data = self.instance_id._graphql_request(
            MUTATION_BULK_QUERY_RUN, variables={'query': query}
        )
        result = data.get('bulkOperationRunQuery', {})
        errors = result.get('userErrors', [])
        if errors:
            raise UserError(_('Bulk start error: %s') % errors[0]['message'])

        op = result.get('bulkOperation', {})
        self.write({
            'shopify_bulk_gid': op.get('id', ''),
            'status': _STATUS_MAP.get(op.get('status', 'RUNNING'), 'running'),
        })
        self.message_post(body=_('Bulk operation started on Shopify.'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Bulk Operation Started'),
                'message': _('Shopify is preparing your export. Status will update automatically.'),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Poll status
    # ------------------------------------------------------------------

    def action_refresh_status(self):
        """Poll Shopify for the current bulk operation status."""
        self.ensure_one()
        data = self.instance_id._graphql_request(QUERY_CURRENT_BULK_OPERATION)
        op = data.get('currentBulkOperation')
        if not op:
            return
        # Only update if it's our operation
        if self.shopify_bulk_gid and op.get('id') != self.shopify_bulk_gid:
            return

        new_status = _STATUS_MAP.get(op.get('status', ''), self.status)
        self.write({
            'status': new_status,
            'object_count': op.get('objectCount') or 0,
            'file_size': op.get('fileSize') or 0,
            'result_url': op.get('url') or '',
            'error_code': op.get('errorCode') or '',
        })
        return new_status

    def action_cancel(self):
        self.ensure_one()
        if not self.shopify_bulk_gid:
            return
        self.instance_id._graphql_request(
            MUTATION_BULK_CANCEL, variables={'id': self.shopify_bulk_gid}
        )
        self.write({'status': 'canceling'})

    # ------------------------------------------------------------------
    # Download + process JSONL result
    # ------------------------------------------------------------------

    def action_process_result(self):
        """Download the JSONL result file and import records into Odoo."""
        self.ensure_one()
        if self.status != 'completed':
            raise UserError(_('Bulk operation is not completed yet (status: %s).') % self.status)
        if not self.result_url:
            raise UserError(_('No result URL available.'))

        # Stream the JSONL file (can be large)
        try:
            response = requests.get(self.result_url, timeout=300, stream=True)
            response.raise_for_status()
        except Exception as e:
            raise UserError(_('Failed to download result file: %s') % e)

        processed = 0
        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            gid = obj.get('id', '')
            if not gid:
                continue
            # Route by GID type — only process top-level Product/Order nodes
            if '/Product/' in gid and self.operation_type == 'products':
                self._process_product_node(obj)
                processed += 1
            elif '/Order/' in gid and self.operation_type == 'orders':
                processed += 1  # order import via bulk is informational here

        self.write({'processed_count': processed})
        self.message_post(body=_('Processed %d records from bulk export.') % processed)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Bulk Import Complete'),
                'message': _('%d records processed into Odoo.') % processed,
                'type': 'success',
            },
        }

    def _process_product_node(self, obj):
        """Upsert a product mapping record from a bulk JSONL product node."""
        instance = self.instance_id
        gid = obj['id']
        numeric_id = instance._gid_to_id(gid)
        existing = self.env['shopify.product'].search([
            ('instance_id', '=', instance.id),
            ('shopify_product_id', '=', numeric_id),
        ], limit=1)
        tags = obj.get('tags') or []
        vals = {
            'name': obj.get('title', ''),
            'shopify_product_id': numeric_id,
            'shopify_gid': gid,
            'instance_id': instance.id,
            'sync_status': 'synced',
            'shopify_status': obj.get('status', 'ACTIVE'),
            'tags': ', '.join(tags) if isinstance(tags, list) else (tags or ''),
            'last_synced': fields.Datetime.now(),
        }
        if existing:
            existing.write(vals)
        else:
            self.env['shopify.product'].create(vals)

    # ------------------------------------------------------------------
    # Cron — auto-refresh running operations + auto-process completed
    # ------------------------------------------------------------------

    @api.model
    def cron_poll_bulk_operations(self):
        """Refresh all running bulk operations; auto-process newly completed ones."""
        running = self.search([('status', 'in', ['running', 'canceling'])])
        for op in running:
            try:
                new_status = op.action_refresh_status()
                if new_status == 'completed' and not op.processed_count:
                    op.action_process_result()
            except Exception as e:
                _logger.error('Bulk poll failed for %s: %s', op.name, e)
