import json
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ShopifyTheme(models.Model):
    _name = 'shopify.theme'
    _description = 'Shopify Theme'
    _inherit = ['mail.thread']

    name = fields.Char(string='Theme Name', required=True)
    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True, ondelete='cascade')
    shopify_theme_id = fields.Char(string='Shopify Theme ID', readonly=True)
    role = fields.Selection([
        ('main', 'Active / Published'),
        ('unpublished', 'Unpublished'),
        ('demo', 'Demo'),
    ], string='Role', readonly=True)
    is_active = fields.Boolean(string='Currently Active', readonly=True)
    theme_store_id = fields.Integer(string='Theme Store ID', readonly=True)
    created_at = fields.Datetime(string='Created At', readonly=True)
    updated_at = fields.Datetime(string='Last Updated', readonly=True)
    previewable = fields.Boolean(string='Previewable', readonly=True)
    processing = fields.Boolean(string='Processing', readonly=True)
    preview_url = fields.Char(string='Preview URL', readonly=True)

    setting_ids = fields.One2many('shopify.theme.setting', 'theme_id', string='Theme Settings')
    asset_ids = fields.One2many('shopify.theme.asset', 'theme_id', string='Assets')

    # ------------------------------------------------------------------
    # Fetch themes from Shopify
    # ------------------------------------------------------------------

    @api.model
    def sync_themes(self, instance):
        """Pull all themes from Shopify into Odoo."""
        data = instance._rest_request('GET', 'themes.json')
        themes = data.get('themes', [])
        synced = []
        for t in themes:
            theme_id = str(t['id'])
            existing = self.search([
                ('instance_id', '=', instance.id),
                ('shopify_theme_id', '=', theme_id),
            ], limit=1)

            role = t.get('role', 'unpublished')
            vals = {
                'name': t.get('name', ''),
                'shopify_theme_id': theme_id,
                'instance_id': instance.id,
                'role': role,
                'is_active': role == 'main',
                'theme_store_id': t.get('theme_store_id', 0),
                'previewable': t.get('previewable', False),
                'processing': t.get('processing', False),
            }
            if t.get('created_at'):
                vals['created_at'] = t['created_at'].replace('T', ' ').replace('Z', '')
            if t.get('updated_at'):
                vals['updated_at'] = t['updated_at'].replace('T', ' ').replace('Z', '')

            if existing:
                existing.write(vals)
                synced.append(existing)
            else:
                synced.append(self.create(vals))

        return synced

    # ------------------------------------------------------------------
    # Publish / activate theme
    # ------------------------------------------------------------------

    def action_publish_theme(self):
        """Set this theme as the active (main) theme on Shopify."""
        self.ensure_one()
        payload = {'theme': {'id': self.shopify_theme_id, 'role': 'main'}}
        self.instance_id._rest_request('PUT', f'themes/{self.shopify_theme_id}.json', payload=payload)
        # Mark all others as unpublished
        siblings = self.search([('instance_id', '=', self.instance_id.id), ('id', '!=', self.id)])
        siblings.write({'role': 'unpublished', 'is_active': False})
        self.write({'role': 'main', 'is_active': True})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Theme Published'),
                'message': _('"%s" is now the active theme.') % self.name,
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Theme settings (settings_data.json)
    # ------------------------------------------------------------------

    def action_fetch_settings(self):
        """Load settings_data.json from Shopify into setting_ids."""
        self.ensure_one()
        params = {'asset[key]': 'config/settings_data.json', 'theme_id': self.shopify_theme_id}
        data = self.instance_id._rest_request('GET', f'themes/{self.shopify_theme_id}/assets.json', params=params)
        asset = data.get('asset', {})
        raw_value = asset.get('value', '{}')
        try:
            settings = json.loads(raw_value)
        except json.JSONDecodeError:
            raise UserError(_('Could not parse settings_data.json from Shopify.'))

        # Flatten current section settings for display
        self.setting_ids.unlink()
        current = settings.get('current', {})
        for section_key, section_val in current.items():
            if isinstance(section_val, dict):
                for setting_key, setting_value in section_val.items():
                    self.env['shopify.theme.setting'].create({
                        'theme_id': self.id,
                        'section': section_key,
                        'key': setting_key,
                        'value': str(setting_value),
                        'original_value': str(setting_value),
                    })

    def action_push_settings(self):
        """Push modified settings back to Shopify as settings_data.json."""
        self.ensure_one()
        # Rebuild settings structure
        settings_current = {}
        for setting in self.setting_ids:
            if setting.section not in settings_current:
                settings_current[setting.section] = {}
            val = setting.value
            # Try to preserve original types (bool, int, float)
            if val.lower() in ('true', 'false'):
                val = val.lower() == 'true'
            else:
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
            settings_current[setting.section][setting.key] = val

        payload_value = json.dumps({'current': settings_current}, indent=2)
        payload = {
            'asset': {
                'key': 'config/settings_data.json',
                'value': payload_value,
            }
        }
        self.instance_id._rest_request('PUT', f'themes/{self.shopify_theme_id}/assets.json', payload=payload)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Settings Pushed'),
                'message': _('Theme settings updated on Shopify.'),
                'type': 'success',
            },
        }

    # ------------------------------------------------------------------
    # Assets (CSS / JS)
    # ------------------------------------------------------------------

    def action_fetch_assets(self):
        """Load the asset list for this theme."""
        self.ensure_one()
        data = self.instance_id._rest_request('GET', f'themes/{self.shopify_theme_id}/assets.json')
        assets = data.get('assets', [])
        self.asset_ids.unlink()
        for a in assets:
            self.env['shopify.theme.asset'].create({
                'theme_id': self.id,
                'key': a.get('key', ''),
                'content_type': a.get('content_type', ''),
                'size': a.get('size', 0),
                'updated_at': (a.get('updated_at') or '').replace('T', ' ').replace('Z', '') or False,
            })

    def action_fetch_asset_content(self):
        """Open wizard to edit a specific theme asset."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Theme Assets'),
            'res_model': 'shopify.theme.asset',
            'view_mode': 'list,form',
            'domain': [('theme_id', '=', self.id)],
        }


class ShopifyThemeSetting(models.Model):
    _name = 'shopify.theme.setting'
    _description = 'Shopify Theme Setting'
    _order = 'section, key'

    theme_id = fields.Many2one('shopify.theme', string='Theme', ondelete='cascade')
    section = fields.Char(string='Section')
    key = fields.Char(string='Key')
    value = fields.Char(string='Value')
    original_value = fields.Char(string='Original Value', readonly=True)
    is_modified = fields.Boolean(compute='_compute_modified')

    @api.depends('value', 'original_value')
    def _compute_modified(self):
        for rec in self:
            rec.is_modified = rec.value != rec.original_value


class ShopifyThemeAsset(models.Model):
    _name = 'shopify.theme.asset'
    _description = 'Shopify Theme Asset'
    _order = 'key'

    theme_id = fields.Many2one('shopify.theme', string='Theme', ondelete='cascade')
    key = fields.Char(string='Asset Key', readonly=True)
    content_type = fields.Char(string='Content Type', readonly=True)
    size = fields.Integer(string='Size (bytes)', readonly=True)
    updated_at = fields.Datetime(string='Last Updated', readonly=True)
    content = fields.Text(string='Content')
    is_loaded = fields.Boolean(default=False)

    def action_load_content(self):
        """Fetch the full content of this asset from Shopify."""
        self.ensure_one()
        params = {'asset[key]': self.key, 'theme_id': self.theme_id.shopify_theme_id}
        data = self.theme_id.instance_id._rest_request(
            'GET', f'themes/{self.theme_id.shopify_theme_id}/assets.json', params=params
        )
        asset = data.get('asset', {})
        self.write({
            'content': asset.get('value') or asset.get('attachment', ''),
            'is_loaded': True,
        })

    def action_save_to_shopify(self):
        """Push edited asset content back to Shopify."""
        self.ensure_one()
        if not self.content:
            raise UserError(_('No content to push.'))
        payload = {
            'asset': {
                'key': self.key,
                'value': self.content,
            }
        }
        self.theme_id.instance_id._rest_request(
            'PUT', f'themes/{self.theme_id.shopify_theme_id}/assets.json', payload=payload
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Asset Saved'),
                'message': _('"%s" pushed to Shopify.') % self.key,
                'type': 'success',
            },
        }
