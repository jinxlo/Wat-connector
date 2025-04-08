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

try:
    import openai
except ImportError:
    _logger.debug("OpenAI library not found. Cannot perform API key validation on save.")
    openai = None

# Define constants for API paths (used in test connection)
WP_API_BASE = "/wp-json/wp/v2"
WC_API_BASE = "/wp-json/wc/v3"

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # --- Activation ---
    woo_sync_active = fields.Boolean(
        string="Activate WooCommerce Sync",
        config_parameter='odoo_woo_sync.sync_active',
        default=False,  # <-- Keeping original default=False
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
        default=False,
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
        help="Synchronize Odoo's main product image with WooCommerce product image (Requires WP Credentials below)."
    )

    # --- WP Auth Fields ---
    wp_admin_username = fields.Char(
        string='WordPress Admin Username',
        config_parameter='odoo_woo_sync.wp_admin_username',
        help="Username of the WP Admin account used to generate the Application Password (for image uploads)."
    )
    wp_app_password = fields.Char(
        string='WordPress Application Password',
        config_parameter='odoo_woo_sync.wp_app_password',
        help="The generated Application Password (paste the password without spaces)."
    )

    # --- OpenAI Configuration ---
    gpt_enrichment_enabled = fields.Boolean(
        string="Enable GPT Product Enrichment",
        config_parameter='odoo_woo_sync.gpt_enrichment_enabled',
        default=False,
        help="If checked, product description, category, and brand suggestion will be fetched from OpenAI GPT during sync."
    )
    openai_api_key = fields.Char(
        string="OpenAI API Key",
        config_parameter='odoo_woo_sync.openai_api_key',
        help="Your OpenAI API key used for generating product data via ChatGPT.",
        groups="base.group_system",
    )
    gpt_model_name = fields.Char(
        string="GPT Model Name",
        config_parameter='odoo_woo_sync.gpt_model_name',
        default='gpt-3.5-turbo',
        # required_if_gpt_enrichment_enabled=True, # Rely on XML 'required' in Odoo 17
        help="Specify the OpenAI model ID to use (e.g., gpt-3.5-turbo, gpt-4, gpt-4o). Ensure your API key has access.",
        groups="base.group_system",
    )

    # +++ FIELD ADDED HERE +++
    woo_gpt_override_fields = fields.Boolean(
        string="Allow GPT to Override Fields",
        config_parameter='odoo_woo_sync.gpt_override_fields',
        default=False, # Default to False (safer)
        help="If checked, descriptions/categories/brands suggested by GPT will overwrite existing/default values during sync.",
        groups="base.group_system", # Often good practice to restrict config changes
    )
    # +++ END OF ADDED FIELD +++


    # --- Test Connection Method ---
    def button_test_woo_connection(self):
        """Tests WC API, optionally WP API, and optionally OpenAI API connection."""
        self.ensure_one()

        # Use the currently entered values in the form for testing
        current_woo_url = self.woo_url
        current_key = self.woo_consumer_key
        current_secret = self.woo_consumer_secret
        current_wp_user = self.wp_admin_username
        current_wp_pass = self.wp_app_password
        current_openai_key = self.openai_api_key
        current_gpt_model = self.gpt_model_name # Get current model name
        image_sync_enabled = self.woo_sync_image
        gpt_enabled = self.gpt_enrichment_enabled
        # current_gpt_override = self.woo_gpt_override_fields # Example if needed in test

        if not self.woo_sync_active:
             raise UserError(_("WooCommerce Sync is currently deactivated in settings. Activate it before testing."))

        # --- Test 1: WooCommerce API Connection ---
        wc_success = False
        wc_message = ""
        if not all([current_woo_url, current_key, current_secret]):
            wc_message = _("WooCommerce API Credentials missing. Skipping test.")
            _logger.warning(wc_message)
        else:
            _logger.info(f"Attempting to test connection to WooCommerce API at: {current_woo_url}")
            base_test_url = current_woo_url
            if not base_test_url.startswith(('http://', 'https://')): base_test_url = 'https://' + base_test_url
            try:
                if not WOO_API: raise ImportError("WooCommerce library not installed.")
                wcapi = WOO_API( url=base_test_url, consumer_key=current_key, consumer_secret=current_secret, wp_api=True, version="wc/v3", timeout=20, query_string_auth=True)
                response_wc = wcapi.get("system_status")
                response_wc.raise_for_status()
                _logger.info("WooCommerce API connection test successful.")
                wc_success = True; wc_message = _('Successfully connected to the WooCommerce API.')
            except ImportError: raise UserError(_("WooCommerce library not found. Run 'pip install woocommerce requests'."))
            except requests.exceptions.HTTPError as e:
                 status_code = e.response.status_code; error_details = e.response.text or str(e)
                 try: error_details = e.response.json().get('message', error_details)
                 except Exception: pass
                 wc_message = _("WooCommerce API Connection Failed! Status: %s, Message: %s") % (status_code, error_details)
                 _logger.error(f"WC API connection test failed. Status: {status_code}, Response: {error_details}", exc_info=False)
            except requests.exceptions.RequestException as e:
                 wc_message = _("WooCommerce API Connection Failed! Network Error: %s") % e
                 _logger.error(f"WC API connection test failed: {e}", exc_info=True)
            except Exception as e:
                 wc_message = _("WooCommerce API Connection Failed! Unexpected Error: %s") % e
                 _logger.error(f"An unexpected error occurred during WC connection test: {e}", exc_info=True)

        # --- Test 2: WordPress Media API Connection (Optional) ---
        wp_success = False
        wp_message = ""
        if image_sync_enabled:
            _logger.info("Image sync enabled, testing WP Media API connection...")
            if not current_wp_user or not current_wp_pass:
                wp_message = _("\nWP Media API: Credentials missing (needed for image upload). Skipping test.")
                _logger.warning("WP Media API connection test skipped: Credentials missing.")
            elif not current_woo_url: wp_message = _("\nWP Media API: Store URL missing. Skipping test.")
            else:
                base_test_url_wp = current_woo_url
                if not base_test_url_wp.startswith(('http://', 'https://')): base_test_url_wp = 'https://' + base_test_url_wp
                try:
                    session_wp = requests.Session(); session_wp.auth = (current_wp_user, current_wp_pass)
                    session_wp.headers.update({'User-Agent': 'Odoo WooCommerce Sync Test'})
                    wp_test_url = f"{base_test_url_wp}{WP_API_BASE}/types/post"
                    response_wp = session_wp.get(wp_test_url, timeout=20); response_wp.raise_for_status()
                    _logger.info("WP Media API connection test successful.")
                    wp_success = True; wp_message = _("\nSuccessfully connected to WP Media API (using App Password).")
                except requests.exceptions.HTTPError as e:
                     status_code = e.response.status_code; error_details = e.response.text or str(e)
                     try: error_details = e.response.json().get('message', error_details)
                     except Exception: pass
                     wp_message = _("\nWP Media API Connection Failed! Status: %s, Message: %s") % (status_code, error_details)
                     _logger.error(f"WP Media API connection test failed. Status: {status_code}, Response: {error_details}", exc_info=False)
                except requests.exceptions.RequestException as e:
                     wp_message = _("\nWP Media API Connection Failed! Network Error: %s") % e
                     _logger.error(f"WP Media API connection test failed: {e}", exc_info=True)
                except Exception as e:
                     wp_message = _("\nWP Media API Connection Failed! Unexpected Error: %s") % e
                     _logger.error(f"An unexpected error occurred during WP connection test: {e}", exc_info=True)
        else: wp_message = _("\n(WP Media API test skipped as image sync is disabled)")

        # --- Test 3: OpenAI API Connection (Optional) ---
        openai_success = False
        openai_message = ""
        if gpt_enabled:
            _logger.info(f"GPT enrichment enabled (Model: {current_gpt_model or 'Not Set'}), testing OpenAI API connection...")
            if not current_openai_key:
                 openai_message = _("\nOpenAI API: Key missing. Skipping test.")
                 _logger.warning(openai_message.strip())
            elif not current_gpt_model: # Check if model name is set
                 openai_message = _("\nOpenAI API: Model Name is missing. Skipping test.")
                 _logger.warning(openai_message.strip())
            elif not openai:
                 openai_message = _("\nOpenAI API: Library not installed (`pip install openai`). Skipping test.")
                 _logger.warning(openai_message.strip())
            else:
                try:
                    # Use consistent OpenAI client instantiation based on library version
                    client = None
                    if hasattr(openai, 'OpenAI'): # v1.x style
                        client = openai.OpenAI(api_key=current_openai_key)
                    elif hasattr(openai, 'api_key'): # v0.x style
                         openai.api_key = current_openai_key
                    else:
                        raise ImportError("Unsupported OpenAI library version structure.")

                    # Perform the model retrieval test
                    if client: # v1.x client object
                        client.models.retrieve(current_gpt_model)
                    else: # v0.x direct call
                        openai.Model.retrieve(current_gpt_model)

                    _logger.info(f"OpenAI API connection test successful for model '{current_gpt_model}'.")
                    openai_success = True
                    openai_message = _("\nSuccessfully connected to OpenAI API (Model: %s).") % current_gpt_model

                except openai.NotFoundError if openai else Exception as e_inner_1: # Catch specific error if model doesn't exist or key lacks access
                     if openai and isinstance(e_inner_1, openai.NotFoundError):
                         openai_message = _("\nOpenAI API Connection Failed! Model '%s' not found or API key lacks permission.") % current_gpt_model
                         _logger.error(f"OpenAI API connection test failed: Model '{current_gpt_model}' not found/accessible.", exc_info=False)
                     else: # Handle case where openai is None or other unexpected error
                         openai_message = _("\nOpenAI API Connection Failed! Unexpected Error during model check: %s") % e_inner_1
                         _logger.error(f"An unexpected error occurred during OpenAI connection test (model check): {e_inner_1}", exc_info=True)
                except openai.AuthenticationError if openai else Exception as e_inner_2:
                     if openai and isinstance(e_inner_2, openai.AuthenticationError):
                         openai_message = _("\nOpenAI API Connection Failed! Authentication Error (Invalid Key?).")
                         _logger.error(f"OpenAI API connection test failed: {e_inner_2}", exc_info=False)
                     else:
                         openai_message = _("\nOpenAI API Connection Failed! Unexpected Error during auth check: %s") % e_inner_2
                         _logger.error(f"An unexpected error occurred during OpenAI connection test (auth check): {e_inner_2}", exc_info=True)
                except openai.APIConnectionError if openai else Exception as e_inner_3:
                    if openai and isinstance(e_inner_3, openai.APIConnectionError):
                         openai_message = _("\nOpenAI API Connection Failed! Network Error: %s") % e_inner_3
                         _logger.error(f"OpenAI API connection test failed: {e_inner_3}", exc_info=True)
                    else:
                         openai_message = _("\nOpenAI API Connection Failed! Unexpected Error during connection check: %s") % e_inner_3
                         _logger.error(f"An unexpected error occurred during OpenAI connection test (connection check): {e_inner_3}", exc_info=True)
                except ImportError as e_import: # Catch if library structure was unexpected
                    openai_message = _("\nOpenAI API Connection Failed! Error initializing library: %s") % e_import
                    _logger.error(f"OpenAI library structure error: {e_import}", exc_info=True)
                except Exception as e: # Generic fallback
                     openai_message = _("\nOpenAI API Connection Failed! Unexpected Error: %s") % e
                     _logger.error(f"An unexpected error occurred during OpenAI connection test: {e}", exc_info=True)
        else:
            openai_message = _("\n(OpenAI API test skipped as GPT enrichment is disabled)")


        # --- Combine Results ---
        final_message = wc_message + wp_message + openai_message
        final_title = _('Connection Test Results')

        all_required_tests_passed = wc_success
        # Only consider WP test result if relevant (image sync enabled AND credentials provided)
        if image_sync_enabled and (current_wp_user or current_wp_pass):
            all_required_tests_passed = all_required_tests_passed and wp_success
        # Only consider OpenAI test result if relevant (GPT enabled AND key/model provided)
        if gpt_enabled and current_openai_key and current_gpt_model:
             all_required_tests_passed = all_required_tests_passed and openai_success

        final_type = 'success' if all_required_tests_passed else 'warning'

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': final_title,
                'message': final_message.strip(),
                'sticky': final_type != 'success', # Make warning messages sticky
                'type': final_type,
            }
        }