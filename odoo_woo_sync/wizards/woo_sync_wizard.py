# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class WooSyncWizard(models.TransientModel):
    _name = 'woo.sync.wizard'
    _description = 'WooCommerce Synchronization Wizard'

    # --- Keep these fields for the actual sync actions ---
    product_tmpl_ids = fields.Many2many(
        'product.template',
        string='Products (for Sync action)',
        help="Select specific products for the 'Sync Selected' action. Only enabled products will be processed.",
    )
    sync_all_enabled = fields.Boolean(
        string="Use 'All Enabled' for Sync Actions",
        default=False,
        help="If checked, the 'Sync...' buttons will ignore the product selection above and process ALL currently enabled products (filtered further by the specific button pressed)."
    )

    @api.model
    def default_get(self, fields_list):
        """ Get default products from context if action is called from tree/form view """
        res = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids')
        active_model = self.env.context.get('active_model')
        if active_model == 'product.template' and active_ids:
             res['product_tmpl_ids'] = [(6, 0, active_ids)]
        elif active_model == 'product.product' and active_ids:
             templates = self.env['product.product'].browse(active_ids).mapped('product_tmpl_id')
             res['product_tmpl_ids'] = [(6, 0, templates.ids)]
        return res

    def _check_prerequisites(self):
        """ Helper method to check basic configuration """
        params = self.env['ir.config_parameter'].sudo()
        sync_active = params.get_param('odoo_woo_sync.sync_active', 'False') == 'True'
        woo_url = params.get_param('odoo_woo_sync.woo_url')
        if not sync_active:
             raise UserError(_("WooCommerce Sync is not active in Settings."))
        if not woo_url:
            raise UserError(_("WooCommerce URL is not configured in Settings."))

    def _process_sync_results(self, products_synced, initial_count, skipped_count=0):
        """ Helper method to generate notification based on sync results """
        # ... (Keep this helper method as defined previously) ...
        successful_syncs = products_synced.filtered(lambda p: not p.woo_sync_error)
        errors_found = products_synced - successful_syncs
        sync_count = len(successful_syncs)
        error_count = len(errors_found)
        attempted_count = len(products_synced)

        final_message = _('WooCommerce sync process finished.\nAttempted: %d\nSuccessfully Synced: %d\nWith Errors: %d') % (attempted_count, sync_count, error_count)
        if skipped_count > 0:
            final_message += _('\nSkipped (Not matching filter): %d') % skipped_count

        final_title = _('Sync Finished')
        notif_type = 'success' if error_count == 0 else 'warning' # Simplified condition

        if error_count > 0:
             error_product_names = ', '.join(errors_found.mapped('name'))
             if len(error_product_names) > 150: error_product_names = error_product_names[:150] + '...'
             final_message += _("\nProducts with errors: %s") % error_product_names
             final_message += _("\nCheck logs or product forms for details.")
             _logger.warning(f"Wizard Sync completed with {error_count} errors.")

        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': { 'title': final_title, 'message': final_message, 'sticky': error_count > 0 or skipped_count > 0,
                        'type': notif_type, 'next': {'type': 'ir.actions.act_window_close'}, } }


    def action_confirm_sync(self):
        """ Executes sync for selected/all ENABLED products. """
        # ... (Keep this method as defined previously) ...
        self.ensure_one(); _logger.info("Wizard: Starting standard sync."); self._check_prerequisites()
        sync_context = {'manual_sync_trigger': True}; ProductTemplate = self.env['product.template'].with_context(sync_context)
        products_to_sync = self.env['product.template']; initial_selection_count = 0; disabled_selected_count = 0
        if self.sync_all_enabled:
            products_to_sync = ProductTemplate.search([('woo_sync_enabled', '=', True)])
            initial_selection_count = len(products_to_sync)
            if not products_to_sync: raise UserError(_("Sync All selected, but no products are enabled."))
            _logger.info(f"Wizard: Syncing all {initial_selection_count} enabled products.")
        elif self.product_tmpl_ids:
            initial_selection_count = len(self.product_tmpl_ids)
            products_to_sync = self.product_tmpl_ids.filtered('woo_sync_enabled')
            disabled_selected_count = initial_selection_count - len(products_to_sync)
            if not products_to_sync: raise UserError(_("Selected %d products, but none are enabled.") % initial_selection_count)
            _logger.info(f"Wizard: Syncing {len(products_to_sync)} selected enabled products (skipped {disabled_selected_count}).")
        else: raise UserError(_("Select products or check 'Use All Enabled'."))
        try: products_to_sync.sync_to_woocommerce(); return self._process_sync_results(products_to_sync, initial_selection_count, disabled_selected_count)
        except Exception as e: _logger.error(f"Wizard Sync Error: {e}", exc_info=True); raise UserError(_("Sync Error: %s") % e)


    def action_confirm_sync_with_images(self):
        """ Executes sync ONLY for enabled products that HAVE an image. """
        # ... (Keep this method as defined previously) ...
        self.ensure_one(); _logger.info("Wizard: Starting sync for enabled products with images."); self._check_prerequisites()
        sync_context = {'manual_sync_trigger': True}; ProductTemplate = self.env['product.template'].with_context(sync_context)
        products_to_sync = self.env['product.template']; initial_selection_count = 0; skipped_count = 0
        base_domain = [('woo_sync_enabled', '=', True), ('image_1920', '!=', False)]
        if self.sync_all_enabled:
            products_to_sync = ProductTemplate.search(base_domain)
            initial_selection_count = len(products_to_sync)
            if not products_to_sync: raise UserError(_("Sync All selected, but no enabled products with images found."))
            _logger.info(f"Wizard: Syncing all {initial_selection_count} enabled products with images.")
        elif self.product_tmpl_ids:
            initial_selection_count = len(self.product_tmpl_ids)
            products_to_sync = self.product_tmpl_ids.filtered_domain(base_domain)
            skipped_count = initial_selection_count - len(products_to_sync)
            if not products_to_sync: raise UserError(_("Selected %d products, but none are enabled with an image.") % initial_selection_count)
            _logger.info(f"Wizard: Syncing {len(products_to_sync)} selected enabled products with images (skipped {skipped_count}).")
        else: raise UserError(_("Select products or check 'Use All Enabled'."))
        try: products_to_sync.sync_to_woocommerce(); return self._process_sync_results(products_to_sync, initial_selection_count, skipped_count)
        except Exception as e: _logger.error(f"Wizard Sync (images) Error: {e}", exc_info=True); raise UserError(_("Sync Error: %s") % e)


    # --- NEW METHOD ---
    def button_enable_sync_for_products_with_images(self):
        """ Finds all products with an image and sets woo_sync_enabled = True """
        self.ensure_one()
        _logger.info("Wizard: Finding products with images to enable sync.")

        # Search for all product templates that have an image set
        # We don't need to filter by woo_sync_enabled here, as we are *setting* it.
        products_with_images = self.env['product.template'].search([
            ('image_1920', '!=', False)
        ])

        count = len(products_with_images)
        if not products_with_images:
            message = _("No products with images found.")
        else:
            try:
                # Update the woo_sync_enabled field for these products
                products_with_images.write({'woo_sync_enabled': True})
                message = _("Successfully enabled 'Sync with WooCommerce' for %d products that have images.") % count
                _logger.info(message)
            except Exception as e:
                 _logger.error(f"Failed to enable sync for products with images: {e}", exc_info=True)
                 raise UserError(_("An error occurred while enabling sync: %s") % e)

        # Return a notification to the user
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Enable Sync Finished'),
                'message': message,
                'sticky': False,
                'type': 'info' if count > 0 else 'warning',
                'next': {'type': 'ir.actions.act_window_close'}, # Close wizard
            }
        }