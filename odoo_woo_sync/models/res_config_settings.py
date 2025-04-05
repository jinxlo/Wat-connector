# -*- coding: utf-8 -*-
import logging
import requests
from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    # Use the official library name and rename to avoid conflict
    from woocommerce import API as WOO_API
except ImportError:
    _logger.warning("WooCommerce library not found. Please install it: pip install woocommerce requests")
    WOO_API = None

# Define constants for API paths (used in test connection)
WP_API_BASE = "/wp-json/wp/v2"
WC_API_BASE = "/wp-json/wc/v3"

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Activation ---
    woo_sync_active = fields.Boolean(
        string="Activate WooCommerce Sync",
        config_parameter='odoo_woo_sync.sync_active',
        default=False,
        help="Globally enable or disable synchronization features."
    )

    # --- API Credentials ---
    woo_url = fields.Char(
        string='WooCommerce Store URL',
        config_parameter='odoo_woo_sync.woo_url',
        help="Base URL of your WooCommerce store, e.g., https://yourstore.com"
    )
    woo_consumer_key = fields.Char(
        string='WooCommerce Consumer Key',
        config_parameter='odoo_woo_sync.woo_consumer_key',
        help="Your WooCommerce REST API Consumer Key."
    )
    woo_consumer_secret = fields.Char(
        string='WooCommerce Consumer Secret',
        config_parameter='odoo_woo_sync.woo_consumer_secret',
        help="Your WooCommerce REST API Consumer Secret. Ensure keys have Read/Write permissions.",
        # Password=True is handled in the view
    )

    # --- Sync Options ---
    woo_sync_stock = fields.Boolean(
        string="Sync Stock Levels",
        config_parameter='odoo_woo_sync.sync_stock',
        default=True,
        help="Synchronize Odoo's 'Quantity on Hand' with WooCommerce stock quantity."
    )
    woo_sync_price = fields.Boolean(
        string="Sync Price (List Price)",
        config_parameter='odoo_woo_sync.sync_price',
        default=False, # Default to False, as price sync can be complex
        help="Synchronize Odoo's 'Sales Price' with WooCommerce 'Regular Price'."
    )
    woo_sync_description = fields.Boolean(
        string="Sync Description",
        config_parameter='odoo_woo_sync.sync_description',
        default=True,
        help="Synchronize Odoo's 'Sales Description' (or main description) with WooCommerce description."
    )
    woo_sync_image = fields.Boolean(
        string="Sync Main Image",
        config_parameter='odoo_woo_sync.sync_image',
        default=True,
        # Updated help text
        help="Synchronize Odoo's main product image with WooCommerce product image (Requires WP Credentials below)."
    )

    # ========== ADD WP AUTH FIELDS ==========
    wp_admin_username = fields.Char(
        string='WordPress Admin Username',
        config_parameter='odoo_woo_sync.wp_admin_username',
        help="Username of the WP Admin account used to generate the Application Password (for image uploads)."
    )
    wp_app_password = fields.Char(
        string='WordPress Application Password',
        config_parameter='odoo_woo_sync.wp_app_password',
        help="The generated Application Password (paste the password without spaces)."
        # Password masking handled in view
    )
    # ========================================

    # --- Test Connection Method (Modified) ---
    def button_test_woo_connection(self):
        """Tests WC API connection using keys and optionally WP API connection using App Password."""
        self.ensure_one()
        if not WOO_API:
             # Corrected pip install command suggestion
             raise UserError(_("WooCommerce library not found. Please run 'pip install woocommerce requests'."))

        params = self.env['ir.config_parameter'].sudo()
        woo_url = params.get_param('odoo_woo_sync.woo_url')
        woo_consumer_key = params.get_param('odoo_woo_sync.woo_consumer_key')
        woo_consumer_secret = params.get_param('odoo_woo_sync.woo_consumer_secret')
        is_active = params.get_param('odoo_woo_sync.sync_active', 'False') == 'True'

        # Use the currently entered values in the form for testing, not just saved ones
        # This provides immediate feedback before saving.
        current_woo_url = self.woo_url or woo_url
        current_key = self.woo_consumer_key or woo_consumer_key
        current_secret = self.woo_consumer_secret or woo_consumer_secret
        current_wp_user = self.wp_admin_username or params.get_param('odoo_woo_sync.wp_admin_username')
        current_wp_pass = self.wp_app_password or params.get_param('odoo_woo_sync.wp_app_password')
        image_sync_enabled = self.woo_sync_image # Check current form value

        if not self.woo_sync_active: # Check current form value for active state
             raise UserError(_("WooCommerce Sync is currently deactivated in settings. Activate it before testing."))

        if not all([current_woo_url, current_key, current_secret]):
            raise UserError(_("WooCommerce API credentials missing. Please enter URL, Key, and Secret."))

        _logger.info(f"Attempting to test connection to WooCommerce API at: {current_woo_url}")
        base_test_url = current_woo_url
        if not base_test_url.startswith(('http://', 'https://')):
             base_test_url = 'https://' + base_test_url

        # --- Test 1: WooCommerce API Connection ---
        try:
            wcapi = WOO_API(
                url=base_test_url,
                consumer_key=current_key,
                consumer_secret=current_secret,
                wp_api=True,
                version="wc/v3",
                timeout=20,
                query_string_auth=True
            )
            response_wc = wcapi.get("system_status") # Lightweight check
            response_wc.raise_for_status() # Check for HTTP errors
            _logger.info("WooCommerce API connection test successful.")
            wc_success = True
            wc_message = _('Successfully connected to the WooCommerce API.')

        except requests.exceptions.HTTPError as e:
             error_details = "Unknown error"; status_code = e.response.status_code
             try: error_details = e.response.json().get('message', e.response.text)
             except Exception: error_details = e.response.text or str(e)
             _logger.error(f"WC API connection test failed. Status: {status_code}, Response: {error_details}", exc_info=False) # Reduce log noise maybe
             raise UserError(_("WooCommerce API Connection Test Failed!\nStatus Code: %s\nMessage: %s") % (status_code, error_details))
        except requests.exceptions.RequestException as e:
             _logger.error(f"WC API connection test failed: {e}", exc_info=True)
             raise UserError(_("WooCommerce API Connection Test Failed! Network Error: %s") % e)
        except Exception as e:
             _logger.error(f"An unexpected error occurred during WC connection test: {e}", exc_info=True)
             raise UserError(_("WooCommerce API Connection Test Failed! Unexpected Error: %s") % e)

        # --- Test 2: WordPress Media API Connection (Optional) ---
        wp_success = False
        wp_message = ""
        if image_sync_enabled:
            _logger.info("Image sync enabled, testing WP Media API connection...")
            if not current_wp_user or not current_wp_pass:
                wp_message = _("\nWP Media API: Credentials missing (needed for image upload).")
                _logger.warning("WP Media API connection test skipped: Credentials missing.")
            else:
                try:
                    session_wp = requests.Session()
                    session_wp.auth = (current_wp_user, current_wp_pass)
                    session_wp.headers.update({'User-Agent': 'Odoo WooCommerce Sync Test'})
                    wp_test_url = f"{base_test_url}{WP_API_BASE}/types/post" # Check core endpoint
                    response_wp = session_wp.get(wp_test_url, timeout=20)
                    response_wp.raise_for_status()
                    _logger.info("WP Media API connection test successful.")
                    wp_success = True
                    wp_message = _("\nSuccessfully connected to WP Media API (using App Password).")
                except requests.exceptions.HTTPError as e:
                     error_details = "Unknown error"; status_code = e.response.status_code
                     try: error_details = e.response.json().get('message', e.response.text)
                     except Exception: error_details = e.response.text or str(e)
                     _logger.error(f"WP Media API connection test failed. Status: {status_code}, Response: {error_details}", exc_info=False)
                     wp_message = _("\nWP Media API Connection Failed! Status: %s, Message: %s") % (status_code, error_details)
                except requests.exceptions.RequestException as e:
                     _logger.error(f"WP Media API connection test failed: {e}", exc_info=True)
                     wp_message = _("\nWP Media API Connection Failed! Network Error: %s") % e
                except Exception as e:
                     _logger.error(f"An unexpected error occurred during WP connection test: {e}", exc_info=True)
                     wp_message = _("\nWP Media API Connection Failed! Unexpected Error: %s") % e
        else:
            wp_message = _("\n(WP Media API test skipped as image sync is disabled)")

        # --- Combine Results ---
        final_message = wc_message + wp_message
        final_title = _('Connection Test Results')
        # Show success only if both required tests pass
        final_type = 'success' if wc_success and (wp_success or not image_sync_enabled or (not current_wp_user or not current_wp_pass)) else 'warning'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': final_title,
                'message': final_message,
                'sticky': final_type != 'success', # Keep sticky if there was a warning/failure
                'type': final_type,
            }
        }

    # Override set_values or create @api.depends methods if needed for complex logic
    # For simple config_parameter fields, Odoo handles saving automatically.