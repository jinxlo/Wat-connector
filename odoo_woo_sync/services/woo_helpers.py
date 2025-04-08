def fetch_and_store_woo_categories(env):
    wcapi = env['product.template']._get_woo_api_client()
    if not wcapi:
        return

    response = wcapi.get("products/categories", params={"per_page": 100})
    if response.status_code != 200:
        raise Exception(f"Failed to fetch Woo categories: {response.text}")

    categories = response.json()
    WooCategory = env['odoo_woo_sync.category']
    WooCategory.sudo().search([]).unlink()  # Clear old

    for cat in categories:
        parent_id = None
        if cat['parent']:
            parent = WooCategory.search([('woo_id', '=', str(cat['parent']))], limit=1)
            parent_id = parent.id if parent else None
        WooCategory.create({
            'woo_id': str(cat['id']),
            'name': cat['name'],
            'parent_id': parent_id
        })
