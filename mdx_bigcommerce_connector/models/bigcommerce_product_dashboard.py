from odoo import api, models


class BigcommerceProductDashboard(models.TransientModel):
    _name = "bigcommerce.product.dashboard"
    _description = "BigCommerce Product Dashboard"

    @api.model
    def get_dashboard_data(self, config_id=None):
        Product = self.env["bigcommerce.product"]
        Variant = self.env["bigcommerce.product.variant"]

        base = [("config_id", "=", int(config_id))] if config_id else []

        total_products = Product.search_count(base)
        visible_count = Product.search_count(base + [("is_visible", "=", True)])
        hidden_count = Product.search_count(base + [("is_visible", "=", False)])

        synced_count = Product.search_count(base + [("sync_state", "=", "synced")])
        pending_count = Product.search_count(base + [("sync_state", "=", "pending")])
        error_count = Product.search_count(base + [("sync_state", "=", "error")])

        products = Product.search(base)
        variants = Variant.search([("bigcommerce_product_id", "in", products.ids)]) if products else Variant.browse()

        in_stock_products = len(products.filtered(lambda p: sum(p.variant_ids.mapped("inventory_level")) > 0))
        out_stock_products = total_products - in_stock_products
        low_stock_products = len(products.filtered(
            lambda p: 0 < sum(p.variant_ids.mapped("inventory_level")) <= 5))

        total_stock = sum(variants.mapped("inventory_level"))
        total_variants = len(variants)

        prices = [v.sale_price or v.price for v in variants if (v.sale_price or v.price)]
        avg_price = round(sum(prices) / len(prices), 2) if prices else 0.0

        brand_counts = {}
        for p in products:
            b = (p.brand_name or "Unbranded").strip() or "Unbranded"
            brand_counts[b] = brand_counts.get(b, 0) + 1
        top_brands = sorted(brand_counts.items(), key=lambda x: -x[1])[:8]

        category_counts = {}
        for p in products:
            for c in p.category_ids:
                category_counts[c.name] = category_counts.get(c.name, 0) + 1
        top_categories = sorted(category_counts.items(), key=lambda x: -x[1])[:8]

        return {
            "total_products": total_products,
            "visible_count": visible_count,
            "hidden_count": hidden_count,
            "synced_count": synced_count,
            "pending_count": pending_count,
            "error_count": error_count,
            "in_stock_products": in_stock_products,
            "out_stock_products": out_stock_products,
            "low_stock_products": low_stock_products,
            "total_stock": total_stock,
            "total_variants": total_variants,
            "avg_price": avg_price,
            "top_brands": [{"name": k, "count": v} for k, v in top_brands],
            "top_categories": [{"name": k, "count": v} for k, v in top_categories],
        }
