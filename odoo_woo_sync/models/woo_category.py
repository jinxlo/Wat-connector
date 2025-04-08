from odoo import models, fields

class WooCategory(models.Model):
    _name = 'odoo_woo_sync.category'
    _description = 'WooCommerce Category'

    woo_id = fields.Char(string='Woo ID', required=True, index=True)
    name = fields.Char(string='Category Name', required=True)
    parent_id = fields.Many2one('odoo_woo_sync.category', string='Parent Category')
