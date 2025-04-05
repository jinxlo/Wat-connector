# -*- coding: utf-8 -*-
{
    'name': "WooCommerce Inventory Sync",
    'summary': """
        Sync inventory (Stock, Description, Image, Price) from Odoo to WooCommerce with configuration interface.
    """,
    'description': """
        This module allows you to synchronize product information like stock levels,
        descriptions, prices, and the main image from Odoo products to your WooCommerce store
        using the WooCommerce REST API.
        - Configure API Credentials in Settings.
        - Test API Connection.
        - Enable/Disable sync per product.
        - Manual Sync / Enable actions via wizard.
        - Scheduled Sync (configurable via Odoo Cron UI).
        - Selectively sync fields (Stock, Description, Image, Price).
        - Basic handling for Simple and Variable products.

        Requires 'woocommerce' python library (pip install woocommerce requests).
    """,
    'author': "Luis Laguna / World App Technologies",
    'website': "https://www.worldapptechnologies.com",
    'category': 'Inventory/Connector',
    'version': '1.4',
    'depends': ['stock', 'sale_management', 'product'],
    'external_dependencies': {
        'python': ['woocommerce', 'requests'],
    },

    'icon': '/odoo_woo_sync/static/description/icon.png',

    'data': [
        # Security first
        'security/ir_model_access.xml',
        # Data
        'data/ir_cron_data.xml',
        # Wizard Views (loaded BEFORE they are referenced in settings)
        'wizards/woo_sync_wizard_views.xml',
        # Main Views
        'views/product_template_views.xml',
        'views/product_product_views.xml',
        'views/res_config_settings_views.xml',
        # Menu (uncomment once finalized)
        # 'views/odoo_woo_sync_menus.xml',
    ],

    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
