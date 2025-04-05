# -*- coding: utf-8 -*-
import base64
import logging
import requests
import time
import json
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

    # --- API Connection Helpers ---
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
            wcapi.get("").raise_for_status(); # Test base WC endpoint
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

        # Check if WP credentials are provided (needed for media upload)
        if not wp_user or not wp_pass:
            msg = "WP Admin Username or Application Password not configured (required for image uploads)."
            # Only raise error immediately if triggered manually AND image sync is enabled
            if self.env.context.get('manual_sync_trigger'):
                 sync_image_enabled = params.get_param('odoo_woo_sync.sync_image', 'False') == 'True'
                 if sync_image_enabled:
                      _logger.error(f"WP Session Error: {msg}")
                      raise UserError(_(msg))
                 else:
                     _logger.warning(f"WP Session Skipped: {msg} Image sync is disabled.")
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

            error_msg = _("Failed to create/validate WP authenticated session using App Password. Status: %s, Details: %s Check WP Username/App Password in Odoo settings and ensure the WP user has permissions.") % (status_code, error_details)
            _logger.error(error_msg, exc_info=False) # Log details, maybe not full traceback unless debugging
            if self.env.context.get('manual_sync_trigger'): raise UserError(error_msg)
            return None, None
        except Exception as e: # Catch other exceptions like Timeout, ConnectionError, etc.
            error_msg = _("Failed to create authenticated session for WordPress API using App Password. Error: %s") % e
            _logger.error(error_msg, exc_info=True)
            if self.env.context.get('manual_sync_trigger'): raise UserError(error_msg)
            return None, None

    # --- _upload_image_to_wp ---
    def _upload_image_to_wp(self, image_field_name, record_name, record_id):
        """ Uploads an image from an Odoo record to WP Media Library using WP Session"""
        self.ensure_one()
        image_data_base64 = getattr(self, image_field_name)
        if not image_data_base64:
            _logger.debug(f"No image data found in field '{image_field_name}' for {record_name} {record_id}.")
            return None

        # Decode base64 image data
        try:
            image_data = base64.b64decode(image_data_base64)
        except Exception as e_decode:
            error_msg = f"Failed to decode base64 image data for {record_name} {record_id}: {e_decode}"
            _logger.error(error_msg)
            self.sudo().write({'woo_sync_error': error_msg})
            return None

        session, base_url = self._get_wp_requests_session() # Gets session authenticated with App Password
        if not session:
             params = self.env['ir.config_parameter'].sudo()
             sync_image = params.get_param('odoo_woo_sync.sync_image', 'False') == 'True'
             if sync_image:
                 error_msg = "Cannot upload image: Failed to get authenticated WP session (check WP credentials in settings)."
                 self.sudo().write({'woo_sync_error': error_msg})
                 _logger.error(error_msg)
             # Even if session fails, return None, don't raise error here unless manually triggered and images are mandatory
             return None

        media_url = f"{base_url}{WP_API_BASE}/media"
        image_type = 'image/png' # Assuming PNG, adjust if needed based on Odoo image format
        filename = f"odoo_{record_name}_{record_id}_image.png" # Simple filename

        headers = {
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Content-Type': image_type,
        }

        try:
            _logger.info(f"Uploading image for {record_name} {record_id} to WP Media Library...")
            response = session.post(media_url, headers=headers, data=image_data, timeout=60) # Use decoded data
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            media_data = response.json()
            media_id = media_data.get('id')

            if media_id:
                _logger.info(f"Successfully uploaded image to WP Media Library. Media ID: {media_id}")
                return media_id
            else:
                _logger.error(f"Image uploaded to WP but no Media ID found in response: {media_data}")
                self.sudo().write({'woo_sync_error': "WP image upload successful but Media ID missing."})
                return None

        except requests.exceptions.RequestException as e:
            # --- THIS BLOCK WAS CORRECTED ---
            error_details = str(e) # Default error
            status_code = "N/A"    # Default status code

            if e.response is not None:
                status_code = e.response.status_code
                try:
                    # Attempt to get JSON error first
                    error_details = e.response.json().get('message', e.response.text)
                except Exception:
                    # Fallback to raw text if not JSON or if json() fails
                    error_details = e.response.text or str(e)
            # --- END OF CORRECTION ---

            error_msg = f"WP Media API Error uploading image. Status: {status_code}. Details: {error_details}"
            _logger.error(error_msg, exc_info=False) # Log details, maybe not full traceback unless debugging
            self.sudo().write({'woo_sync_error': error_msg})
            return None
        except Exception as e:
            error_msg = f"Unexpected error uploading image to WP: {e}"
            _logger.error(error_msg, exc_info=True)
            self.sudo().write({'woo_sync_error': error_msg})
            return None

    # --- _prepare_woocommerce_product_data ---
    def _prepare_woocommerce_product_data(self, wcapi):
        """ Prepares WC product data, using image ID from WP upload """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        sync_image = params.get_param('odoo_woo_sync.sync_image', 'True') == 'True'

        product_sku = self.default_code or f'odoo-tmpl-{self.id}'
        vals = {
            'name': self.name or f'Odoo Product {self.id}',
            'sku': product_sku,
            'type': 'variable' if self.product_variant_count > 1 else 'simple',
            'status': 'publish' # Or 'draft'/'pending' if needed
        }

        if params.get_param('odoo_woo_sync.sync_description', 'True') == 'True':
            vals['description'] = self.description_sale or self.description or ''
            # Consider short description too if needed:
            # vals['short_description'] = self.description_picking or ''

        if params.get_param('odoo_woo_sync.sync_price', 'False') == 'True':
            vals['regular_price'] = str(self.list_price)
            # Consider sale price:
            # if self.sale_price_field: vals['sale_price'] = str(self.sale_price_field)

        # Stock for Simple Products (Template level)
        if vals['type'] == 'simple' and params.get_param('odoo_woo_sync.sync_stock', 'True') == 'True':
            simple_variant = self.product_variant_id # The single variant for a simple product
            vals['manage_stock'] = True
            stock_qty = int(simple_variant.qty_available) if simple_variant else 0
            vals['stock_quantity'] = stock_qty
            vals['stock_status'] = 'instock' if stock_qty > 0 else 'outofstock'
            # vals['backorders'] = 'no' / 'notify' / 'yes' # Optional

        # Image Sync (uses WP Media upload)
        if sync_image:
            # Clear previous image-related error before attempting upload again
            if self.woo_sync_error and any(term in self.woo_sync_error for term in ['image', 'WP', 'Media']):
                self.sudo().write({'woo_sync_error': False})

            media_id = self._upload_image_to_wp('image_1920', 'product', self.id) # Use main image field
            if media_id:
                vals['images'] = [{'id': media_id, 'position': 0}]
            else:
                vals['images'] = [] # Send empty list if upload failed or no image
                # Don't necessarily stop the whole sync, but error is logged/set by _upload_image_to_wp

        # Attributes for Variable Products
        variation_attribute_names = [] # Keep track of attributes used for variations
        if vals['type'] == 'variable':
            attributes_data = []
            for line in self.attribute_line_ids:
                attr_options = line.value_ids.mapped('name')
                if attr_options:
                    # Determine if this attribute is for variation based on Odoo config or a flag
                    is_for_variation = True # Default assumption, adjust if Odoo has a specific field
                    attributes_data.append({
                        'name': line.attribute_id.name,
                        'options': attr_options,
                        'visible': True, # Visible on product page
                        'variation': is_for_variation # Used for variations?
                    })
                    if is_for_variation:
                         variation_attribute_names.append(line.attribute_id.name)

            # Sanity check: Variable products should ideally have attributes marked for variation
            if not any(attr['variation'] for attr in attributes_data) and self.product_variant_ids:
                 _logger.warning(f"Variable product '{self.name}' (ID:{self.id}) might be missing attributes explicitly marked for variation in WooCommerce payload.")
                 # You might want to force the first attribute to be for variation or handle this case specifically

            vals['attributes'] = attributes_data

        # Other potential fields: categories, tags, weight, dimensions, etc.
        # Example:
        # if self.categ_id: vals['categories'] = [{'id': woo_category_id_mapped_from_odoo}]
        # if self.weight: vals['weight'] = str(self.weight)

        return vals, variation_attribute_names

    # --- _find_existing_woo_product ---
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
                    # Log other errors but don't clear ID immediately, might be temporary API issue
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
                    # Found one or more products with this SKU
                    found_product = results[0] # Assume first result is the one
                    existing_woo_id = str(found_product['id'])
                    _logger.info(f"Found existing Woo product by SKU '{self.default_code}'. Woo ID: {existing_woo_id} for {product_display}")
                    # Update Odoo record with the found ID if it wasn't stored or was cleared
                    if not self.woo_product_id or self.woo_product_id != existing_woo_id:
                        self.sudo().write({'woo_product_id': existing_woo_id})
                else:
                     _logger.debug(f"No Woo product found with SKU: '{self.default_code}'")

            except requests.exceptions.RequestException as e_sku_req:
                 # Handle specific request errors (like 404 if SKU not found, though API might return empty list)
                 if e_sku_req.response is not None:
                      _logger.error(f"Error checking Woo SKU '{self.default_code}' (Status: {e_sku_req.response.status_code}): {e_sku_req.response.text}")
                 else:
                      _logger.error(f"Network/Request error checking Woo SKU '{self.default_code}': {e_sku_req}")
            except Exception as e_sku:
                _logger.error(f"Unexpected exception occurred while checking Woo SKU '{self.default_code}': {e_sku}", exc_info=False)

        return existing_woo_id

    # --- sync_to_woocommerce ---
    def sync_to_woocommerce(self):
        """ Syncs selected Odoo product templates (and variants) to WooCommerce. """
        # Use context to indicate if manually triggered (affects error raising)
        sync_context = self.env.context.copy()
        sync_context['manual_sync_trigger'] = True # Assume manual if called directly

        # Get API client (checks credentials and active status)
        wcapi = self.with_context(sync_context)._get_woo_api_client()
        if not wcapi:
            # Error handling/logging is done within _get_woo_api_client
            return True # Exit gracefully

        # Filter products that are actually enabled for sync
        products_to_process = self.filtered('woo_sync_enabled')
        if not products_to_process:
            if self.env.context.get('manual_sync_trigger') and len(self) > 0:
                # Only raise error if manually triggered on records where none are enabled
                raise UserError(_("None of the selected products are enabled for WooCommerce sync."))
            _logger.info("Sync: No products in the current set are enabled for sync.")
            return True # Nothing to do

        _logger.info(f"Starting WooCommerce sync for {len(products_to_process)} product template(s)...")
        synced_count = 0
        error_count = 0
        products_with_errors = self.env['product.template']

        for template in products_to_process:
            # Clear previous sync error for this attempt
            template.sudo().write({'woo_sync_error': False})
            template_display = f"'{template.name}' (ID:{template.id}, SKU:{template.default_code or 'N/A'})"
            _logger.info(f"Processing {template_display}...")

            try:
                # Prepare the data payload for WooCommerce
                product_data, variation_attribute_names = template._prepare_woocommerce_product_data(wcapi)

                # Check if image upload failed previously during _prepare_...
                # If so, skip API call for this product to avoid repeated failures if image is critical
                # The error would have been set on the template by _upload_image_to_wp
                if template.woo_sync_error and ('image' in template.woo_sync_error or 'WP' in template.woo_sync_error or 'Media' in template.woo_sync_error):
                    _logger.error(f"Skipping API call for {template_display} due to previous image/WP error: {template.woo_sync_error}")
                    error_count += 1
                    products_with_errors |= template
                    time.sleep(0.1) # Small delay
                    continue # Move to the next product

                # Find if the product exists in Woo
                existing_woo_id = template._find_existing_woo_product(wcapi)
                action = 'update' if existing_woo_id else 'create'
                start_time = time.time()

                # Perform the Create or Update API call
                if existing_woo_id:
                    _logger.debug(f"Updating Woo product ID {existing_woo_id} with data: {product_data}")
                    response = wcapi.put(f"products/{existing_woo_id}", product_data)
                else:
                    _logger.debug(f"Creating new Woo product with data: {product_data}")
                    response = wcapi.post("products", product_data)

                end_time = time.time()
                _logger.debug(f"WooCommerce API '{action}' call for {template_display} took {end_time - start_time:.2f}s. Status: {response.status_code if response else 'N/A'}")

                # Process the response
                if response and response.status_code in [200, 201]: # OK or Created
                    woo_product = response.json()
                    retrieved_woo_id = str(woo_product.get('id'))
                    _logger.info(f"Successfully {action}d product in WooCommerce. Woo ID: {retrieved_woo_id} for {template_display}")

                    vals_to_write = {
                        'woo_last_sync_date': fields.Datetime.now(),
                        'woo_sync_error': False # Explicitly clear error on success
                    }
                    # Update Woo ID if it was created or changed
                    if not template.woo_product_id or template.woo_product_id != retrieved_woo_id:
                        vals_to_write['woo_product_id'] = retrieved_woo_id

                    template.sudo().write(vals_to_write)

                    # Sync variations if it's a variable product and sync was successful
                    if product_data['type'] == 'variable' and retrieved_woo_id:
                         # Pass the confirmed Woo ID and the attributes marked for variation
                         template._sync_woocommerce_variations(wcapi, retrieved_woo_id, variation_attribute_names)
                         # Variation sync errors are handled within that method

                    synced_count += 1 # Count template success

                else:
                    # Handle API errors
                    error_details = "Unknown API error"
                    status_code = "N/A"
                    if response is not None:
                        status_code = response.status_code
                        try:
                            error_json = response.json()
                            # Try to get specific WooCommerce error message
                            error_details = f"Code: {error_json.get('code', 'N/A')}. Message: {error_json.get('message', str(error_json))}"
                        except Exception:
                            # Fallback if response is not JSON
                            error_details = response.text or "No response body"
                    else:
                         error_details = "No response received from API." # E.g. timeout before response

                    error_message = f"WooCommerce API Error syncing {template_display}. Status: {status_code}. Details: {error_details}"
                    _logger.error(error_message)
                    template.sudo().write({'woo_sync_error': error_message})
                    error_count += 1
                    products_with_errors |= template

            except Exception as e:
                # Catch unexpected errors during preparation or API call
                error_msg = f"Unexpected error occurred while syncing {template_display}: {e}"
                _logger.error(error_msg, exc_info=True) # Log full traceback for unexpected errors
                # Set error on the record if not already set by a sub-process
                if not template.woo_sync_error:
                    template.sudo().write({'woo_sync_error': error_msg})
                error_count += 1
                products_with_errors |= template
            finally:
                # Add a small delay to avoid hitting API rate limits
                time.sleep(0.1) # Adjust as needed

        _logger.info(f"WooCommerce sync finished. Processed: {len(products_to_process)}, Synced OK: {synced_count}, Errors: {error_count}")
        if products_with_errors:
            _logger.error(f"Products with sync errors: {products_with_errors.mapped('name')}")
            # Optionally, raise a less severe warning if manually triggered and there were errors
            # if self.env.context.get('manual_sync_trigger'):
            #     raise UserError(_("Sync finished with errors for products: %s") % ', '.join(products_with_errors.mapped('name')))

        return True

    # --- _sync_woocommerce_variations ---
    def _sync_woocommerce_variations(self, wcapi, woo_product_id, variation_attribute_names):
        """ Syncs product variants using batch API, handles image ID """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        sync_image = params.get_param('odoo_woo_sync.sync_image', 'True') == 'True'
        sync_price = params.get_param('odoo_woo_sync.sync_price', 'False') == 'True'
        sync_stock = params.get_param('odoo_woo_sync.sync_stock', 'True') == 'True'

        variants_to_sync = self.product_variant_ids
        if not variants_to_sync:
            _logger.info(f"No Odoo variants found for template '{self.name}' (Woo ID: {woo_product_id}). Skipping variation sync.")
            return

        _logger.info(f"Starting variation sync for Woo Product ID {woo_product_id} ('{self.name}')")

        # 1. Fetch existing variations from WooCommerce for this product
        existing_variations_map = {} # Map SKU -> Woo Variation Data
        try:
            all_variations = []
            page = 1
            while True:
                # Fetch variations in pages
                response = wcapi.get(f"products/{woo_product_id}/variations", params={'per_page': 100, 'page': page})
                response.raise_for_status()
                current_page_variations = response.json()
                if not current_page_variations:
                    break # No more variations on subsequent pages
                all_variations.extend(current_page_variations)
                page += 1

            for var_data in all_variations:
                 if var_data.get('sku'): # Only map if SKU exists in Woo
                     existing_variations_map[var_data['sku']] = var_data

            _logger.info(f"Found {len(existing_variations_map)} existing variations with SKU in WooCommerce for Woo Product ID {woo_product_id}.")

        except Exception as e:
            error_msg = f"Error fetching existing WooCommerce variations for Woo Product ID {woo_product_id}: {e}"
            _logger.error(error_msg, exc_info=True)
            # Set error on template, as this prevents proper variation sync
            self.sudo().write({'woo_sync_error': f"Error fetching Woo variations: {e}"})
            return # Cannot proceed without knowing existing variations

        # 2. Prepare batch data (create, update, delete)
        variation_batch_data = {'create': [], 'update': [], 'delete': []}
        odoo_variant_map_by_sku = {} # Map SKU -> Odoo product.product record
        odoo_variant_skus = set()   # Keep track of SKUs from Odoo variants being processed
        variants_with_prep_errors = self.env['product.product'] # Track variants that failed preparation

        for variant in variants_to_sync:
            # Clear previous variation-specific error
            variant.sudo().write({'woo_variation_sync_error': False})

            # Ensure unique SKU for the variant (mandatory for variations)
            variant_sku = variant.default_code
            if not variant_sku:
                 # Generate a fallback SKU if missing, ensure it's somewhat unique
                 variant_sku = f'odoo-var-{variant.id}'
                 _logger.warning(f"Variant ID {variant.id} for template '{self.name}' is missing an SKU (default_code). Using generated SKU: {variant_sku}")
                 # Consider if you want to enforce SKUs or auto-generate reliably

            if variant_sku in odoo_variant_skus:
                # Critical error: Duplicate SKU within the Odoo variants for this template
                error_msg = f"Duplicate SKU '{variant_sku}' found for Odoo variants of template '{self.name}'. Cannot sync variations reliably."
                _logger.error(error_msg)
                # Set error on both the template and the specific variant causing issues
                self.sudo().write({'woo_sync_error': error_msg})
                variant.sudo().write({'woo_variation_sync_error': error_msg})
                variants_with_prep_errors |= variant
                continue # Skip this variant

            odoo_variant_skus.add(variant_sku)
            odoo_variant_map_by_sku[variant_sku] = variant

            try:
                # Prepare payload for this specific variation
                variation_payload = {'sku': variant_sku}
                attributes_payload = []
                valid_attributes_found = False

                # Map Odoo variant attribute values to Woo attributes
                for ptav in variant.product_template_attribute_value_ids:
                    # Only include attributes that are designated for variation use (from template prep)
                    if ptav.attribute_id.name in variation_attribute_names:
                        attributes_payload.append({
                            'name': ptav.attribute_id.name,
                            'option': ptav.product_attribute_value_id.name # The specific value (e.g., 'Red', 'XL')
                        })
                        valid_attributes_found = True

                if not valid_attributes_found:
                    # This variant doesn't seem to have valid attributes for variation matching
                    error_msg = f"Variant '{variant.display_name}' (SKU: {variant_sku}) has no attribute values matching the template's variation attributes ({variation_attribute_names}). Cannot sync as variation."
                    _logger.error(error_msg)
                    variant.sudo().write({'woo_variation_sync_error': error_msg})
                    variants_with_prep_errors |= variant
                    continue # Skip this variant

                variation_payload['attributes'] = attributes_payload

                # Sync Price, Stock, Image for the variation
                if sync_price:
                    variation_payload['regular_price'] = str(variant.lst_price)
                    # variation_payload['sale_price'] = str(variant.sale_price_field) # If applicable

                if sync_stock:
                    variation_payload['manage_stock'] = True
                    stock_qty = int(variant.qty_available)
                    variation_payload['stock_quantity'] = stock_qty
                    variation_payload['stock_status'] = 'instock' if stock_qty > 0 else 'outofstock'
                    # variation_payload['backorders'] = 'no' / 'notify' / 'yes'

                if sync_image:
                    # Clear previous variation image error
                    if variant.woo_variation_sync_error and any(term in variant.woo_variation_sync_error for term in ['image', 'WP', 'Media']):
                         variant.sudo().write({'woo_variation_sync_error': False})

                    # Upload image (using variant's image field, calls template method)
                    # Pass variant-specific details for filename/logging
                    media_id = variant._upload_image_to_wp('image_1920', 'variant', variant.id)
                    if media_id:
                        variation_payload['image'] = {'id': media_id}
                    # If upload fails, _upload_image_to_wp sets the error on the variant
                    if variant.woo_variation_sync_error:
                        _logger.warning(f"Image upload failed for variant SKU {variant_sku}. Proceeding without image for this variation.")
                        variants_with_prep_errors |= variant # Mark as having issues, even if non-blocking


                # Determine if creating or updating based on SKU map
                existing_woo_variation = existing_variations_map.get(variant_sku)
                if existing_woo_variation:
                    # Update existing variation
                    variation_payload['id'] = existing_woo_variation['id']
                    variation_batch_data['update'].append(variation_payload)
                    # Ensure Odoo has the correct Woo Variation ID stored
                    if str(variant.woo_variation_id) != str(existing_woo_variation['id']):
                        variant.sudo().write({'woo_variation_id': str(existing_woo_variation['id'])})
                else:
                    # Create new variation
                    variation_batch_data['create'].append(variation_payload)
                    # Clear any potentially stale Woo Variation ID
                    if variant.woo_variation_id:
                        variant.sudo().write({'woo_variation_id': False})

            except Exception as e_var_prep:
                error_msg = f"Error preparing variation data for SKU {variant_sku}: {e_var_prep}"
                _logger.error(error_msg, exc_info=True)
                variant.sudo().write({'woo_variation_sync_error': error_msg})
                variants_with_prep_errors |= variant


        # 3. Determine variations to delete (exist in Woo SKU map but not in Odoo SKUs for this sync)
        woo_skus_to_delete = set(existing_variations_map.keys()) - odoo_variant_skus
        for woo_sku_del in woo_skus_to_delete:
            woo_var_id_del = existing_variations_map[woo_sku_del]['id']
            _logger.warning(f"Variation with SKU '{woo_sku_del}' (Woo Variation ID: {woo_var_id_del}) exists in WooCommerce but not found in Odoo variants for template '{self.name}'. Adding to delete batch.")
            variation_batch_data['delete'].append(woo_var_id_del)


        # 4. Execute Batch API call if there's anything to sync
        if any(variation_batch_data.values()): # Check if any create/update/delete lists are non-empty
            try:
                batch_endpoint = f"products/{woo_product_id}/variations/batch"
                _logger.info(f"Executing variation batch sync for Woo Product ID {woo_product_id}: "
                             f"Create: {len(variation_batch_data['create'])}, "
                             f"Update: {len(variation_batch_data['update'])}, "
                             f"Delete: {len(variation_batch_data['delete'])}")

                response = wcapi.post(batch_endpoint, variation_batch_data)
                response.raise_for_status() # Check for HTTP errors on the batch call itself
                result = response.json()
                now_time = fields.Datetime.now()

                _logger.info(f"Variation batch sync completed for Woo Product ID {woo_product_id}.")

                # --- Process Batch Response ---
                # Handle Creates
                for created_var_resp in result.get('create', []):
                    resp_sku = created_var_resp.get('sku')
                    odoo_variant = odoo_variant_map_by_sku.get(resp_sku)
                    if not odoo_variant:
                        _logger.warning(f"Received create response for unknown SKU '{resp_sku}'.")
                        continue

                    if created_var_resp.get('error'):
                        error = created_var_resp['error']
                        error_msg = f"Batch Create Error: {error.get('code')} - {error.get('message')}"
                        _logger.error(f"Failed to create variation for SKU '{resp_sku}'. Error: {error_msg}")
                        odoo_variant.sudo().write({'woo_variation_sync_error': error_msg})
                    elif created_var_resp.get('id'):
                        # Success: Store new Woo Variation ID and update sync date
                        new_woo_var_id = str(created_var_resp['id'])
                        odoo_variant.sudo().write({
                            'woo_variation_id': new_woo_var_id,
                            'woo_variation_last_sync_date': now_time,
                            'woo_variation_sync_error': False # Clear error on success
                        })
                        _logger.info(f"Successfully created variation. Stored Woo Variation ID {new_woo_var_id} for SKU {resp_sku}")
                    else:
                         _logger.error(f"Unexpected create response for SKU '{resp_sku}': {created_var_resp}")
                         odoo_variant.sudo().write({'woo_variation_sync_error': "Unexpected create response from batch API"})

                # Handle Updates
                for updated_var_resp in result.get('update', []):
                    resp_id = updated_var_resp.get('id')
                    resp_sku = updated_var_resp.get('sku') # SKU might be in response too
                    # Find Odoo variant, preferably by stored ID, fallback to SKU map
                    odoo_variant = self.env['product.product'].search([('woo_variation_id', '=', str(resp_id))], limit=1)
                    if not odoo_variant and resp_sku:
                        odoo_variant = odoo_variant_map_by_sku.get(resp_sku)

                    if not odoo_variant:
                         _logger.warning(f"Received update response for unknown Woo Variation ID '{resp_id}' / SKU '{resp_sku}'.")
                         continue

                    if updated_var_resp.get('error'):
                        error = updated_var_resp['error']
                        error_msg = f"Batch Update Error: {error.get('code')} - {error.get('message')}"
                        _logger.error(f"Failed to update variation Woo ID {resp_id} (SKU: {resp_sku}). Error: {error_msg}")
                        odoo_variant.sudo().write({'woo_variation_sync_error': error_msg})
                    elif resp_id:
                        # Success: Update sync date, clear error
                        odoo_variant.sudo().write({
                            'woo_variation_last_sync_date': now_time,
                            'woo_variation_sync_error': False
                        })
                        _logger.info(f"Successfully updated variation Woo ID {resp_id} (SKU: {resp_sku})")
                    else:
                        _logger.error(f"Unexpected update response for Woo ID '{resp_id}' / SKU '{resp_sku}': {updated_var_resp}")
                        odoo_variant.sudo().write({'woo_variation_sync_error': "Unexpected update response from batch API"})

                # Handle Deletes
                for deleted_var_resp in result.get('delete', []):
                    resp_id = deleted_var_resp.get('id')
                    # Find corresponding Odoo variant if needed (e.g., to clear fields), though it should already be gone from odoo_variant_skus
                    # odoo_variant_deleted = self.env['product.product'].search([('woo_variation_id', '=', str(resp_id))], limit=1)

                    if deleted_var_resp.get('error'):
                        error = deleted_var_resp['error']
                        error_msg = f"Batch Delete Error: {error.get('code')} - {error.get('message')}"
                        _logger.error(f"Failed to delete variation Woo ID {resp_id}. Error: {error_msg}")
                        # Cannot set error on Odoo record as it doesn't exist/match anymore
                        # Maybe set a general error on the template?
                        self.sudo().write({'woo_sync_error': f"Failed to delete Woo Variation ID {resp_id}: {error_msg}"})
                    elif resp_id:
                        _logger.info(f"Successfully processed delete request for variation Woo ID {resp_id}")
                        # If found, clear woo_variation_id field on the Odoo variant that *was* deleted in Woo
                        # variant_to_clear = self.env['product.product'].search([('woo_variation_id', '=', str(resp_id))], limit=1)
                        # if variant_to_clear: variant_to_clear.sudo().write({'woo_variation_id': False, 'woo_variation_sync_error': 'Deleted from WooCommerce'})


            except (requests.exceptions.RequestException, UserError, Exception) as e_batch:
                # Catch errors during the batch API call itself or response processing
                error_msg = f"Error during variation batch sync for Woo Product ID {woo_product_id}: {e_batch}"
                _logger.error(error_msg, exc_info=True)
                # Set a general error on the template for batch failures
                self.sudo().write({'woo_sync_error': f"Variation Batch Sync Error: {e_batch}"})
                # Potentially mark all variants involved in the batch as having an error
                for variant in variants_to_sync - variants_with_prep_errors:
                     if not variant.woo_variation_sync_error: # Avoid overwriting specific prep errors
                         variant.sudo().write({'woo_variation_sync_error': "Batch sync API call failed."})
        else:
            _logger.info(f"No variation changes detected to sync via batch for Woo Product ID {woo_product_id}.")


    # --- Cron Job ---
    @api.model
    def _cron_sync_woocommerce(self):
        """ Scheduled action to sync all enabled products. """
        _logger.info("Starting scheduled WooCommerce sync via Cron...")
        # Use a specific context for cron to differentiate from manual triggers
        cron_context = {'manual_sync_trigger': False}
        ProductTemplate = self.with_context(cron_context)

        # Check if sync is active and credentials are valid before searching products
        try:
            wcapi_test = ProductTemplate._get_woo_api_client()
            if wcapi_test is None:
                _logger.info("Scheduled sync skipped (sync not active or API credentials invalid/missing).")
                return
        except Exception as e:
             _logger.error(f"Scheduled sync aborted during API client initialization: {e}", exc_info=True)
             return # Don't proceed if basic connection fails

        # Find all products enabled for sync
        products_to_sync = ProductTemplate.search([('woo_sync_enabled', '=', True)])

        if products_to_sync:
            _logger.info(f"Cron: Found {len(products_to_sync)} product templates enabled for sync.")
            # Process in batches to avoid long transactions and memory issues
            batch_size = 50 # Adjust batch size based on server resources and API limits
            total_batches = (len(products_to_sync) + batch_size - 1) // batch_size

            for i in range(0, len(products_to_sync), batch_size):
                batch = products_to_sync[i:i + batch_size]
                current_batch_num = i // batch_size + 1
                _logger.info(f"Cron: Processing batch {current_batch_num}/{total_batches} ({len(batch)} products)")

                try:
                    # Call the main sync method for the batch
                    batch.sync_to_woocommerce()
                    # Commit transaction after each successful batch
                    self.env.cr.commit()
                    _logger.info(f"Cron: Batch {current_batch_num} processed and committed.")
                except Exception as e:
                    _logger.error(f"Error occurred syncing batch {current_batch_num}: {e}", exc_info=True)
                    # Rollback changes for the failed batch
                    self.env.cr.rollback()
                    _logger.info(f"Cron: Rolled back changes for batch {current_batch_num}.")
                    # Consider adding failed batch product IDs to a list for retry or reporting

                # Optional: Small delay between batches
                time.sleep(1) # Be mindful of total cron execution time

        else:
            _logger.info("Cron: No product templates found enabled for WooCommerce sync.")

        _logger.info("Scheduled WooCommerce sync finished.")


# --- product.product Model ---
# Inherit product.product to add variation-specific fields and helpers
class ProductProduct(models.Model):
    _inherit = 'product.product'

    # --- Fields for Variation Sync Status ---
    # Use distinct field names to avoid confusion with template-level sync
    woo_variation_id = fields.Char(
        string="WooCommerce Variation ID",
        copy=False,
        readonly=True,
        index=True, # Index for searching by Woo ID
        help="The ID of this specific product variation in WooCommerce."
    )
    # Link to template's sync enabled status for visibility on variant form
    woo_sync_enabled = fields.Boolean(
        related='product_tmpl_id.woo_sync_enabled',
        store=False, # No need to store, just for display
        readonly=True,
        string="Sync Enabled (Template)"
    )
    woo_variation_last_sync_date = fields.Datetime(
        string="Variation Last Sync Date",
        copy=False,
        readonly=True,
        help="Timestamp of the last successful synchronization of this variation."
    )
    woo_variation_sync_error = fields.Text(
        string="Variation Sync Error",
        copy=False,
        readonly=True,
        help="Details of the last synchronization error specific to this variation, if any."
    )

    # --- Helper Method for Variant Image Upload ---
    # This allows calling the template's upload logic from the variant context
    def _upload_image_to_wp(self, image_field_name, record_name, record_id):
        """
        Uploads an image for this variant using the template's WP session and logic.
        Relies on the template having the main _upload_image_to_wp method.
        """
        self.ensure_one()
        if self.product_tmpl_id:
             # Get the actual image data from the variant itself
             image_data_base64 = getattr(self, image_field_name, None)
             if not image_data_base64:
                  _logger.debug(f"No image data in field '{image_field_name}' for variant {record_id}.")
                  return None

             # Call the template's upload method, passing the variant's image data
             # Use sudo() just in case variant permissions differ, though template method handles auth
             # Need to pass the raw base64 data to the template method, assuming it handles decoding
             # Correction: Pass the field name, let the template method fetch it via getattr
             return self.product_tmpl_id.sudo()._upload_image_to_wp(image_field_name, record_name, record_id)
             # Alternative: Decode here and pass raw data if template method expects it
             # try:
             #     image_data = base64.b64decode(image_data_base64)
             #     return self.product_tmpl_id.sudo()._call_template_upload(image_data, record_name, record_id) # Requires modified template method
             # except Exception as e_decode:
             #     # Handle decode error here
             #     return None

        _logger.error(f"Cannot upload image for variant {self.id}, missing link to parent product template.")
        # Set error directly on the variant using the specific error field
        self.sudo().write({'woo_variation_sync_error': 'Cannot sync image: Missing parent template link.'})
        return None


    # --- Sync Button Action (Optional: Button on Variant Form) ---
    def action_sync_variant_parent_to_woocommerce(self):
        """
        Button action placed on the product.product (variant) form
        to trigger the synchronization process for its parent template.
        """
        templates_to_sync = self.mapped('product_tmpl_id')
        if not templates_to_sync:
            raise UserError(_("No parent product template found for the selected variant(s)."))

        # Check if the template is actually enabled for sync
        enabled_templates = templates_to_sync.filtered('woo_sync_enabled')
        if not enabled_templates:
             raise UserError(_("WooCommerce sync is not enabled for the parent product template(s) of the selected variant(s). Please enable sync on the template first."))

        _logger.info(f"Manual sync trigger initiated from variant form for template(s): {enabled_templates.mapped('name')}")

        # Call the sync method on the template(s)
        enabled_templates.sync_to_woocommerce() # This will handle templates and their variants

        # Provide user feedback
        message = _('Synchronization started for parent template(s): %s. Check logs or template status for details.') % ', '.join(enabled_templates.mapped('name'))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('WooCommerce Sync Initiated'),
                'message': message,
                'sticky': False, # Notification disappears automatically
                'type': 'info', # 'info', 'warning', 'danger'
            }
        }