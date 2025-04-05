# -*- coding: utf-8 -*-
import logging
from odoo import fields, models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class ProductProduct(models.Model):
    _inherit = 'product.product'

    woo_variation_id = fields.Char(
        string="WooCommerce Variation ID",
        copy=False,
        readonly=True,
        help="The ID of this product variation in WooCommerce."
    )
    # Use related fields for easier access in views if needed, or add specific ones
    woo_sync_enabled = fields.Boolean(
        related='product_tmpl_id.woo_sync_enabled',
        string="Sync with WooCommerce",
        store=False, # No need to store, just for display/filtering
        readonly=True,
    )
    woo_last_sync_date = fields.Datetime(
        string="Last WooCommerce Sync",
        # related='product_tmpl_id.woo_last_sync_date', # Let's make it specific if needed later
        copy=False,
        readonly=True,
        help="Last sync timestamp specifically for this variation update."
    )
    woo_sync_error = fields.Text(
        string="WooCommerce Sync Error",
        copy=False,
        readonly=True,
        help="Specific error related to this variation during the last sync."
    )

    def action_sync_variant_parent_to_woocommerce(self):
        """Button action to trigger sync for the parent template"""
        templates_to_sync = self.mapped('product_tmpl_id')
        if not templates_to_sync:
             raise UserError(_("No parent product template found for the selected variant(s)."))

        enabled_templates = templates_to_sync.filtered('woo_sync_enabled')
        disabled_templates = templates_to_sync - enabled_templates

        if disabled_templates:
            _logger.warning("Sync requested from variant, but parent template(s) %s are not enabled for sync.",
                            disabled_templates.mapped('name'))
            # Optionally raise error or just sync the enabled ones
            # raise UserError(_("Synchronization is not enabled for the parent product(s): %s") % ', '.join(disabled_templates.mapped('name')))

        if not enabled_templates:
             raise UserError(_("Synchronization is not enabled for the parent product template(s) of the selected variant(s)."))


        _logger.info(f"Manual sync triggered for template(s) '{', '.join(enabled_templates.mapped('name'))}' from variant view.")
        # Call the template's sync method
        enabled_templates.sync_to_woocommerce() # Sync method handles multiple records

        # Provide feedback
        message = _('Synchronization process started for product template(s): %s.') % ', '.join(enabled_templates.mapped('name'))
        if disabled_templates:
             message += _('\nNote: Sync was not initiated for disabled templates: %s.') % ', '.join(disabled_templates.mapped('name'))

        return {
             'type': 'ir.actions.client',
             'tag': 'display_notification',
             'params': {
                 'title': _('Sync Initiated'),
                 'message': message,
                 'sticky': False,
                 'type': 'info',
             }
         }