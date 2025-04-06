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
        self.ensure_one() # Assumes this method is called on a single record (template or variant)

        # Determine the actual record (template or variant) calling this method
        record = self # Default to self (template)
        # If called via variant._upload_image_to_wp, 'self' will be the variant record
        if self._name == 'product.product':
            record = self
            # Note: The template method 'product_template._upload_image_to_wp' is still used
            # but it operates on data fetched from the 'record' (the variant).

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
            # Use sudo() and specific error fields if available (variant uses woo_variation_sync_error)
            error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
            record.sudo().write({error_field: error_msg})
            return None

        # Get the WP Session (using template's method, assumes template context exists if called from variant)
        # If called from variant, self.product_tmpl_id._get_wp_requests_session() is implicitly used
        # Corrected logic: call _get_wp_requests_session on the template model
        session, base_url = self.env['product.template']._get_wp_requests_session() # Always get session via template model

        if not session:
             params = self.env['ir.config_parameter'].sudo()
             sync_image = params.get_param('odoo_woo_sync.sync_image', 'False') == 'True'
             if sync_image:
                 error_msg = "Cannot upload image: Failed to get authenticated WP session (check WP credentials in settings)."
                 error_field = 'woo_variation_sync_error' if record._name == 'product.product' else 'woo_sync_error'
                 record.sudo().write({error_field: error_msg})
                 _logger.error(f"{error_msg} for {record_name} {record_id}")
             return None

        media_url = f"{base_url}{WP_API_BASE}/media"
        image_type = 'image/png' # Assuming PNG, adjust if needed based on Odoo image format
        # Make filename more specific for variants
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

    # --- _prepare_woocommerce_product_data ---
    def _prepare_woocommerce_product_data(self, wcapi):
        """ Prepares WC product data, using image ID from WP upload """
        self.ensure_one()
        params = self.env['ir.config_parameter'].sudo()
        sync_image = params.get_param('odoo_woo_sync.sync_image', 'True') == 'True'

        product_sku = self.default_code or f'odoo-tmpl-{self.id}'

        # --- MODIFICATION START: Robust Name Handling ---
        # Original line:
        # 'name': self.name or f'Odoo Product {self.id}',
        # New robust handling:
        product_name = self.name.strip() if self.name else '' # Strip whitespace if name exists, else empty string
        final_name = product_name or f'Odoo Product {self.id}' # Use stripped name or fallback if empty/falsy
        # --- MODIFICATION END ---

        vals = {
            'name': final_name, # Use the robustly determined name
            'sku': product_sku,
            'type': 'variable' if self.product_variant_count > 1 else 'simple',
            'status': 'publish' # Or 'draft'/'pending' if needed
        }

        if params.get_param('odoo_woo_sync.sync_description', 'True') == 'True':
            vals['description'] = self.description_sale or self.description or ''
            # Consider short description too if needed:
            # vals['short_description'] = self.description_picking or ''

        if params.get_param('odoo_woo_sync.sync_price', 'False') == 'True':
             # For simple products, price comes from template. For variable, it comes from variants.
             if vals['type'] == 'simple':
                 vals['regular_price'] = str(self.list_price)
            # Consider sale price:
            # if self.sale_price_field: vals['sale_price'] = str(self.sale_price_field)

        # Stock for Simple Products (Template level) - Use the single variant's stock
        if vals['type'] == 'simple' and params.get_param('odoo_woo_sync.sync_stock', 'True') == 'True':
            simple_variant = self.product_variant_id # The single variant for a simple product template
            if simple_variant: # Ensure the variant exists
                vals['manage_stock'] = True
                stock_qty = int(simple_variant.qty_available)
                vals['stock_quantity'] = stock_qty
                vals['stock_status'] = 'instock' if stock_qty > 0 else 'outofstock'
                # vals['backorders'] = 'no' / 'notify' / 'yes' # Optional
            else:
                 _logger.warning(f"Cannot sync stock for simple product '{self.name}' (ID:{self.id}) as its single variant could not be found.")
                 vals['manage_stock'] = False # Cannot manage stock without variant reference


        # Image Sync for Template (uses WP Media upload)
        if sync_image:
            # Clear previous image-related error before attempting upload again
            if self.woo_sync_error and any(term in self.woo_sync_error for term in ['image', 'WP', 'Media']):
                self.sudo().write({'woo_sync_error': False})

            # Call the _upload_image_to_wp method, passing template details
            media_id = self._upload_image_to_wp('image_1920', 'product_template', self.id) # Use main image field
            if media_id:
                vals['images'] = [{'id': media_id, 'position': 0}]
            else:
                vals['images'] = [] # Send empty list if upload failed or no image
                # Error is logged/set on the template by _upload_image_to_wp

        # Attributes for Variable Products
        variation_attribute_names = [] # Keep track of attributes used for variations
        if vals['type'] == 'variable':
            attributes_data = []
            for line in self.attribute_line_ids:
                attr_options = line.value_ids.mapped('name')
                if attr_options:
                    # Determine if this attribute is for variation based on Odoo config or a flag
                    # Assuming all attributes on a variable product template are intended for variation for now
                    is_for_variation = True # Default assumption, adjust if Odoo has a specific field like `create_variant='always'` etc.
                    attributes_data.append({
                        'name': line.attribute_id.name,        # Use attribute's technical/internal name if it differs
                        'options': attr_options,
                        'visible': True,                       # Visible on product page
                        'variation': is_for_variation          # Used for variations?
                    })
                    if is_for_variation:
                         variation_attribute_names.append(line.attribute_id.name) # Store the name used in Woo

            # Sanity check: Variable products should ideally have attributes marked for variation
            if not any(attr['variation'] for attr in attributes_data) and self.product_variant_ids:
                 _logger.warning(f"Variable product '{self.name}' (ID:{self.id}) has attributes defined, but none were marked for 'variation: true' in the WooCommerce payload. This might prevent variations from working correctly in Woo.")
                 # Consider forcing the first attribute or handling this based on specific business logic
                 # If attributes_data is not empty, maybe mark the first one?
                 # if attributes_data:
                 #    attributes_data[0]['variation'] = True
                 #    variation_attribute_names.append(attributes_data[0]['name'])
                 #    _logger.warning(f" --> Forcing first attribute '{attributes_data[0]['name']}' to be used for variations.")

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
        """
        Syncs selected Odoo product templates (and variants) to WooCommerce.
        This method processes the products contained within the 'self' recordset.
        It's designed to be called either directly or by the batching method.
        """
        # If called directly (not via batch method), set manual trigger context
        sync_context = self.env.context.copy()
        if 'manual_sync_trigger' not in sync_context:
            sync_context['manual_sync_trigger'] = True # Assume manual if context not set

        # Get API client (checks credentials and active status within the method)
        # Use with_context to pass the manual_sync_trigger status
        wcapi = self.with_context(sync_context)._get_woo_api_client()
        if not wcapi:
            # Error handling/logging is done within _get_woo_api_client
            # If called manually, error is raised there. If via cron/batch, it logs and returns None.
            _logger.error("Sync Aborted: Could not get WooCommerce API client.")
            # Should we raise here? Depends. If called by batch, raising stops the whole batch job.
            # Let's rely on _get_woo_api_client to raise UserError if manual.
            return False # Indicate failure to the calling batch method if needed

        # Filter products in the current recordset ('self') that are actually enabled for sync
        # Note: The batch method *should* have pre-filtered, but double-checking is safe.
        products_to_process = self.filtered('woo_sync_enabled')
        if not products_to_process:
            # Check context before raising: only show error if manually triggered on records where none are enabled
            if self.env.context.get('manual_sync_trigger') and len(self) > 0:
                 raise UserError(_("None of the products in this batch/selection are enabled for WooCommerce sync."))
            _logger.info("Sync: No products in the current set are enabled for sync.")
            return True # Nothing to do in this batch/selection

        _logger.info(f"Starting WooCommerce sync for {len(products_to_process)} product template(s) in this batch/selection...")
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

                # --- MODIFICATION START: Add Logging for Missing Name ---
                # Check if the 'name' field is missing or empty BEFORE the API call
                if not product_data.get('name'):
                    _logger.error(
                        f"Product '{template.display_name}' (ID: {template.id}) is missing a name "
                        f"before sending to WooCommerce! Prepared Payload: "
                        f"{json.dumps(product_data, indent=2)}"
                    )
                    # Depending on desired behavior, you might want to:
                    # 1. Skip this product:
                    #    error_message = "Cannot sync product: Name is missing or invalid."
                    #    template.sudo().write({'woo_sync_error': error_message})
                    #    error_count += 1
                    #    products_with_errors |= template
                    #    time.sleep(0.1)
                    #    continue # Skip to next product
                    # 2. Raise an error (might stop batch process):
                    #    raise ValidationError(_("Cannot sync product '%s' (ID: %s) to WooCommerce: Name is missing or invalid.") % (template.display_name, template.id))
                    # 3. Allow the API call to proceed (current behavior - it will likely fail)
                    pass # Logged the error, let the API call proceed and fail if name is truly the issue
                # --- MODIFICATION END ---

                # Check if image upload failed previously during _prepare_...
                # The error would have been set on the template by _upload_image_to_wp
                if template.woo_sync_error and any(term in template.woo_sync_error for term in ['image', 'WP', 'Media']):
                    _logger.error(f"Skipping API call for {template_display} due to previous image/WP error: {template.woo_sync_error}")
                    error_count += 1
                    products_with_errors |= template
                    time.sleep(0.1) # Small delay
                    continue # Move to the next product in this batch

                # Find if the product exists in Woo
                existing_woo_id = template._find_existing_woo_product(wcapi)
                action = 'update' if existing_woo_id else 'create'
                start_time = time.time()

                # Perform the Create or Update API call
                if existing_woo_id:
                    _logger.debug(f"Updating Woo product ID {existing_woo_id} with data: {json.dumps(product_data, indent=2)}") # Log payload
                    response = wcapi.put(f"products/{existing_woo_id}", product_data)
                else:
                    _logger.debug(f"Creating new Woo product with data: {json.dumps(product_data, indent=2)}") # Log payload
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
                         variation_success = template._sync_woocommerce_variations(wcapi, retrieved_woo_id, variation_attribute_names)
                         # Variation sync errors are handled within that method and set on variants/template
                         if not variation_success:
                              _logger.warning(f"Variation sync encountered errors for template {template_display} (Woo ID: {retrieved_woo_id}). Template sync considered successful, but check variants.")
                              # Decide if template should be marked with error if variations failed.
                              # Currently, only variation method sets errors.

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
                # Catch unexpected errors during preparation or API call for THIS template
                error_msg = f"Unexpected error occurred while syncing {template_display}: {e}"
                _logger.error(error_msg, exc_info=True) # Log full traceback
                # Set error on the record if not already set by a sub-process
                if not template.woo_sync_error:
                    template.sudo().write({'woo_sync_error': error_msg})
                error_count += 1
                products_with_errors |= template
            finally:
                # Small delay within the loop (processing items in the batch)
                time.sleep(0.1) # Adjust as needed

        _logger.info(f"WooCommerce sync finished for this batch/selection. Processed: {len(products_to_process)}, Synced OK: {synced_count}, Errors: {error_count}")
        if products_with_errors:
            _logger.error(f"Products with sync errors in this batch/selection: {products_with_errors.mapped('name')}")
            # Raise error only if manually triggered AND there were errors in this specific call
            # Batch method will handle overall success/failure reporting
            if self.env.context.get('manual_sync_trigger') and error_count > 0:
                 # This error might be too disruptive if called from the batch method.
                 # Consider just logging and letting the batch method summarize.
                 # For now, let's raise it if manually called.
                 # raise UserError(_("Sync finished with errors for products: %s") % ', '.join(products_with_errors.mapped('name')))
                 pass # Let the calling context decide how to report errors


        # Return True if no errors occurred during this batch/selection run, False otherwise
        return error_count == 0


    # --- _sync_woocommerce_variations ---
    # (No changes needed in this method based on the request)
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
                # Fetch variations in pages
                response = wcapi.get(f"products/{woo_product_id}/variations", params={'per_page': 100, 'page': page})
                response.raise_for_status()
                current_page_variations = response.json()
                if not current_page_variations:
                    break # No more variations on subsequent pages
                all_variations.extend(current_page_variations)
                page += 1
                # Add a small delay between fetching pages if needed
                time.sleep(0.1)

            for var_data in all_variations:
                 if var_data.get('sku'): # Only map if SKU exists in Woo
                     existing_variations_map[var_data['sku']] = var_data

            _logger.info(f"Found {len(existing_variations_map)} existing variations with SKU in WooCommerce for Woo Product ID {woo_product_id}.")

        except Exception as e:
            error_msg = f"Error fetching existing WooCommerce variations for Woo Product ID {woo_product_id}: {e}"
            _logger.error(error_msg, exc_info=True)
            # Set error on template, as this prevents proper variation sync
            self.sudo().write({'woo_sync_error': f"Error fetching Woo variations: {e}"})
            return False # Cannot proceed without knowing existing variations

        # 2. Prepare batch data (create, update, delete)
        variation_batch_data = {'create': [], 'update': [], 'delete': []}
        odoo_variant_map_by_sku = {} # Map SKU -> Odoo product.product record
        odoo_variant_skus = set()   # Keep track of SKUs from Odoo variants being processed
        variants_with_prep_errors = self.env['product.product'] # Track variants that failed preparation

        for variant in variants_to_sync:
            # Clear previous variation-specific error
            variant.sudo().write({'woo_variation_sync_error': False})
            variant_prep_success = True # Track success for this specific variant prep

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
                overall_success = False
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
                    # ptav = product.template.attribute.value record linking template, attribute, and value
                    # We need the attribute name (from attribute_id) and the value name (from product_attribute_value_id)
                    attribute_name = ptav.attribute_id.name
                    attribute_value_name = ptav.product_attribute_value_id.name

                    # Only include attributes that are designated for variation use (from template prep)
                    if attribute_name in variation_attribute_names:
                        attributes_payload.append({
                            'name': attribute_name, # The name of the attribute (e.g., 'Color', 'Size')
                            'option': attribute_value_name # The specific value (e.g., 'Red', 'XL')
                        })
                        valid_attributes_found = True

                if not valid_attributes_found and variation_attribute_names:
                    # This variant doesn't seem to have valid attributes for variation matching, AND the template expects variation attributes
                    error_msg = f"Variant '{variant.display_name}' (SKU: {variant_sku}) has no attribute values matching the template's variation attributes ({variation_attribute_names}). Cannot sync as variation."
                    _logger.error(error_msg)
                    variant.sudo().write({'woo_variation_sync_error': error_msg})
                    variants_with_prep_errors |= variant
                    variant_prep_success = False
                    overall_success = False
                    continue # Skip adding this variant payload

                variation_payload['attributes'] = attributes_payload

                # Sync Price, Stock, Image for the variation
                if sync_price:
                    variation_payload['regular_price'] = str(variant.lst_price)
                    # Add sale price if applicable and configured
                    # sale_price = getattr(variant, 'sale_price_field_name', None) # Replace with your actual sale price field if exists
                    # if sale_price: variation_payload['sale_price'] = str(sale_price)

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

                    # Upload image using variant's image field
                    # This now calls the variant's _upload_image_to_wp which delegates to template's logic
                    media_id = variant._upload_image_to_wp('image_1920', 'variant', variant.id)
                    if media_id:
                        variation_payload['image'] = {'id': media_id}
                    # If upload fails, _upload_image_to_wp sets the error on the variant
                    if variant.woo_variation_sync_error:
                        _logger.warning(f"Image upload failed for variant SKU {variant_sku}. Proceeding without image for this variation.")
                        variants_with_prep_errors |= variant # Mark as having issues, even if non-blocking
                        # Decide if image failure should block variation sync (currently does not)
                        # variant_prep_success = False # Uncomment if image is mandatory
                        # overall_success = False # Uncomment if image is mandatory


                # Determine if creating or updating based on SKU map
                if variant_prep_success: # Only add if preparation was successful
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
                overall_success = False


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

                # Log the full batch payload if debugging is needed
                # _logger.debug(f"Variation Batch Payload: {json.dumps(variation_batch_data, indent=2)}")

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
                        overall_success = False # Mark overall failure
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
                         overall_success = False

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
                        overall_success = False
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
                        overall_success = False

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
                        overall_success = False
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
                overall_success = False
        else:
            _logger.info(f"No variation changes detected to sync via batch for Woo Product ID {woo_product_id}.")

        # Return True if no errors occurred during preparation or batch processing, False otherwise
        return overall_success


    # --- Cron Job ---
    # (No changes needed in this method based on the request)
    @api.model
    def _cron_sync_woocommerce(self):
        """ Scheduled action to sync all enabled products. """
        _logger.info("Starting scheduled WooCommerce sync via Cron...")
        # Use a specific context for cron to differentiate from manual triggers
        cron_context = {'manual_sync_trigger': False}
        ProductTemplate = self.with_context(cron_context)

        # Check if sync is active and credentials are valid before searching products
        try:
            # Use with_context when calling API client getter
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
            # Process in batches using the existing cron batching logic
            # We are NOT using the new sync_to_woocommerce_in_batches here,
            # as the cron job needs to manage its own potentially larger batches.
            batch_size = 5 # Keep the cron's batch size (or make configurable)
            total_batches = (len(products_to_sync) + batch_size - 1) // batch_size

            for i in range(0, len(products_to_sync), batch_size):
                batch = products_to_sync[i:i + batch_size]
                current_batch_num = i // batch_size + 1
                _logger.info(f"Cron: Processing batch {current_batch_num}/{total_batches} ({len(batch)} products)")

                try:
                    # Call the main sync method for the batch, passing cron context
                    # The sync_to_woocommerce method handles the items in 'batch'
                    batch_success = batch.sync_to_woocommerce()
                    # Commit transaction after each batch attempt (success or failure handled within sync_to_woocommerce)
                    if batch_success:
                         self.env.cr.commit()
                         _logger.info(f"Cron: Batch {current_batch_num} processed and committed.")
                    else:
                         _logger.warning(f"Cron: Batch {current_batch_num} processed with errors. Committing partial success/errors.")
                         self.env.cr.commit() # Commit even with errors to save error messages on records
                         # Consider rollback if partial success is not desired:
                         # self.env.cr.rollback()
                         # _logger.info(f"Cron: Rolled back changes for batch {current_batch_num} due to errors.")


                except Exception as e:
                    # Catch errors that might occur *outside* sync_to_woocommerce during batch handling
                    _logger.error(f"Critical Error occurred processing cron batch {current_batch_num}: {e}", exc_info=True)
                    # Rollback changes for the failed batch
                    self.env.cr.rollback()
                    _logger.info(f"Cron: Rolled back changes for batch {current_batch_num} due to critical error.")
                    # Consider adding failed batch product IDs to a list for retry or reporting

                # Optional: Small delay between batches
                time.sleep(1) # Be mindful of total cron execution time

        else:
            _logger.info("Cron: No product templates found enabled for WooCommerce sync.")

        _logger.info("Scheduled WooCommerce sync finished.")

    # --- NEW BATCHING METHOD ---
    # (No changes needed in this method based on the request)
    def sync_to_woocommerce_in_batches(self, batch_size=5):
        """
        Syncs enabled product templates within 'self' to WooCommerce in small batches.
        Handles transaction commit/rollback per batch.
        Intended to be called manually (e.g., from a wizard).
        """
        # Use context to indicate manual trigger if not already set (important for error handling in sub-methods)
        batch_context = self.env.context.copy()
        batch_context['manual_sync_trigger'] = True # Assume manual when called this way

        # Filter the initial recordset ('self') for enabled products
        enabled_products = self.filtered('woo_sync_enabled')
        if not enabled_products:
            # Raise error directly if called manually and nothing is enabled in the selection
            if len(self) > 0: # Check if the original selection was not empty
                raise UserError(_("None of the selected products are enabled for WooCommerce sync."))
            else:
                 _logger.info("Batch Sync: No enabled products found in the provided selection.")
                 return True # Nothing to do

        total_products = len(enabled_products)
        total_batches = (total_products + batch_size - 1) // batch_size
        _logger.info(f"Batch Sync: Starting sync for {total_products} enabled products in {total_batches} batches (batch size: {batch_size})")

        all_batches_successful = True # Track overall success

        for i in range(0, total_products, batch_size):
            batch = enabled_products[i:i + batch_size]
            current_batch_num = (i // batch_size) + 1
            _logger.info(f"Batch Sync: Processing batch {current_batch_num}/{total_batches} (Products: {len(batch)})")

            try:
                # Call the core sync logic for the current batch, passing the context
                batch_success = batch.with_context(batch_context).sync_to_woocommerce()

                if batch_success:
                    self.env.cr.commit()
                    _logger.info(f"Batch {current_batch_num} sync completed successfully and committed.")
                else:
                     # sync_to_woocommerce returned False, indicating errors occurred within the batch
                     _logger.warning(f"Batch {current_batch_num} sync completed with errors. Committing state (including error messages).")
                     self.env.cr.commit() # Commit to save error messages set by sync_to_woocommerce
                     all_batches_successful = False # Mark that at least one batch had issues
                     # Optionally rollback if partial success isn't desired:
                     # self.env.cr.rollback()
                     # _logger.warning(f"Batch {current_batch_num} sync failed. Rolling back changes for this batch.")


            except Exception as e:
                # Catch unexpected errors during the batch processing (e.g., commit failure, or error raised by sync_to_woocommerce)
                self.env.cr.rollback()
                _logger.error(f"Batch Sync: Critical error processing batch {current_batch_num}. Rolled back. Error: {e}", exc_info=True)
                all_batches_successful = False
                # Option 1: Stop processing further batches on critical error
                # raise UserError(_("Critical error during batch %s: %s. Aborting further batches.") % (current_batch_num, e))
                # Option 2: Continue to the next batch (current implementation)
                _logger.info(f"Batch Sync: Continuing to next batch despite error in batch {current_batch_num}.")


            # Slight delay between batches regardless of success/failure
            time.sleep(1)

        _logger.info(f"Batch Sync finished processing all {total_batches} batches.")
        return all_batches_successful # Return overall status


# --- product.product Model ---
# Inherit product.product to add variation-specific fields and helpers
# (No changes needed in this class based on the request)
class ProductProduct(models.Model):
    _inherit = 'product.product'

    # --- Fields for Variation Sync Status ---
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
    # This delegates image upload logic to the template's method but uses variant's data
    def _upload_image_to_wp(self, image_field_name, record_name, record_id):
        """
        Uploads an image for this variant using the template's WP session and logic.
        It ensures the variant's image data is used and errors are set on the variant.
        """
        self.ensure_one()
        if not self.product_tmpl_id:
             error_msg = 'Cannot sync variant image: Missing parent template link.'
             _logger.error(f"{error_msg} for variant {self.id}")
             self.sudo().write({'woo_variation_sync_error': error_msg})
             return None

        # Call the *template's* _upload_image_to_wp method,
        # but it will internally use getattr(self, image_field_name) where 'self' is this variant record.
        # Pass correct record_name and record_id for logging/filename purposes.
        return self.product_tmpl_id._upload_image_to_wp(image_field_name, 'variant', self.id)


    # --- Sync Button Action (Optional: Button on Variant Form) ---
    # (No changes needed in this method based on the request)
    def action_sync_variant_parent_to_woocommerce(self):
        """
        Button action placed on the product.product (variant) form
        to trigger the synchronization process for its parent template using BATCHING.
        """
        templates_to_sync = self.mapped('product_tmpl_id')
        if not templates_to_sync:
            raise UserError(_("No parent product template found for the selected variant(s)."))

        # Check if the template is actually enabled for sync
        enabled_templates = templates_to_sync.filtered('woo_sync_enabled')
        if not enabled_templates:
             raise UserError(_("WooCommerce sync is not enabled for the parent product template(s) of the selected variant(s). Please enable sync on the template first."))

        _logger.info(f"Manual sync trigger initiated from variant form for template(s): {enabled_templates.mapped('name')}. Using batch sync.")

        # Call the BATCH sync method on the template(s)
        # Use a small batch size appropriate for a manual trigger on potentially few templates
        batch_success = enabled_templates.sync_to_woocommerce_in_batches(batch_size=5)

        # Provide user feedback based on overall success
        if batch_success:
             message = _('Synchronization complete for parent template(s): %s. Check logs or template/variant status for details.') % ', '.join(enabled_templates.mapped('name'))
             notif_type = 'success'
             title = _('WooCommerce Sync Completed')
        else:
             message = _('Synchronization finished with errors for parent template(s): %s. Please check logs and template/variant error fields.') % ', '.join(enabled_templates.mapped('name'))
             notif_type = 'warning' # Use warning as some might have succeeded
             title = _('WooCommerce Sync Finished with Errors')

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'sticky': False, # Notification disappears automatically
                'type': notif_type, # 'info', 'warning', 'danger', 'success'
            }
        }