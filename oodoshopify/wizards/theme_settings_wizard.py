from odoo import models, fields, api, _
from odoo.exceptions import UserError


class ThemeSettingsWizard(models.TransientModel):
    _name = 'shopify.theme.settings.wizard'
    _description = 'Theme Settings Wizard'

    instance_id = fields.Many2one('shopify.instance', string='Instance', required=True)
    action = fields.Selection([
        ('sync_themes', 'Sync Themes from Shopify'),
        ('fetch_settings', 'Fetch Theme Settings'),
        ('push_settings', 'Push Settings to Shopify'),
        ('fetch_assets', 'Fetch Asset List'),
    ], string='Action', default='sync_themes', required=True)
    theme_id = fields.Many2one('shopify.theme', string='Theme',
                               domain="[('instance_id', '=', instance_id)]")

    def action_confirm(self):
        if self.action == 'sync_themes':
            themes = self.env['shopify.theme'].sync_themes(self.instance_id)
            msg = _('Synced %d themes from Shopify.') % len(themes)
        elif self.action == 'fetch_settings':
            if not self.theme_id:
                raise UserError(_('Please select a theme.'))
            self.theme_id.action_fetch_settings()
            msg = _('Settings loaded for "%s".') % self.theme_id.name
        elif self.action == 'push_settings':
            if not self.theme_id:
                raise UserError(_('Please select a theme.'))
            self.theme_id.action_push_settings()
            msg = _('Settings pushed for "%s".') % self.theme_id.name
        elif self.action == 'fetch_assets':
            if not self.theme_id:
                raise UserError(_('Please select a theme.'))
            self.theme_id.action_fetch_assets()
            msg = _('Asset list loaded for "%s".') % self.theme_id.name
        else:
            msg = _('Done.')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Done'), 'message': msg, 'type': 'success'},
        }
