# -*- coding: utf-8 -*-
{
    'name': "WooCommerce Inventory Sync + AI Descriptions",
    'summary': """
        Sync inventory (Stock, Description, Image, Price) from Odoo to WooCommerce — now with AI-generated descriptions.
    """,
    'description': """
        This module allows you to synchronize product information like stock levels,
        descriptions, prices, categories, brand names, and main images from Odoo products to your WooCommerce store
        using the WooCommerce REST API.

        ✨ New Features:
        - AI-powered product descriptions via OpenAI ChatGPT.
        - Automatic brand extraction and smart category assignment using GPT.
        - OpenAI configuration directly in Odoo Settings UI.
        
        🛠 Existing Features:
        - Configure WooCommerce API credentials in Odoo Settings.
        - Enable/Disable sync per product.
        - Manual Sync & Scheduled Sync (via Odoo Cron Jobs).
        - Sync individual fields selectively (Stock, Description, Image, Price).
        - Support for both Simple and Variable products.
        - Batch syncing with progress logging and error feedback.

        📦 Requirements:
        - Python libraries:
          - woocommerce
          - requests
          - openai

          (Install with: pip install woocommerce requests openai)
    """,
    'author': "Luis Laguna / World App Technologies",
    'website': "https://www.worldapptechnologies.com",
    'category': 'Inventory/Connector',
    'version': '1.5',
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
    'auto_install': False,

    'depends': [
        'stock',
        'sale_management',
        'product',
    ],
    'external_dependencies': {
        'python': ['woocommerce', 'requests', 'openai'],
    },

    'icon': '/odoo_woo_sync/static/description/icon.png',

    'data': [
        'security/ir_model_access.xml',
        'data/ir_cron_data.xml',
        'wizards/woo_sync_wizard_views.xml',
        'views/product_template_views.xml',
        'views/product_product_views.xml',
        'views/res_config_settings_views.xml',
        # 'views/odoo_woo_sync_menus.xml',  # Uncomment when needed
    ],
}
