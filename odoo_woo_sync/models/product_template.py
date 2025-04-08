# -*- coding: utf-8 -*-
import base64
import logging
import requests
import time
import json # Ensure json is imported
from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

try:
    # Use the official library name and rename to avoid conflict
    from woocommerce import API as WOO_API
except ImportError:
    _logger.warning("WooCommerce library not found. Please install it: pip install woocommerce requests")
    WOO_API = None

# Define constants for API paths
WP_API_BASE = "/wp-json/wp/v2"
WC_API_BASE = "/wp-json/wc/v3"

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    # --- Fields ---
    woo_product_id = fields.Char(
        string="WooCommerce Product ID", copy=False, readonly=True, index=True,
        help="The ID of this product in WooCommerce." )
    woo_sync_enabled = fields.Boolean(
        string="Sync with WooCommerce", default=False, copy=False, index=True,
        help="If checked, this product will be included in synchronization with WooCommerce." )
    woo_last_sync_date = fields.Datetime(
        string="Last WooCommerce Sync", readonly=True, copy=False )
    woo_sync_error = fields.Text(
        string="WooCommerce Sync Error", readonly=True, copy=False,
        help="Details of the last synchronization error, if any." )

    # <<< REMOVED Odoo Brand Field Placeholder >>>

    # <<< --- NEW HELPER: Fetch Live WooCommerce Categories --- >>>
    def _fetch_live_woo_category_data(self, wcapi):
        """
        Fetches all active categories from WooCommerce via API.
        Handles pagination.
        Returns:
            dict: A dictionary mapping category names (lowercase) to their WooCommerce IDs.
                  Returns None on failure.
        """
        if not wcapi: return None

        live_category_data = {}
        page = 1
        _logger.info("Fetching live categories from WooCommerce...")
        while True:
            try:
                response = wcapi.get('products/categories', params={'per_page': 100, 'page': page})
                response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
                current_page_cats = response.json()
                if not current_page_cats:
                    break # No more categories on subsequent pages

                for cat in current_page_cats:
                    cat_id = cat.get('id')
                    cat_name = cat.get('name')
                    if cat_id and cat_name:
                        # Store with lowercase name for case-insensitive lookup later
                        live_category_data[cat_name.lower()] = cat_id

                # Check if the number of results is less than per_page, indicating the last page
                if len(current_page_cats) < 100:
                    break

                page += 1
                time.sleep(0.1) # Small delay between pages to be nice to the API

            except requests.exceptions.RequestException as e:
                error_detail = str(e)
                status_code = "N/A"
                if e.response is not None:
                    status_code = e.response.status_code
                    try: error_detail = e.response.json().get('message', e.response.text)
                    except Exception: error_detail = e.response.text or str(e)
                _logger.error(f"Failed to fetch live WooCommerce categories (page {page}). Status: {status_code}, Error: {error_detail}", exc_info=True)
                return None # Indicate failure
            except Exception as e:
                 _logger.error(f"Unexpected error fetching live WooCommerce categories (page {page}): {e}", exc_info=True)
                 return None # Indicate failure

        _logger.info(f"Successfully fetched {len(live_category_data)} live categories from WooCommerce.")
        return live_category_data
    # <<< --- END NEW HELPER --- >>>


    # --- MODIFIED: GPT Enrichment Method using Service ---
    # <<< Now accepts live_category_names list >>>
    def _get_gpt_enriched_data(self, name, live_category_names=None): # Changed category_list to live_category_names
        """
        Calls the AI helper service to get enriched product data from GPT.
        Passes the list of LIVE WooCommerce category names to constrain GPT.
        """
        try:
            # Ensure the path to your ai_helpers service is correct
            from odoo.addons.odoo_woo_sync.services import ai_helpers
        except ImportError:
            _logger.error("Could not import ai_helpers service. Ensure the module structure and __init__.py files are correct.")
            return None

        params = self.env['ir.config_parameter'].sudo()
        gpt_enabled = params.get_param('odoo_woo_sync.gpt_enrichment_enabled', 'False') == 'True'
        if not gpt_enabled:
            _logger.debug("GPT enrichment is disabled in settings.")
            return None

        api_key = params.get_param('odoo_woo_sync.openai_api_key')
        model = params.get_param('odoo_woo_sync.gpt_model_name', 'gpt-3.5-turbo') # Use configured model

        if not api_key:
             _logger.warning("OpenAI API Key is missing in settings. Cannot perform GPT enrichment.")
             return None

        # Pass the live_category_names list fetched from WooCommerce.
        if live_category_names is None:
            live_category_names = [] # Ensure it's a list even if fetching failed earlier
            _logger.warning("No live category names provided to _get_gpt_enriched_data. GPT will not be constrained.")

        _logger.debug(f"Calling GPT enrichment for '{name}' with {len(live_category_names)} live categories using model '{model}'.")
        # Call the centralized helper function in ai_helpers.py
        return ai_helpers.call_openai_enrichment(name, live_category_names, api_key, model)
        # <<< --- END MODIFICATION --- >>>

    # --- API Connection Helpers (Unchanged) ---
    @api.model
    def _get_woo_api_client(self):
        """ Returns an initialized WooCommerce API client (using WC Keys) """
        if not WOO_API: raise UserError(_("WooCommerce library not found. Run 'pip install woocommerce requests'."))
        params = self.env['ir.config_parameter'].sudo()
        is_active = params.get_param('odoo_woo_sync.sync_active', 'False') == 'True'
        if not is_active: return None
        woo_url = params.get_param('odoo_woo_sync.woo_url')
        key = params.get_param('odoo_woo_sync.woo_consumer_key')
        secret = params.get_param('odoo_woo_sync.woo_consumer_secret')
        if not all([woo_url, key, secret]):
             if self.env.context.get('manual_sync_trigger'): raise UserError(_("WooCommerce API credentials missing in Settings."))
             _logger.error("WooCommerce API credentials missing. Skipping scheduled sync."); return None
        try:
            if not woo_url.startswith(('http://', 'https://')): woo_url = 'https://' + woo_url
            wcapi = WOO_API( url=woo_url, consumer_key=key, consumer_secret=secret, wp_api=True, version="wc/v3", timeout=45, query_string_auth=True )
            # Test connection to a reliable WC endpoint
            _logger.debug(f"Testing WooCommerce API connection to {woo_url}{WC_API_BASE}/data")
            wcapi.get("data").raise_for_status()
            _logger.info("WooCommerce API client initialized successfully.")
            return wcapi
        except Exception as e:
            # Extract more specific error details if possible
            error_detail = str(e)
            status_code = "N/A"
            if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
                status_code = e.response.status_code
                try: error_detail = e.response.json().get('message', e.response.text)
                except Exception: error_detail = e.response.text or str(e)

            error_msg = _("Failed to initialize WooCommerce API client. Status:[%s] Error:[%s]") % (status_code, error_detail)
            _logger.error(error_msg, exc_info=True) # Log full traceback
            if self.env.context.get('manual_sync_trigger'): raise UserError(error_msg)
            return None

    @api.model
    def _get_wp_requests_session(self):
        """ Returns a requests session configured for WP basic auth using App Password """
        params = self.env['ir.config_parameter'].sudo()
        is_active = params.get_param('odoo_woo_sync.sync_active', 'False') == 'True'
        if not is_active:
            _logger.info("WP Session: Sync not active.")
            return None, None # Return None for session and url

        woo_url = params.get_param('odoo_woo_sync.woo_url')
        if not woo_url: # Basic check
             _logger.error("WP Session: WooCommerce Store URL missing.")
             return None, None

        # ========== GET WP USERNAME AND APP PASSWORD FROM SETTINGS ==========
        wp_user = params.get_param('odoo_woo_sync.wp_admin_username')
        wp_pass = params.get_param('odoo_woo_sync.wp_app_password')
        # ====================================================================

        # Check if WP credentials are provided (needed for media upload and now brand taxonomy)
        if not wp_user or not wp_pass:
            # Adjusted message as it's now needed for brands too
            msg = "WP Admin Username or Application Password not configured (required for image uploads and brand sync)."
            # Only raise error immediately if triggered manually AND image sync OR brand sync is implied
            if self.env.context.get('manual_sync_trigger'):
                 sync_image_enabled = params.get_param('odoo_woo_sync.sync_image', 'False') == 'True'
                 gpt_enabled = params.get_param('odoo_woo_sync.gpt_enrichment_enabled', 'False') == 'True'
                 # Raise error if image sync is on OR if GPT is on (as GPT provides brands)
                 if sync_image_enabled or gpt_enabled:
                      _logger.error(f"WP Session Error: {msg}")
                      raise UserError(_(msg))
                 else:
                     _logger.warning(f"WP Session Skipped: {msg} Image sync and GPT enrichment are disabled.")
                     if not woo_url.startswith(('http://', 'https://')): woo_url = 'https://' + woo_url
                     return None, woo_url # Return None session, but valid URL if needed elsewhere
            else: # Cron job or other context
                 _logger.error(f"Scheduled Sync/Other Context: {msg} Skipping WP session creation.")
                 if not woo_url.startswith(('http://', 'https://')): woo_url = 'https://' + woo_url
                 return None, woo_url

        # Proceed with creating authenticated session
        try:
            if not woo_url.startswith(('http://', 'https://')): woo_url = 'https://' + woo_url
            session = requests.Session()
            # ========== USE WP USERNAME AND APP PASSWORD FOR AUTH ==========
            session.auth = (wp_user, wp_pass)
            # ===============================================================
            session.headers.update({'User-Agent': 'Odoo WooCommerce Sync Module'})

            # Test connection using the authenticated session
            test_url = f"{woo_url}{WP_API_BASE}/users/me" # Check if we can get current user info
            _logger.debug(f"Testing WP session authentication against: {test_url}")
            response = session.get(test_url, timeout=20)
            response.raise_for_status() # Raise error for non-2xx status
            _logger.info("WP authenticated session test successful.")
            return session, woo_url # Return session and base URL

        except requests.exceptions.HTTPError as e:
            error_details = "Unknown error"
            status_code = "N/A"
            if e.response is not None:
                status_code = e.response.status_code
                try:
                    # Attempt to get JSON error first
                    error_details = e.response.json().get('message', e.response.text)
                except Exception:
                    # Fallback to raw text if not JSON
                    error_details = e.response.text or str(e)
            else:
                 error_details = str(e) # Use the exception string if no response

            error_msg = _("Failed to create/validate WP authenticated session using App Password. Status: %s, Details: %s Check WP Username/App Password in Odoo settings and ensure the WP user has permissions (e.g., edit_posts, upload_files, manage_terms).") % (status_code, error_details)
            _logger.error(error_msg, exc_info=False) # Log details, maybe not full traceback unless debugging
            if self.env.context.get('manual_sync_trigger'): raise UserError(error_msg)
            return None, None
        except Exception as e: # Catch other exceptions like Timeout, ConnectionError, etc.
            error_msg = _("Failed to create authenticated session for WordPress API using App Password. Error: %s") % e
            _logger.error(error_msg, exc_info=True)
            if self.env.context.get('manual_sync_trigger'): raise UserError(error_msg)
            return None, None

    # --- _upload_image_to_wp (Unchanged) ---
    def _upload_image_to_wp(self, image_field_name, record_name, record_id):
        """ Uploads an image from an Odoo record to WP Media Library using WP Session"""
        self.ensure_one() # Assumes this method is called on a single record (template or variant)

        # Determine the actual record (template or variant) calling this method
        record = self # Default to self (template)
        # If called via variant._upload_image_to_wp, 'self' will be the variant record
        if self._name == 'product.product':
            record = self

        image_data_base64 = getattr(record, image_field_name, None) # Get image from the specific record
        if not image_data_base64:
            _logger.debug(f"No image data found in field '{image_field_name}' for {record_name} {record_id}.")
            return None

        # Decode base64 image data
        try:
            image_data = base64.b64decode(image_data_base64)
        except Exception as e_decode:
            error_msg = f"Failed to decode base64 image data for {record_name} {record_id}: {e_decode}"
            _logger.error(error_msg)
            error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
            record.sudo().write({error_field: error_msg})
            return None

        # Get the WP Session
        session, base_url = self.env['product.template']._get_wp_requests_session() # Always get session via template model

        if not session:
             # Error logging/handling is now done inside _get_wp_requests_session if needed
             # We just need to check if the session was returned
             _logger.warning(f"Cannot upload image for {record_name} {record_id}: Failed to get authenticated WP session.")
             # Set error on the specific record
             error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
             # Avoid overwriting existing specific WP session errors if already set by _get_wp_requests_session
             if not record[error_field]:
                  record.sudo().write({error_field: "Cannot upload image: WP Session failed."})
             return None

        media_url = f"{base_url}{WP_API_BASE}/media"
        image_type = 'image/png' # Assuming PNG, adjust if needed based on Odoo image format
        variant_suffix = f"_var_{record.id}" if record._name == 'product.product' else f"_tmpl_{record.id}"
        filename = f"odoo_{record_name}{variant_suffix}_image.png" # More specific filename

        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': image_type,
        }

        try:
            _logger.info(f"Uploading image for {record_name} {record_id} (Record Type: {record._name}) to WP Media Library...")
            response = session.post(media_url, headers=headers, data=image_data, timeout=60) # Use decoded data
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            media_data = response.json()
            media_id = media_data.get('id')

            if media_id:
                _logger.info(f"Successfully uploaded image to WP Media Library for {record_name} {record_id}. Media ID: {media_id}")
                return media_id
            else:
                error_msg = f"Image uploaded to WP for {record_name} {record_id} but no Media ID found in response: {media_data}"
                _logger.error(error_msg)
                error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
                record.sudo().write({error_field: "WP image upload successful but Media ID missing."})
                return None

        except requests.exceptions.RequestException as e:
            error_details = str(e) # Default error
            status_code = "N/A"    # Default status code

            if e.response is not None:
                status_code = e.response.status_code
                try:
                    error_details = e.response.json().get('message', e.response.text)
                except Exception:
                    error_details = e.response.text or str(e)

            error_msg = f"WP Media API Error uploading image for {record_name} {record_id}. Status: {status_code}. Details: {error_details}"
            _logger.error(error_msg, exc_info=False)
            error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
            record.sudo().write({error_field: error_msg})
            return None
        except Exception as e:
            error_msg = f"Unexpected error uploading image to WP for {record_name} {record_id}: {e}"
            _logger.error(error_msg, exc_info=True)
            error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
            record.sudo().write({error_field: error_msg})
            return None


    # +++ REPLACED FUNCTION +++
    # --- MODIFIED: _get_or_create_woo_brand (for Taxonomy-based Brands using 'product_brand' slug) ---
    def _get_or_create_woo_brand(self, brand_name):
        """
        Fetch or create a WooCommerce brand term using the WP REST API for taxonomies.
        Uses the 'product_brand' taxonomy slug.
        Requires WP Admin Username & App Password to be configured in settings.
        """
        # NOTE: This function now relies on _get_wp_requests_session and no longer needs 'wcapi' passed in.

        # Sanitize brand name
        brand_name = brand_name.strip()
        if not brand_name:
            _logger.warning("Attempted to get/create brand with empty name.")
            return None

        # --- Confirmed Taxonomy Slug ---
        brand_taxonomy_slug = 'product_brand' # Set based on your provided URL
        # --- End Confirmed Slug ---

        # Get the authenticated WP session and base URL
        # We use the template model's method to ensure consistency
        session, base_url = self.env['product.template']._get_wp_requests_session()

        if not session or not base_url:
            _logger.error(f"Cannot get/create brand '{brand_name}': Failed to get authenticated WP session. Check WP Admin Username/App Password in settings.")
            # Set error on the product template record that called this?
            # This function doesn't know which template called it directly.
            # The error will be logged, and the calling function (_prepare...) will log the failure to assign.
            return None

        # Use the WP v2 API endpoint for the specific taxonomy
        # Ensure WP_API_BASE constant is defined correctly (e.g., "/wp-json/wp/v2")
        term_endpoint = f"{base_url}{WP_API_BASE}/{brand_taxonomy_slug}"

        try:
            # 1. Try to fetch brand term by name using the WP API search
            _logger.debug(f"Searching for WP taxonomy term '{brand_name}' in taxonomy '{brand_taxonomy_slug}' via endpoint {term_endpoint}...")
            # WP API uses 'search' parameter for GET requests on taxonomy term endpoints
            response = session.get(term_endpoint, params={'search': brand_name, 'per_page': 5, 'orderby': 'id'}, timeout=20)
            response.raise_for_status() # Raise HTTPError for bad responses (e.g., 5xx, 401, 403, but allow 404 initially)

            results = response.json()
            if results and isinstance(results, list):
                # Find exact match (case-insensitive) as WP search can be broad
                for term in results:
                    if term.get('name', '').lower() == brand_name.lower():
                        term_id = term.get('id')
                        _logger.info(f"Found WP brand term '{term['name']}' (ID: {term_id}) in taxonomy '{brand_taxonomy_slug}' via search.")
                        return term_id # Found it

            # If no exact match found in results list or list was empty
            _logger.info(f"Brand term '{brand_name}' not found via search in taxonomy '{brand_taxonomy_slug}'. Attempting to create.")

            # 2. If not found, create the brand term using the WP API POST request
            create_data = {'name': brand_name} # Data payload for creating a term
            _logger.info(f"Creating WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}' via endpoint {term_endpoint}...")
            response = session.post(term_endpoint, json=create_data, timeout=30) # Use json= data for POST with requests session

            if response.status_code in [200, 201]: # 201 Created is standard, 200 OK sometimes returned
                created_term = response.json()
                term_id = created_term.get('id')
                if term_id:
                    _logger.info(f"Successfully created WP brand term '{created_term['name']}' (ID: {term_id}) in taxonomy '{brand_taxonomy_slug}'.")
                    return term_id
                else:
                    _logger.error(f"Failed to create WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}'. API success status {response.status_code} but response missing ID: {response.text}")
                    return None
            else:
                # Log specific creation errors from WP API
                error_details = response.text
                try:
                    # WP API often returns error details in 'message'
                    error_details = response.json().get('message', response.text)
                except Exception: pass # Keep raw text if not JSON
                _logger.error(f"Error creating WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}' (Status: {response.status_code}): {error_details}")
                # Add hint for common 401/403 errors (permissions)
                if response.status_code in [401, 403]:
                     _logger.error(f" --> Hint: Authentication failed. Check WP Admin Username/App Password and ensure the user has 'manage_terms' capability for the '{brand_taxonomy_slug}' taxonomy.")
                # Check for 'term_exists' code if creating a duplicate (though search should prevent this)
                elif 'term_exists' in error_details:
                     _logger.warning(f" --> Hint: Brand term '{brand_name}' might already exist (possible race condition or search mismatch?).")
                return None

        except requests.exceptions.HTTPError as e:
             # Handle 404 specifically on the GET search - means taxonomy might not exist or WP API path is wrong
             if e.response is not None and e.response.status_code == 404:
                 _logger.error(f"WP API endpoint '{term_endpoint}' for taxonomy '{brand_taxonomy_slug}' returned 404. Cannot search/create brand term '{brand_name}'. Ensure the taxonomy slug is correct and the brand plugin/feature is active.")
             else: # Log other HTTP errors (5xx, 401, 403 on GET, etc.)
                 status_code = "N/A"; error_details = str(e)
                 if e.response is not None:
                     status_code=e.response.status_code; error_details = e.response.text
                     try: error_details = e.response.json().get('message', error_details)
                     except Exception: pass
                 _logger.error(f"HTTP error during GET/POST for WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}' (Status: {status_code}): {error_details}", exc_info=False)

        except requests.exceptions.RequestException as e_req:
             # Network errors (DNS, connection refused, timeout during request)
             _logger.error(f"Network/Request error getting/creating WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}': {e_req}", exc_info=False)
        except Exception as e:
            # Any other unexpected errors (JSON decoding, etc.)
            _logger.error(f"Unexpected error getting/creating WP brand term '{brand_name}' in taxonomy '{brand_taxonomy_slug}': {e}", exc_info=True)

        return None # Return None if any exception occurs or creation fails
    # +++ END OF REPLACED FUNCTION +++


    # +++ MODIFIED FUNCTION +++
    # --- MODIFIED: _prepare_woocommerce_product_data ---
    def _prepare_woocommerce_product_data(self, wcapi, enriched_data=None, live_category_data=None):
        """
        Prepares WC product data, using image ID from WP upload.
        Optionally incorporates enriched data for description, category (using live lookup),
        and brand (using taxonomy lookup).
        Args:
            wcapi: Initialized WooCommerce API client (still needed for product create/update).
            enriched_data (dict): Data returned from GPT (description, category name, brand name).
            live_category_data (dict): Dictionary mapping live Woo category names (lowercase) to IDs.
        """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        sync_image = params.get_param('odoo_woo_sync.sync_image', 'True') == 'True'
        # Renamed for clarity & consistency
        sync_description_setting = params.get_param('odoo_woo_sync.sync_description', 'True') == 'True'
        sync_price = params.get_param('odoo_woo_sync.sync_price', 'False') == 'True'
        sync_stock = params.get_param('odoo_woo_sync.sync_stock', 'True') == 'True'
        # Renamed for clarity & consistency
        gpt_override_enabled_setting = params.get_param('odoo_woo_sync.gpt_override_fields', 'False') == 'True'

        product_sku = self.default_code or f'odoo-tmpl-{self.id}'
        product_name = self.name.strip() if self.name else ''
        final_name = product_name or f'Odoo Product {self.id}'

        vals = {
            'name': final_name,
            'sku': product_sku,
            'type': 'variable' if self.product_variant_count > 1 else 'simple',
            'status': 'publish',
            'categories': [], # Initialize categories list
            'attributes': [], # Initialize attributes list (brands will NOT go here)
            # --- CORRECTED: Initialize brand taxonomy key ---
            'product_brand': [], # Use the correct slug 'product_brand'
            # --- END CORRECTED ---
        }

        # --- Description Handling ---
        # Set default description first (if sync is enabled)
        if sync_description_setting:
            vals['description'] = self.description_sale or self.description or ''
            _logger.debug(f"[{final_name}] Default description sync enabled. Initial description set (or empty).")
        else:
            _logger.debug(f"[{final_name}] Default description sync disabled.")

        # --- Price/Stock/Image Handling ---
        if sync_price:
             if vals['type'] == 'simple':
                 vals['regular_price'] = str(self.list_price)
        if vals['type'] == 'simple' and sync_stock:
            simple_variant = self.product_variant_id
            if simple_variant:
                vals['manage_stock'] = True
                stock_qty = int(simple_variant.qty_available)
                vals['stock_quantity'] = stock_qty
                vals['stock_status'] = 'instock' if stock_qty > 0 else 'outofstock'
            else:
                 _logger.warning(f"Cannot sync stock for simple product '{self.name}' (ID:{self.id}) as its single variant could not be found.")
                 vals['manage_stock'] = False
        if sync_image:
            # Clear previous image errors before attempting upload
            if self.woo_sync_error and any(term in self.woo_sync_error for term in ['image', 'WP', 'Media']):
                self.sudo().write({'woo_sync_error': False})
            media_id = self._upload_image_to_wp('image_1920', 'product_template', self.id)
            if media_id:
                vals['images'] = [{'id': media_id, 'position': 0}]
            # else: # If upload fails or no image, don't send 'images' key
            #    pass

        # --- Attribute Handling (excluding brand) ---
        variation_attribute_names = []
        if vals['type'] == 'variable':
            attributes_data = []
            for line in self.attribute_line_ids:
                # Optional: Add check if you also store brand as an Odoo attribute internally
                # if line.attribute_id.name.lower() == 'brand': continue

                attr_options = line.value_ids.mapped('name')
                if attr_options:
                    # Mark as variation attribute (assuming all Odoo attributes are for variations)
                    # You might need more specific logic if some are just informational
                    is_for_variation = True
                    attributes_data.append({
                        'name': line.attribute_id.name,
                        'options': attr_options,
                        'visible': True, # Generally true for variation attributes
                        'variation': is_for_variation
                    })
                    if is_for_variation:
                         variation_attribute_names.append(line.attribute_id.name)

            # Only assign attributes if there are any non-brand ones
            if attributes_data:
                vals['attributes'] = attributes_data
            # else: # If no attributes, don't send 'attributes' key
            #    vals.pop('attributes', None) # Already handled by cleanup at end


        # --- Process Enriched Data ---
        if enriched_data:
            # --- Apply GPT Description (WITH LOGGING) ---
            gpt_desc = enriched_data.get('description')
            # Debug logging for override condition
            _logger.debug(f"[{final_name}] Checking GPT description override condition:")
            _logger.debug(f"  - gpt_override_enabled_setting: {gpt_override_enabled_setting} (Type: {type(gpt_override_enabled_setting)})")
            _logger.debug(f"  - sync_description_setting: {sync_description_setting} (Type: {type(sync_description_setting)})")
            _logger.debug(f"  - gpt_desc: '{gpt_desc}' (Type: {type(gpt_desc)}, Is Truthy: {bool(gpt_desc)})")
            if gpt_override_enabled_setting and sync_description_setting and gpt_desc:
                 _logger.info(f"[{final_name}] Applying GPT-enriched description (Override Enabled).")
                 vals['description'] = gpt_desc # Overwrite default description
            else:
                 _logger.info(f"[{final_name}] Condition for applying GPT description not met. Keeping default description (if any).")

            # --- Apply GPT Category (using live lookup) ---
            gpt_cat_name = enriched_data.get("category")
            if gpt_cat_name and isinstance(gpt_cat_name, str) and gpt_cat_name.strip():
                clean_cat_name = gpt_cat_name.strip()
                if live_category_data:
                    found_woo_cat_id = live_category_data.get(clean_cat_name.lower())
                    if found_woo_cat_id:
                        # Assign using the standard 'categories' key
                        vals['categories'] = [{'id': int(found_woo_cat_id)}]
                        _logger.info(f"[{final_name}] Mapped GPT category '{clean_cat_name}' to live Woo Category ID {found_woo_cat_id}.")
                    else:
                        _logger.warning(f"[{final_name}] GPT-suggested category '{clean_cat_name}' not found in live WooCommerce categories. Cannot set category.")
                else:
                    _logger.warning(f"[{final_name}] Cannot map GPT category '{clean_cat_name}' because live category data was not available.")
            elif gpt_cat_name is not None:
                 _logger.info(f"[{final_name}] GPT returned category '{gpt_cat_name}', but it's null or empty. Skipping category assignment.")


            # --- Apply GPT Brand (MODIFIED for TAXONOMY) ---
            gpt_brand_name = enriched_data.get('brand')
            if gpt_brand_name and isinstance(gpt_brand_name, str) and gpt_brand_name.strip() and gpt_brand_name.strip().lower() != 'unknown':
                clean_brand_name = gpt_brand_name.strip()
                _logger.info(f"[{final_name}] GPT provided brand '{clean_brand_name}'. Attempting to get/create in Woo Brand Taxonomy.")

                # Call the MODIFIED brand function (which now uses WP API)
                # No longer pass wcapi
                brand_term_id = self._get_or_create_woo_brand(clean_brand_name)

                if brand_term_id:
                    # --- Assign brand using TAXONOMY SLUG 'product_brand' as the key ---
                    brand_taxonomy_slug = 'product_brand' # Define slug for clarity
                    vals[brand_taxonomy_slug] = [{'id': brand_term_id}] # Assign list with dict containing id
                    # --- END ASSIGNMENT ---
                    _logger.info(f"[{final_name}] Assigned brand '{clean_brand_name}' (Term ID: {brand_term_id}) to Woo Payload using taxonomy key '{brand_taxonomy_slug}'.")
                else:
                    # Error already logged in _get_or_create_woo_brand
                    _logger.warning(f"[{final_name}] Could not get or create brand term '{clean_brand_name}' in WooCommerce Taxonomy. Brand will not be assigned.")
            elif gpt_brand_name is not None:
                 _logger.info(f"[{final_name}] GPT returned brand '{gpt_brand_name}', but it's null, empty, or 'unknown'. Skipping brand assignment.")
            # --- END Apply GPT Brand ---
        else:
            _logger.debug(f"[{final_name}] No enriched_data provided to _prepare_woocommerce_product_data.")

        # --- Clean up empty list keys before returning ---
        # Remove keys if their corresponding list value is empty, as WC API prefers this
        if not vals.get('categories'): vals.pop('categories', None)
        if not vals.get('product_brand'): vals.pop('product_brand', None) # Use correct slug
        if not vals.get('attributes'): vals.pop('attributes', None)
        if not vals.get('images'): vals.pop('images', None)

        return vals, variation_attribute_names
    # +++ END OF MODIFIED FUNCTION +++


    # --- _find_existing_woo_product (Unchanged) ---
    def _find_existing_woo_product(self, wcapi):
        """ Finds existing Woo product by ID or SKU """
        self.ensure_one()
        existing_woo_id = None
        product_display = f"'{self.name}' (ID: {self.id}, SKU: {self.default_code or 'N/A'})"

        # 1. Check by stored WooCommerce ID
        if self.woo_product_id:
            try:
                _logger.debug(f"Checking for existing Woo product by stored ID: {self.woo_product_id} for {product_display}")
                response = wcapi.get(f"products/{self.woo_product_id}")
                if response.status_code == 200:
                    existing_woo_id = self.woo_product_id
                    _logger.debug(f"Confirmed Woo product exists with ID: {existing_woo_id}")
                elif response.status_code == 404:
                    _logger.warning(f"Stored WooCommerce ID {self.woo_product_id} for {product_display} not found (404). Clearing stored ID.")
                    self.sudo().write({'woo_product_id': False}) # Clear invalid ID
                else:
                     _logger.error(f"Error checking Woo ID {self.woo_product_id} (Status: {response.status_code}): {response.text}")
            except Exception as e_get:
                _logger.error(f"Exception occurred while checking Woo ID {self.woo_product_id}: {e_get}", exc_info=False)

        # 2. If not found by ID, check by SKU (if SKU exists in Odoo)
        if not existing_woo_id and self.default_code:
            try:
                _logger.debug(f"Checking for existing Woo product by SKU: '{self.default_code}' for {product_display}")
                response = wcapi.get("products", params={'sku': self.default_code})
                response.raise_for_status() # Check for HTTP errors
                results = response.json()
                if results and isinstance(results, list):
                    found_product = results[0] # Assume first result is the one
                    existing_woo_id = str(found_product['id'])
                    _logger.info(f"Found existing Woo product by SKU '{self.default_code}'. Woo ID: {existing_woo_id} for {product_display}")
                    if not self.woo_product_id or self.woo_product_id != existing_woo_id:
                        self.sudo().write({'woo_product_id': existing_woo_id})
                else:
                     _logger.debug(f"No Woo product found with SKU: '{self.default_code}'")

            except requests.exceptions.RequestException as e_sku_req:
                 if e_sku_req.response is not None:
                      _logger.error(f"Error checking Woo SKU '{self.default_code}' (Status: {e_sku_req.response.status_code}): {e_sku_req.response.text}")
                 else:
                      _logger.error(f"Network/Request error checking Woo SKU '{self.default_code}': {e_sku_req}")
            except Exception as e_sku:
                _logger.error(f"Unexpected exception occurred while checking Woo SKU '{self.default_code}': {e_sku}", exc_info=False)

        return existing_woo_id

    # --- sync_to_woocommerce (Added logging for final payload - DEBUG ONLY) ---
    def sync_to_woocommerce(self):
        """
        Syncs selected Odoo product templates (and variants) to WooCommerce.
        Fetches live categories, calls GPT, prepares payload using live data, and sends to Woo.
        """
        sync_context = self.env.context.copy()
        if 'manual_sync_trigger' not in sync_context:
            sync_context['manual_sync_trigger'] = True # Assume manual unless called by cron with context set

        wcapi = self.with_context(sync_context)._get_woo_api_client()
        if not wcapi:
            _logger.error("Sync Aborted: Could not get WooCommerce API client.")
            # Error handled/raised in _get_woo_api_client for manual trigger
            return False # Indicate failure

        products_to_process = self.filtered('woo_sync_enabled')
        if not products_to_process:
            if self.env.context.get('manual_sync_trigger') and len(self) > 0:
                 raise UserError(_("None of the products in this batch/selection are enabled for WooCommerce sync."))
            _logger.info("Sync: No products in the current set are enabled for sync.")
            return True # No products to sync is considered success

        _logger.info(f"Starting WooCommerce sync for {len(products_to_process)} product template(s)...")

        # --- Fetch Live Data ONCE per run (if not passed via context) ---
        live_category_data = sync_context.get('live_category_data')
        live_category_names_for_gpt = sync_context.get('live_category_names_for_gpt')

        if live_category_data is None:
            _logger.info("Fetching live WooCommerce category data for this sync run...")
            live_category_data = self._fetch_live_woo_category_data(wcapi)
            if live_category_data is None:
                _logger.warning("Proceeding with sync, but category enrichment/assignment will likely fail due to inability to fetch live categories.")
                live_category_data = {} # Use empty dict to avoid errors later
                live_category_names_for_gpt = []
            else:
                live_category_names_for_gpt = list(live_category_data.keys()) # Keys are already lowercase
        else:
             _logger.info("Using pre-fetched live category data provided via context.")

        # --- Sync Loop ---
        synced_count = 0
        error_count = 0
        products_with_errors = self.env['product.template']

        for template in products_to_process:
            template.sudo().write({'woo_sync_error': False}) # Clear previous error
            template_display = f"'{template.name}' (ID:{template.id}, SKU:{template.default_code or 'N/A'})"
            _logger.info(f"Processing {template_display}...")

            try:
                # --- GPT Enrichment Call ---
                enriched_data = template._get_gpt_enriched_data(template.name, live_category_names=live_category_names_for_gpt)

                # --- Prepare Woo Payload Call ---
                product_data, variation_attribute_names = template._prepare_woocommerce_product_data(
                    wcapi, # Pass wcapi here, still needed for the main product sync
                    enriched_data=enriched_data,
                    live_category_data=live_category_data
                )

                # --- Basic Payload Validation ---
                if not product_data.get('name'):
                     _logger.error(f"Product data preparation failed for {template_display}: Missing product name.")
                     template.sudo().write({'woo_sync_error': "Failed to prepare product data: Name is missing."})
                     error_count += 1; products_with_errors |= template; time.sleep(0.1); continue

                # Check for blocking WP session errors if image/brand sync failed during prep
                # Note: _get_wp_requests_session handles raising UserError on manual trigger if creds missing
                if template.woo_sync_error and "WP Session" in template.woo_sync_error:
                     _logger.error(f"Skipping API call for {template_display} due to WP session error during preparation: {template.woo_sync_error}")
                     error_count += 1; products_with_errors |= template; time.sleep(0.1); continue

                # --- Find Existing Product & Determine Action ---
                existing_woo_id = template._find_existing_woo_product(wcapi)
                action = 'update' if existing_woo_id else 'create'
                start_time = time.time()

                # --- Make API Call ---
                # >>> DEBUG LOGGING: Uncomment the line below to see the EXACT payload sent <<<
                # _logger.debug(f"{action.capitalize()} Payload for {template_display}: {json.dumps(product_data, indent=2)}")
                # >>> END DEBUG LOGGING <<<

                if existing_woo_id:
                    _logger.debug(f"Updating Woo product ID {existing_woo_id} for {template_display}. Payload keys: {list(product_data.keys())}")
                    response = wcapi.put(f"products/{existing_woo_id}", product_data)
                else:
                    _logger.debug(f"Creating new Woo product for {template_display}. Payload keys: {list(product_data.keys())}")
                    response = wcapi.post("products", product_data)

                end_time = time.time()
                _logger.debug(f"WooCommerce API '{action}' call for {template_display} took {end_time - start_time:.2f}s. Status: {response.status_code if response else 'N/A'}")

                # --- Process API Response ---
                if response and response.status_code in [200, 201]:
                    woo_product = response.json()
                    retrieved_woo_id = str(woo_product.get('id'))
                    if not retrieved_woo_id:
                         _logger.error(f"WooCommerce API success status ({response.status_code}) but no product ID returned for {template_display}. Response: {woo_product}")
                         template.sudo().write({'woo_sync_error': f"Sync successful (Status {response.status_code}) but no Woo ID returned."})
                         error_count += 1; products_with_errors |= template; continue # Treat as error

                    _logger.info(f"Successfully {action}d product in WooCommerce. Woo ID: {retrieved_woo_id} for {template_display}")

                    vals_to_write = { 'woo_last_sync_date': fields.Datetime.now(), 'woo_sync_error': False }
                    if not template.woo_product_id or template.woo_product_id != retrieved_woo_id:
                        vals_to_write['woo_product_id'] = retrieved_woo_id
                    template.sudo().write(vals_to_write)

                    # --- Sync Variations (if applicable) ---
                    if product_data.get('type') == 'variable' and retrieved_woo_id: # Check key exists
                         variation_success = template._sync_woocommerce_variations(wcapi, retrieved_woo_id, variation_attribute_names)
                         if not variation_success:
                              _logger.warning(f"Variation sync encountered errors for template {template_display}. Check variant records for details.")
                              # Optionally mark template with a general variation error
                              if not template.woo_sync_error: # Don't overwrite other errors
                                    template.sudo().write({'woo_sync_error': 'Variation sync completed with errors.'})
                              # Don't increment error_count here, variation errors are on variants

                    synced_count += 1
                else:
                    # Handle API errors
                    error_details = "Unknown API error"; status_code = "N/A"
                    if response is not None:
                        status_code = response.status_code
                        try:
                            error_json = response.json()
                            error_details = f"Code: {error_json.get('code', 'N/A')}. Message: {error_json.get('message', str(error_json))}"
                            # Add hints for common issues
                            if 'product_invalid_sku' in error_json.get('code', ''):
                                error_details += f" (Hint: SKU '{product_data.get('sku')}' might already exist in Woo for another product?)"
                            elif any(err_code in error_json.get('code', '') for err_code in ['product_invalid_attribute', 'product_invalid_term', 'woocommerce_rest_term_invalid']):
                                error_details += " (Hint: Attribute/Term assignment issue? Check Brand/Category/Attribute slugs and existence in Woo.)"
                            elif 'taxonomy' in error_json.get('message',''):
                                 error_details += " (Hint: Problem with category or brand assignment?)"
                        except Exception: error_details = response.text or "No response body"
                    else: error_details = "No response received from API."

                    error_message = f"WooCommerce API Error syncing {template_display}. Action: {action}, Status: {status_code}. Details: {error_details}"
                    _logger.error(error_message)
                    template.sudo().write({'woo_sync_error': error_message})
                    error_count += 1; products_with_errors |= template

            except Exception as e:
                # Catch unexpected errors during the loop for a single product
                error_msg = f"Unexpected error occurred while syncing {template_display}: {e}"
                _logger.error(error_msg, exc_info=True)
                if not template.woo_sync_error: # Avoid overwriting specific API errors
                    template.sudo().write({'woo_sync_error': error_msg})
                error_count += 1; products_with_errors |= template
            finally:
                # Optional small delay between processing each product in the loop
                time.sleep(0.1)

        _logger.info(f"WooCommerce sync finished for this batch/selection. Processed: {len(products_to_process)}, Synced OK: {synced_count}, Errors: {error_count}")
        if products_with_errors:
            _logger.error(f"Products with sync errors in this run: {products_with_errors.mapped('name')} (IDs: {products_with_errors.ids})")
            # No UserError raised here for background/cron jobs, handled by batch methods if needed

        return error_count == 0 # Return True if no errors, False otherwise


    # --- _sync_woocommerce_variations (Unchanged) ---
    def _sync_woocommerce_variations(self, wcapi, woo_product_id, variation_attribute_names):
        """ Syncs product variants using batch API, handles image ID. Returns True on success, False if errors occurred."""
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        sync_image = params.get_param('odoo_woo_sync.sync_image', 'True') == 'True'
        sync_price = params.get_param('odoo_woo_sync.sync_price', 'False') == 'True'
        sync_stock = params.get_param('odoo_woo_sync.sync_stock', 'True') == 'True'
        overall_success = True # Track if any errors occur

        variants_to_sync = self.product_variant_ids
        if not variants_to_sync:
            _logger.info(f"No Odoo variants found for template '{self.name}' (Woo ID: {woo_product_id}). Skipping variation sync.")
            return True # No variants is not an error condition

        _logger.info(f"Starting variation sync for Woo Product ID {woo_product_id} ('{self.name}')")

        # 1. Fetch existing variations from WooCommerce for this product
        existing_variations_map = {} # Map SKU -> Woo Variation Data
        try:
            all_variations = []
            page = 1
            while True:
                response = wcapi.get(f"products/{woo_product_id}/variations", params={'per_page': 100, 'page': page})
                response.raise_for_status()
                current_page_variations = response.json()
                if not current_page_variations:
                    break
                all_variations.extend(current_page_variations)
                if len(current_page_variations) < 100:
                     break
                page += 1
                time.sleep(0.1)

            for var_data in all_variations:
                 if var_data.get('sku'):
                     existing_variations_map[var_data['sku']] = var_data
            _logger.info(f"Found {len(existing_variations_map)} existing variations with SKU in WooCommerce for Woo Product ID {woo_product_id}.")

        except Exception as e:
            error_msg = f"Error fetching existing WooCommerce variations for Woo Product ID {woo_product_id}: {e}"
            _logger.error(error_msg, exc_info=True)
            self.sudo().write({'woo_sync_error': f"Error fetching Woo variations: {e}"})
            return False

        # 2. Prepare batch data (create, update, delete)
        variation_batch_data = {'create': [], 'update': [], 'delete': []}
        odoo_variant_map_by_sku = {}
        odoo_variant_skus = set()
        variants_with_prep_errors = self.env['product.product']

        for variant in variants_to_sync:
            variant.sudo().write({'woo_variation_sync_error': False})
            variant_prep_success = True

            variant_sku = variant.default_code
            if not variant_sku:
                 variant_sku = f'odoo-var-{variant.id}'
                 _logger.warning(f"Variant ID {variant.id} for template '{self.name}' is missing an SKU. Using generated SKU: {variant_sku}")

            if variant_sku in odoo_variant_skus:
                error_msg = f"Duplicate SKU '{variant_sku}' found for Odoo variants of template '{self.name}'. Cannot sync variations reliably."
                _logger.error(error_msg)
                self.sudo().write({'woo_sync_error': error_msg})
                variant.sudo().write({'woo_variation_sync_error': error_msg})
                variants_with_prep_errors |= variant
                overall_success = False
                continue

            odoo_variant_skus.add(variant_sku)
            odoo_variant_map_by_sku[variant_sku] = variant

            try:
                variation_payload = {'sku': variant_sku}
                attributes_payload = []
                valid_attributes_found = False

                for ptav in variant.product_template_attribute_value_ids:
                    attribute_name = ptav.attribute_id.name
                    attribute_value_name = ptav.product_attribute_value_id.name
                    if attribute_name in variation_attribute_names:
                        attributes_payload.append({'name': attribute_name, 'option': attribute_value_name})
                        valid_attributes_found = True

                if not valid_attributes_found and variation_attribute_names:
                    error_msg = f"Variant '{variant.display_name}' (SKU: {variant_sku}) has no attribute values matching template's variation attributes ({variation_attribute_names}). Cannot sync as variation."
                    _logger.error(error_msg)
                    variant.sudo().write({'woo_variation_sync_error': error_msg})
                    variants_with_prep_errors |= variant
                    variant_prep_success = False
                    overall_success = False
                    continue

                variation_payload['attributes'] = attributes_payload

                if sync_price: variation_payload['regular_price'] = str(variant.lst_price)
                if sync_stock:
                    variation_payload['manage_stock'] = True
                    stock_qty = int(variant.qty_available)
                    variation_payload['stock_quantity'] = stock_qty
                    variation_payload['stock_status'] = 'instock' if stock_qty > 0 else 'outofstock'

                if sync_image:
                    if variant.woo_variation_sync_error and any(term in variant.woo_variation_sync_error for term in ['image', 'WP', 'Media']):
                         variant.sudo().write({'woo_variation_sync_error': False})
                    media_id = variant._upload_image_to_wp('image_1920', 'variant', variant.id)
                    if media_id: variation_payload['image'] = {'id': media_id}
                    elif variant.woo_variation_sync_error:
                        _logger.warning(f"Image upload failed for variant SKU {variant_sku}. Proceeding without image.")
                        variants_with_prep_errors |= variant

                if variant_prep_success:
                    existing_woo_variation = existing_variations_map.get(variant_sku)
                    if existing_woo_variation:
                        variation_payload['id'] = existing_woo_variation['id']
                        variation_batch_data['update'].append(variation_payload)
                        if str(variant.woo_variation_id) != str(existing_woo_variation['id']):
                            variant.sudo().write({'woo_variation_id': str(existing_woo_variation['id'])})
                    else:
                        variation_batch_data['create'].append(variation_payload)
                        if variant.woo_variation_id:
                            variant.sudo().write({'woo_variation_id': False})

            except Exception as e_var_prep:
                error_msg = f"Error preparing variation data for SKU {variant_sku}: {e_var_prep}"
                _logger.error(error_msg, exc_info=True)
                variant.sudo().write({'woo_variation_sync_error': error_msg})
                variants_with_prep_errors |= variant
                overall_success = False

        # 3. Determine variations to delete
        woo_skus_to_delete = set(existing_variations_map.keys()) - odoo_variant_skus
        for woo_sku_del in woo_skus_to_delete:
            woo_var_id_del = existing_variations_map[woo_sku_del]['id']
            _logger.warning(f"Variation SKU '{woo_sku_del}' (Woo ID: {woo_var_id_del}) exists in Woo but not Odoo. Adding to delete batch.")
            variation_batch_data['delete'].append(woo_var_id_del)

        # 4. Execute Batch API call
        if any(variation_batch_data.values()):
            try:
                batch_endpoint = f"products/{woo_product_id}/variations/batch"
                _logger.info(f"Executing variation batch sync for Woo Product ID {woo_product_id}: Create: {len(variation_batch_data['create'])}, Update: {len(variation_batch_data['update'])}, Delete: {len(variation_batch_data['delete'])}")
                response = wcapi.post(batch_endpoint, variation_batch_data)
                response.raise_for_status()
                result = response.json()
                now_time = fields.Datetime.now()
                _logger.info(f"Variation batch sync completed for Woo Product ID {woo_product_id}.")

                # Process Batch Response (Create, Update, Delete) - Condensed error handling
                for op_type, responses in result.items():
                    if op_type not in ['create', 'update', 'delete']: continue
                    for resp_item in responses:
                        item_id = resp_item.get('id')
                        item_sku = resp_item.get('sku')
                        error = resp_item.get('error')
                        log_prefix = f"Batch {op_type.capitalize()}"
                        odoo_variant = None
                        if op_type != 'delete':
                            odoo_variant = odoo_variant_map_by_sku.get(item_sku)
                            if not odoo_variant and op_type == 'update': # Find by ID for update if SKU missing
                                 odoo_variant = self.env['product.product'].search([('woo_variation_id', '=', str(item_id))], limit=1)
                        elif op_type == 'delete': # Log deletion target
                            log_prefix += f" for Woo ID {item_id}"

                        if error:
                            error_msg = f"{log_prefix} Error: {error.get('code')} - {error.get('message')}"
                            _logger.error(f"Failed variation {op_type} for SKU '{item_sku or 'N/A'}' / Woo ID '{item_id}'. Error: {error_msg}")
                            if odoo_variant: odoo_variant.sudo().write({'woo_variation_sync_error': error_msg})
                            overall_success = False
                        elif item_id:
                            _logger.info(f"Successfully processed variation {op_type} for SKU '{item_sku or 'N/A'}' / Woo ID {item_id}.")
                            if odoo_variant and op_type != 'delete':
                                vals = {'woo_variation_last_sync_date': now_time, 'woo_variation_sync_error': False}
                                if op_type == 'create': vals['woo_variation_id'] = str(item_id)
                                odoo_variant.sudo().write(vals)
                            elif op_type == 'delete': # Clear ID on Odoo variant if found
                                variant_to_clear = self.env['product.product'].search([('product_tmpl_id', '=', self.id), ('woo_variation_id', '=', str(item_id))], limit=1)
                                if variant_to_clear: variant_to_clear.sudo().write({'woo_variation_id': False, 'woo_variation_sync_error': 'Deleted from Woo.'})
                        else: # Unexpected response
                             _logger.error(f"Unexpected {op_type} response for SKU '{item_sku or 'N/A'}' / Woo ID '{item_id}': {resp_item}")
                             if odoo_variant: odoo_variant.sudo().write({'woo_variation_sync_error': f"Unexpected {op_type} batch response"})
                             overall_success = False


            except (requests.exceptions.RequestException, UserError, Exception) as e_batch:
                error_msg = f"Error during variation batch sync API call for Woo Product ID {woo_product_id}: {e_batch}"
                _logger.error(error_msg, exc_info=True)
                self.sudo().write({'woo_sync_error': f"Variation Batch Sync Error: {e_batch}"})
                for variant in variants_to_sync - variants_with_prep_errors:
                     if not variant.woo_variation_sync_error:
                         variant.sudo().write({'woo_variation_sync_error': "Batch sync API call failed."})
                overall_success = False
        else:
            _logger.info(f"No variation changes detected to sync via batch for Woo Product ID {woo_product_id}.")

        return overall_success


    # --- Cron Job (Unchanged - Relies on underlying methods) ---
    @api.model
    def _cron_sync_woocommerce(self):
        """ Scheduled action to sync all enabled products. """
        _logger.info("Starting scheduled WooCommerce sync via Cron...")
        cron_context = {'manual_sync_trigger': False} # Identify as non-manual trigger
        ProductTemplate = self.with_context(cron_context)

        # --- Check API Connection Early ---
        try:
            wcapi_test = ProductTemplate._get_woo_api_client()
            if wcapi_test is None:
                _logger.info("Scheduled sync skipped (sync not active or API client setup failed).")
                return
        except Exception as e:
             _logger.error(f"Scheduled sync aborted during API client initialization: {e}", exc_info=True)
             return

        # --- Fetch Live Categories ONCE for the entire cron run ---
        _logger.info("CRON: Fetching live WooCommerce categories...")
        live_category_data = ProductTemplate._fetch_live_woo_category_data(wcapi_test) # Use the tested client
        if live_category_data is None:
            _logger.error("CRON: Aborting sync run. Failed to fetch live WooCommerce categories at the start.")
            # Consider creating a notification/activity for admin
            return
        live_category_names_for_gpt = list(live_category_data.keys()) # Keys are already lowercase
        _logger.info(f"CRON: Fetched {len(live_category_data)} live categories.")
        # --- End Fetch ---

        products_to_sync = ProductTemplate.search([('woo_sync_enabled', '=', True)])

        if products_to_sync:
            _logger.info(f"Cron: Found {len(products_to_sync)} product templates enabled for sync.")
            # Get batch size from system parameter or default
            batch_size_str = self.env['ir.config_parameter'].sudo().get_param('odoo_woo_sync.cron_batch_size', '50')
            try: batch_size = int(batch_size_str)
            except ValueError: batch_size = 50
            if batch_size <= 0: batch_size = 50 # Ensure positive batch size
            total_batches = (len(products_to_sync) + batch_size - 1) // batch_size

            for i in range(0, len(products_to_sync), batch_size):
                batch = products_to_sync[i:i + batch_size]
                current_batch_num = i // batch_size + 1
                _logger.info(f"Cron: Processing batch {current_batch_num}/{total_batches} ({len(batch)} products)")

                try:
                    # --- Pass live category info to the batch sync call via context ---
                    batch_context = cron_context.copy()
                    batch_context['live_category_data'] = live_category_data
                    batch_context['live_category_names_for_gpt'] = live_category_names_for_gpt

                    # Call sync_to_woocommerce on the batch with the context
                    batch_success = batch.with_context(batch_context).sync_to_woocommerce()

                    if batch_success:
                         self.env.cr.commit() # Commit after each successful batch
                         _logger.info(f"Cron: Batch {current_batch_num} processed and committed successfully.")
                    else:
                         _logger.warning(f"Cron: Batch {current_batch_num} processed with errors. Committing partial success/errors.")
                         self.env.cr.commit() # Commit even if there were errors in the batch
                except Exception as e:
                    _logger.error(f"Cron: Critical Error occurred processing batch {current_batch_num}. Error: {e}", exc_info=True)
                    self.env.cr.rollback() # Rollback the transaction for the failed batch
                    _logger.info(f"Cron: Rolled back changes for batch {current_batch_num} due to critical error.")
                    # Consider adding a delay or stopping the cron after a critical error

                # Optional delay between batches
                time.sleep(1)
        else:
            _logger.info("Cron: No product templates found enabled for WooCommerce sync.")

        _logger.info("Scheduled WooCommerce sync finished.")

    # --- Cron for GPT Enrichment Preview (Unchanged - Relies on underlying methods) ---
    @api.model
    def _cron_gpt_enrichment_preview(self):
        """
        Cron job that runs GPT enrichment on products enabled for sync,
        using live categories, but does NOT push to WooCommerce or save changes.
        Logs the potential enrichment data.
        """
        _logger.info("Running GPT enrichment preview cron (using live categories, no Woo sync/save)...")
        preview_context = {'manual_sync_trigger': False}
        ProductTemplate = self.env['product.template'].with_context(preview_context)

        # --- Need API client to fetch categories ---
        try:
            wcapi = ProductTemplate._get_woo_api_client()
            if wcapi is None:
                _logger.error("GPT Preview Cron: Cannot run, sync disabled or API client setup failed.")
                return
        except Exception as e:
             _logger.error(f"GPT Preview Cron: Aborted during API client initialization: {e}", exc_info=True)
             return
        # --- End API client fetch ---

        # --- Fetch Live Categories for the preview run ---
        live_category_data = ProductTemplate._fetch_live_woo_category_data(wcapi)
        if live_category_data is None:
            _logger.warning("GPT Preview Cron: Failed to fetch live WooCommerce categories. Preview will run without category constraints.")
            live_category_names_for_gpt = []
        else:
            live_category_names_for_gpt = list(live_category_data.keys()) # Already lowercase
        # --- End Fetch ---

        products_to_enrich = ProductTemplate.search([('woo_sync_enabled', '=', True)])

        if not products_to_enrich:
            _logger.info("GPT Preview Cron: No products found with Woo sync enabled.")
            return

        _logger.info(f"GPT Preview Cron: Found {len(products_to_enrich)} products to preview enrichment for.")
        enriched_count = 0; error_count = 0; skipped_count = 0

        for product in products_to_enrich:
            product_display = f"'{product.name}' (ID:{product.id})"
            try:
                # Call GPT enrichment directly (does not modify the record)
                # Pass live category names
                enriched_data = product._get_gpt_enriched_data(
                    product.name,
                    live_category_names=live_category_names_for_gpt
                )

                if enriched_data:
                    # Log the structured preview data
                    _logger.info(f"GPT Enrichment Preview for {product_display}:\n{json.dumps(enriched_data, indent=2)}")
                    enriched_count += 1
                else:
                    # Log if GPT was disabled or returned nothing
                    _logger.info(f"No enrichment data returned for {product_display}. (GPT disabled or no suggestions)")
                    skipped_count +=1

            except Exception as e:
                _logger.error(f"Error during GPT enrichment preview for {product_display}: {e}", exc_info=True)
                error_count += 1
            finally:
                # Add a small delay to avoid hitting API rate limits if applicable
                time.sleep(0.2) # Adjust as needed

        _logger.info(f"GPT Enrichment Preview Cron finished. Checked: {len(products_to_enrich)}, Enriched Previews Logged: {enriched_count}, Skipped/No Data: {skipped_count}, Errors: {error_count}")

    # --- Batching Method (Unchanged - Relies on underlying methods) ---
    def sync_to_woocommerce_in_batches(self, batch_size=30):
        """
        Action method to sync enabled product templates within 'self' (current selection/recordset)
        to WooCommerce in small batches. Fetches live categories once and uses them for the entire run.
        Intended for manual triggering (e.g., from Action menu).
        """
        batch_context = self.env.context.copy()
        batch_context['manual_sync_trigger'] = True # Ensure context indicates manual trigger

        enabled_products = self.filtered('woo_sync_enabled')
        if not enabled_products:
            if len(self) > 0: raise UserError(_("None of the selected products are enabled for WooCommerce sync."))
            else: _logger.info("Batch Sync Action: No enabled products found in selection."); return True # No action needed

        # --- Get API client early & Fetch Live Categories ONCE ---
        try:
            wcapi = self.with_context(batch_context)._get_woo_api_client() # Will raise UserError if fails on manual trigger
            if wcapi is None: return False # Should not happen if UserError is raised, but defensive check

            _logger.info("Batch Sync Action: Fetching live categories for the run...")
            live_category_data = self._fetch_live_woo_category_data(wcapi)
            if live_category_data is None:
                # Fail loudly for manual action if categories can't be fetched
                raise UserError(_("Failed to fetch live WooCommerce categories. Cannot proceed with sync. Check connection and API logs."))

            live_category_names_for_gpt = list(live_category_data.keys()) # Already lowercase
        except (UserError, Exception) as e:
             # Catch connection errors or UserErrors raised during client/category fetch
             _logger.error(f"Batch Sync Action: Aborted during initialization: {e}", exc_info=True)
             # Re-raise UserError to show to the user
             raise UserError(_("Failed to initialize sync: %s") % e)

        # --- Process in Batches ---
        total_products = len(enabled_products)
        effective_batch_size = batch_size
        if effective_batch_size <= 0: effective_batch_size = 30 # Default if invalid size given
        total_batches = (total_products + effective_batch_size - 1) // effective_batch_size
        _logger.info(f"Batch Sync Action: Starting sync for {total_products} selected/enabled products in {total_batches} batches (size: {effective_batch_size})")

        all_batches_successful = True
        overall_error_messages = []

        for i in range(0, total_products, effective_batch_size):
            batch = enabled_products[i:i + effective_batch_size]
            current_batch_num = (i // effective_batch_size) + 1
            _logger.info(f"Batch Sync Action: Processing batch {current_batch_num}/{total_batches} (Products: {len(batch)})")

            try:
                # Pass live category info via context to the underlying sync method
                current_batch_context = batch_context.copy()
                current_batch_context['live_category_data'] = live_category_data
                current_batch_context['live_category_names_for_gpt'] = live_category_names_for_gpt

                batch_success = batch.with_context(current_batch_context).sync_to_woocommerce()

                if batch_success:
                    self.env.cr.commit() # Commit after each successful batch
                    _logger.info(f"Batch {current_batch_num} sync completed successfully and committed.")
                else:
                     _logger.warning(f"Batch {current_batch_num} sync completed with errors. Committing state.")
                     self.env.cr.commit() # Commit state even with errors
                     all_batches_successful = False
                     # Collect errors from the batch
                     batch_errors = batch.filtered(lambda p: p.woo_sync_error).mapped(lambda p: f"{p.name}: {p.woo_sync_error}")
                     overall_error_messages.extend(batch_errors)

            except Exception as e:
                # Catch critical errors during a batch's execution
                self.env.cr.rollback() # Rollback the failed batch
                _logger.error(f"Batch Sync Action: Critical error processing batch {current_batch_num}. Rolled back. Error: {e}", exc_info=True)
                all_batches_successful = False
                error_msg = f"Critical error in batch {current_batch_num}: {e}"
                overall_error_messages.append(error_msg)
                # Ask user whether to continue or stop? For now, continue.
                _logger.info(f"Batch Sync Action: Continuing to next batch despite critical error in batch {current_batch_num}.")

            # Optional delay between batches for manual action as well
            time.sleep(0.5) # Shorter delay might be okay for manual actions

        _logger.info(f"Batch Sync Action finished processing all {total_batches} batches.")

        # --- Provide Feedback to User ---
        if all_batches_successful:
            message = _('WooCommerce sync completed successfully for %d products.') % total_products
            notif_type = 'success'
            title = _('Sync Completed')
        else:
            message = _('WooCommerce sync finished with errors for %d products. Check logs and product sync error fields for details.\n\nSummary:\n%s') % (total_products, '\n'.join(overall_error_messages[:10])) # Show first 10 errors
            notif_type = 'warning'
            title = _('Sync Finished with Errors')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'sticky': not all_batches_successful, # Keep error messages sticky
                'type': notif_type
            }
        }


# --- product.product Model (Unchanged) ---
class ProductProduct(models.Model):
    _inherit = 'product.product'

    # --- Fields for Variation Sync Status ---
    woo_variation_id = fields.Char( string="WooCommerce Variation ID", copy=False, readonly=True, index=True, help="The ID of this specific product variation in WooCommerce.")
    woo_sync_enabled = fields.Boolean( related='product_tmpl_id.woo_sync_enabled', store=False, readonly=True, string="Sync Enabled (Template)" )
    woo_variation_last_sync_date = fields.Datetime( string="Variation Last Sync Date", copy=False, readonly=True, help="Timestamp of the last successful synchronization of this variation." )
    woo_variation_sync_error = fields.Text( string="Variation Sync Error", copy=False, readonly=True, help="Details of the last synchronization error specific to this variation, if any." )

    # --- Helper Method for Variant Image Upload (Relies on Template Method) ---
    def _upload_image_to_wp(self, image_field_name, record_name, record_id):
        """ Delegates image upload to the product template's method. """
        self.ensure_one()
        if not self.product_tmpl_id:
             error_msg = 'Cannot sync variant image: Missing parent product template link.'
             _logger.error(f"{error_msg} for variant {self.id} (SKU: {self.default_code or 'N/A'})")
             self.sudo().write({'woo_variation_sync_error': error_msg})
             return None

        # Check if the template method exists before calling
        if hasattr(self.product_tmpl_id, '_upload_image_to_wp') and callable(getattr(self.product_tmpl_id, '_upload_image_to_wp')):
             return self.product_tmpl_id._upload_image_to_wp(image_field_name, 'variant', self.id)
        else:
             error_msg = 'Cannot sync variant image: Parent template is missing the required _upload_image_to_wp method.'
             _logger.error(f"{error_msg} for variant {self.id} (SKU: {self.default_code or 'N/A'})")
             self.sudo().write({'woo_variation_sync_error': error_msg})
             return None

    # --- Sync Button Action (Calls Template Batch Method) ---
    def action_sync_variant_parent_to_woocommerce(self):
        """
        Action button on the product.product (variant) form.
        Triggers the batch sync action on the parent template(s) of the selected variants.
        """
        templates_to_sync = self.mapped('product_tmpl_id')
        if not templates_to_sync:
            raise UserError(_("No parent product template found for the selected variant(s)."))

        enabled_templates = templates_to_sync.filtered('woo_sync_enabled')
        if not enabled_templates:
            disabled_names = templates_to_sync.mapped('name')
            raise UserError(_("WooCommerce sync is not enabled for the parent template(s) of the selected variant(s): %s") % ', '.join(disabled_names))

        _logger.info(f"Manual sync trigger from variant form for template(s): {enabled_templates.mapped('name')}. Using template's batch sync method.")
        # Call the batch sync action method on the parent template(s)
        return enabled_templates.sync_to_woocommerce_in_batches(batch_size=30) # Use a reasonable default batch size