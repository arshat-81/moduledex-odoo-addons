import base64
import binascii
import csv
import io
import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Value column(s) expected in an uploaded file, per update type. The parser
# also accepts a generic "value" column as a fallback for the single-value ops.
FILE_COLUMNS = {
    "price": ("price",),
    "sale_price": ("sale_price",),
    "cost_price": ("cost_price",),
    "quantity": ("quantity",),
    "weight": ("weight",),
    "moq": ("order_quantity_minimum", "order_quantity_maximum"),
    "tax_code": ("tax_code",),
    "search_keywords": ("search_keywords",),
    "availability": ("availability",),
    "featured": ("is_featured",),
    "purchasing": ("purchasing_enabled",),
    "publish": (),
    "unpublish": (),
}

TRUTHY = {"1", "true", "yes", "y", "t", "enabled", "visible"}


def _to_bool(raw):
    return str(raw).strip().lower() in TRUTHY


def _to_float(raw):
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise UserError(_("'%(raw)s' is not a valid number.", raw=raw)) from exc

# Which BigCommerce level each operation writes to.
PRODUCT_LEVEL = {
    "publish", "unpublish", "availability", "featured", "moq",
    "search_keywords", "tax_code",
}
VARIANT_LEVEL = {"price", "sale_price", "cost_price", "quantity", "weight", "purchasing"}


class BigcommerceBulkUpdateWizard(models.TransientModel):
    _name = "bigcommerce.bulk.update.wizard"
    _description = "Bulk update BigCommerce from Odoo"

    update_type = fields.Selection(
        [
            ("price", "Price"),
            ("sale_price", "Sale Price"),
            ("cost_price", "Cost Price"),
            ("quantity", "Quantity / Stock"),
            ("weight", "Weight"),
            ("purchasing", "Enable / Disable Purchasing"),
            ("publish", "Publish (show on storefront)"),
            ("unpublish", "Unpublish (hide from storefront)"),
            ("availability", "Availability"),
            ("featured", "Featured / Not Featured"),
            ("moq", "Min / Max Order Quantity"),
            ("search_keywords", "Search Keywords"),
            ("tax_code", "Tax Code"),
        ],
        required=True, default="price",
    )

    source = fields.Selection(
        [("records", "Pick records"), ("file", "Upload a file")],
        default="records", required=True,
        help="A file lets you set a different value per SKU in one go.",
    )
    import_file = fields.Binary(string="File (.csv, .xlsx, .ods)")
    import_filename = fields.Char()
    sample_file = fields.Binary(readonly=True)
    sample_filename = fields.Char(readonly=True)

    product_ids = fields.Many2many("bigcommerce.product", string="Products")
    variant_ids = fields.Many2many("bigcommerce.product.variant", string="Variants")
    target_count = fields.Integer(compute="_compute_target_count")
    level_hint = fields.Char(compute="_compute_target_count")

    # price-family options
    value_mode = fields.Selection(
        [
            ("odoo", "Use the current Odoo price"),
            ("fixed", "Set a fixed value"),
            ("increase", "Increase by amount"),
            ("decrease", "Decrease by amount"),
            ("pct_inc", "Increase by %"),
            ("pct_dec", "Decrease by %"),
        ],
        default="odoo", required=True,
    )
    amount = fields.Float(string="Amount / Percentage")

    # simple scalar options
    availability = fields.Selection(
        [("available", "Available"), ("disabled", "Disabled"), ("preorder", "Pre-order")],
        default="available",
    )
    purchasing_enabled = fields.Boolean(string="Purchasing enabled", default=True)
    is_featured = fields.Boolean(string="Featured", default=True)
    order_quantity_minimum = fields.Integer()
    order_quantity_maximum = fields.Integer()
    search_keywords = fields.Char(help="Comma-separated. BigCommerce's closest equivalent to tags.")
    tax_code = fields.Char(string="Product Tax Code")

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        model, ids = ctx.get("active_model"), ctx.get("active_ids", [])
        if model == "bigcommerce.product.variant":
            variants = self.env["bigcommerce.product.variant"].browse(ids)
            res["variant_ids"] = [(6, 0, variants.ids)]
            res["product_ids"] = [(6, 0, variants.bigcommerce_product_id.ids)]
        elif model == "bigcommerce.product":
            products = self.env["bigcommerce.product"].browse(ids)
            res["product_ids"] = [(6, 0, products.ids)]
            res["variant_ids"] = [(6, 0, products.variant_ids.ids)]
        return res

    @api.depends("update_type", "product_ids", "variant_ids")
    def _compute_target_count(self):
        for wiz in self:
            if wiz.update_type in PRODUCT_LEVEL:
                wiz.target_count = len(wiz.product_ids)
                wiz.level_hint = _("Applies to %(n)s product(s)", n=len(wiz.product_ids))
            else:
                wiz.target_count = len(wiz.variant_ids)
                wiz.level_hint = _("Applies to %(n)s variant(s)", n=len(wiz.variant_ids))

    # ── file parsing ────────────────────────────────────────────────────────
    def _expected_columns(self):
        return ("sku",) + FILE_COLUMNS.get(self.update_type, ())

    def _read_rows(self):
        """Parse the uploaded csv/xlsx/ods into a list of lower-cased dicts.

        Returns [] rather than raising when the file is simply empty, but
        raises a clear UserError for anything the user needs to fix.
        """
        self.ensure_one()
        if not self.import_file:
            raise UserError(_("Attach a file first, or switch back to picking records."))
        name = (self.import_filename or "").lower()
        try:
            blob = base64.b64decode(self.import_file)
        except (binascii.Error, TypeError) as exc:
            raise UserError(_("That file could not be decoded.")) from exc

        if name.endswith(".csv"):
            rows = self._read_csv(blob)
        elif name.endswith((".xlsx", ".xlsm")):
            rows = self._read_xlsx(blob)
        elif name.endswith(".ods"):
            rows = self._read_ods(blob)
        else:
            raise UserError(_(
                "Unsupported file type. Use .csv, .xlsx or .ods — the filename "
                "must keep its extension."))

        if not rows:
            raise UserError(_("That file has no data rows."))

        header = [str(h or "").strip().lower() for h in rows[0]]
        if "sku" not in header:
            raise UserError(_(
                "The file needs a 'sku' column. Found: %(cols)s\n\n"
                "Use the Download Sample button to get a correctly-shaped file.",
                cols=", ".join(h for h in header if h) or _("(nothing)")))

        out = []
        for raw in rows[1:]:
            row = {header[i]: raw[i] for i in range(min(len(header), len(raw)))}
            if str(row.get("sku") or "").strip():
                out.append(row)
        return out

    def _read_csv(self, blob):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = blob.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise UserError(_("Could not read that CSV — try saving it as UTF-8."))
        # sniff , ; or tab so exports from any locale work
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        return [row for row in csv.reader(io.StringIO(text), dialect) if any(str(c).strip() for c in row)]

    def _read_xlsx(self, blob):
        import openpyxl  # noqa: PLC0415
        book = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
        sheet = book[book.sheetnames[0]]
        rows = []
        for record in sheet.rows:
            values = ["" if c.value is None else c.value for c in record]
            if any(str(v).strip() for v in values):
                rows.append(values)
        book.close()
        return rows

    def _read_ods(self, blob):
        """Read the first sheet of an .ods file.

        Prefers Odoo's base_import reader, but that needs the optional `odfpy`
        package. ODS is just a zip with a content.xml inside, so fall back to
        parsing it directly rather than making buyers install a dependency.
        """
        try:
            from odoo.addons.base_import.models.odf_ods_reader import ODSReader  # noqa: PLC0415
            doc = ODSReader(file=io.BytesIO(blob))
            sheet = list(doc.SHEETS.keys())[0]
            return [row for row in doc.getSheet(sheet) if any(str(x).strip() for x in row)]
        except ImportError:
            return self._read_ods_fallback(blob)

    def _read_ods_fallback(self, blob):
        import zipfile  # noqa: PLC0415
        from xml.etree import ElementTree  # noqa: PLC0415

        TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
        TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"

        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                content = archive.read("content.xml")
        except (zipfile.BadZipFile, KeyError) as exc:
            raise UserError(_("That .ods file could not be read.")) from exc

        root = ElementTree.fromstring(content)
        table = root.find(f".//{TABLE}table")
        if table is None:
            raise UserError(_("That .ods file has no sheet in it."))

        rows = []
        for tr in table.findall(f"{TABLE}table-row"):
            row = []
            for cell in tr.findall(f"{TABLE}table-cell"):
                # a cell carries its text in one or more <text:p> children
                text = "".join(
                    "".join(p.itertext()) for p in cell.findall(f"{TEXT}p"))
                # ODS collapses runs of identical cells into one repeated cell
                repeat = int(cell.get(f"{TABLE}number-columns-repeated", 1) or 1)
                # guard against the trailing "repeat to end of sheet" padding
                row.extend([text] * min(repeat, 64))
            while row and not str(row[-1]).strip():
                row.pop()
            if any(str(c).strip() for c in row):
                rows.append(row)
        return rows

    def action_download_sample(self):
        """Build a sample CSV whose columns match the chosen update type."""
        self.ensure_one()
        columns = self._expected_columns()
        if len(columns) == 1:  # publish / unpublish: sku only
            examples = [["SKU-001"], ["SKU-002"]]
        elif self.update_type == "moq":
            examples = [["SKU-001", 1, 10], ["SKU-002", 5, 50]]
        elif self.update_type in ("availability",):
            examples = [["SKU-001", "available"], ["SKU-002", "disabled"]]
        elif self.update_type in ("featured", "purchasing"):
            examples = [["SKU-001", "yes"], ["SKU-002", "no"]]
        elif self.update_type in ("tax_code", "search_keywords"):
            examples = [["SKU-001", "example-value"], ["SKU-002", "another-value"]]
        else:
            examples = [["SKU-001", 19.99], ["SKU-002", 24.50]]

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        writer.writerows(examples)
        self.write({
            "sample_file": base64.b64encode(buf.getvalue().encode("utf-8")),
            "sample_filename": f"bigcommerce_{self.update_type}_sample.csv",
        })
        return {
            "type": "ir.actions.act_url",
            "url": (f"/web/content/?model={self._name}&id={self.id}"
                    f"&field=sample_file&filename_field=sample_filename&download=true"),
            "target": "self",
        }

    # ── value helpers ───────────────────────────────────────────────────────
    def _derive_value(self, current, odoo_value):
        """Resolve the new numeric value from the chosen mode."""
        self.ensure_one()
        mode = self.value_mode
        if mode == "odoo":
            if odoo_value is None:
                raise UserError(_("No linked Odoo product to take the value from."))
            return round(odoo_value, 4)
        if mode == "fixed":
            return round(self.amount, 4)
        base = current or 0.0
        if mode == "increase":
            return round(base + self.amount, 4)
        if mode == "decrease":
            return round(max(base - self.amount, 0.0), 4)
        if mode == "pct_inc":
            return round(base * (1 + self.amount / 100.0), 4)
        if mode == "pct_dec":
            return round(base * (1 - self.amount / 100.0), 4)
        return base

    def _odoo_stock(self, variant):
        return int(sum(self.env["stock.quant"].sudo().search([
            ("product_id", "=", variant.product_id.id),
            ("location_id", "=", variant.config_id.warehouse_id.lot_stock_id.id),
        ]).mapped("quantity"))) if variant.product_id else 0

    # ── main ────────────────────────────────────────────────────────────────
    def _queue_active(self):
        """True when at least one target store defers work to the job queue."""
        configs = (self.product_ids.config_id | self.variant_ids.config_id)
        if not configs:
            configs = self.env["bigcommerce.config"].search(
                [("active", "=", True), ("state", "=", "connected")])
        return any(configs.mapped("use_job_queue"))

    def action_push_to_bigcommerce(self):
        self.ensure_one()
        Log = self.env["bigcommerce.update.log"]
        run_id = "PUSH-%s" % uuid.uuid4().hex[:10].upper()
        updated, failed = 0, 0

        if self.source == "file":
            updated, failed, missing = self._push_from_file(Log, run_id)
            message = (_("Queued %(count)s job(s). They run in the background - "
                         "watch Configuration > Queued Jobs.", count=updated)
                       if self._queue_active() else
                       _("Updated %(count)s record(s) on BigCommerce.", count=updated))
            if missing:
                message += "\n" + _("%(count)s SKU(s) not found in Odoo: %(skus)s",
                                    count=len(missing), skus=", ".join(missing[:8]))
            if failed:
                message += "\n" + _("%(count)s failed — see the sync log (run %(run)s).",
                                    count=failed, run=run_id)
            return {
                "type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": _("BigCommerce"), "message": message,
                           "type": "warning" if (failed or missing) else "success",
                           "sticky": bool(failed or missing)},
            }

        if not self.target_count:
            raise UserError(_("Nothing selected for this update type."))

        if self.update_type in PRODUCT_LEVEL:
            updated, failed = self._push_product_level(Log, run_id)
        else:
            updated, failed = self._push_variant_level(Log, run_id)

        message = (_("Queued %(count)s job(s). They run in the background - "
                     "watch Configuration > Queued Jobs.", count=updated)
                   if self._queue_active() else
                   _("Updated %(count)s record(s) on BigCommerce.", count=updated))
        if failed:
            message += "\n" + _("%(count)s failed — see the sync log (run %(run)s).",
                                count=failed, run=run_id)
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("BigCommerce"), "message": message,
                       "type": "warning" if failed else "success", "sticky": bool(failed)},
        }

    def _row_value(self, row, column):
        """Pull a column from a file row, accepting a generic 'value' fallback."""
        if column in row and str(row[column]).strip() != "":
            return row[column]
        if "value" in row and str(row["value"]).strip() != "":
            return row["value"]
        raise UserError(_(
            "Row for SKU '%(sku)s' has no '%(col)s' (or 'value') column filled in.",
            sku=row.get("sku"), col=column))

    def _push_from_file(self, Log, run_id):
        """Apply per-row values from the uploaded file, matched by SKU."""
        Variant = self.env["bigcommerce.product.variant"]
        Product = self.env["bigcommerce.product"]
        rows = self._read_rows()
        product_level = self.update_type in PRODUCT_LEVEL
        updated = failed = 0
        missing = []

        for row in rows:
            sku = str(row["sku"]).strip()
            variant = Variant.search([("sku", "=", sku)], limit=1)
            if product_level:
                target = (Product.search([("sku", "=", sku)], limit=1)
                          or variant.bigcommerce_product_id)
            else:
                target = variant
            if not target:
                missing.append(sku)
                continue

            config = target.config_id
            body, old, new = {}, None, None
            try:
                if self.update_type in ("publish", "unpublish"):
                    new = self.update_type == "publish"
                    old, body["is_visible"] = target.is_visible, new
                elif self.update_type == "availability":
                    new = str(self._row_value(row, "availability")).strip().lower()
                    body["availability"] = new
                elif self.update_type == "featured":
                    new = _to_bool(self._row_value(row, "is_featured"))
                    body["is_featured"] = new
                elif self.update_type == "moq":
                    mn = int(_to_float(self._row_value(row, "order_quantity_minimum")))
                    mx = int(_to_float(self._row_value(row, "order_quantity_maximum")))
                    new = f"{mn}/{mx}"
                    body["order_quantity_minimum"] = mn
                    body["order_quantity_maximum"] = mx
                elif self.update_type == "search_keywords":
                    new = str(self._row_value(row, "search_keywords"))
                    body["search_keywords"] = new
                elif self.update_type == "tax_code":
                    new = str(self._row_value(row, "tax_code"))
                    body["product_tax_code"] = new
                elif self.update_type == "price":
                    old = target.price
                    new = _to_float(self._row_value(row, "price"))
                    body["price"] = new
                elif self.update_type == "sale_price":
                    old = target.sale_price
                    new = _to_float(self._row_value(row, "sale_price"))
                    body["sale_price"] = new
                elif self.update_type == "cost_price":
                    new = _to_float(self._row_value(row, "cost_price"))
                    body["cost_price"] = new
                elif self.update_type == "quantity":
                    old = target.inventory_level
                    new = int(_to_float(self._row_value(row, "quantity")))
                    body["inventory_level"] = new
                elif self.update_type == "weight":
                    old = target.weight
                    new = _to_float(self._row_value(row, "weight"))
                    body["weight"] = new
                elif self.update_type == "purchasing":
                    new = not _to_bool(self._row_value(row, "purchasing_enabled"))
                    body["purchasing_disabled"] = new
            except UserError as exc:
                failed += 1
                Log.log_change(config, run_id, self.update_type, variant, old, new, "error", str(exc))
                continue

            try:
                # job_push_fields owns the request, the write-back and the log,
                # so it behaves identically whether queued or run inline.
                config.dispatch(
                    target, "job_push_fields",
                    "[%s] %s | %s" % (run_id, self.update_type, sku),
                    run_id=run_id, body=body, update_type=self.update_type,
                    old=old, new=new)
            except Exception:  # noqa: BLE001 — one bad row shouldn't stop the file
                failed += 1
                continue
            updated += 1

        return updated, failed, missing

    def _push_product_level(self, Log, run_id):
        updated = failed = 0
        for product in self.product_ids:
            config = product.config_id
            body, old, new = {}, None, None

            if self.update_type in ("publish", "unpublish"):
                new = self.update_type == "publish"
                old = product.is_visible
                body["is_visible"] = new
            elif self.update_type == "availability":
                old, new = None, self.availability
                body["availability"] = new
            elif self.update_type == "featured":
                old, new = None, self.is_featured
                body["is_featured"] = new
            elif self.update_type == "moq":
                old, new = None, f"{self.order_quantity_minimum}/{self.order_quantity_maximum}"
                body["order_quantity_minimum"] = self.order_quantity_minimum
                body["order_quantity_maximum"] = self.order_quantity_maximum
            elif self.update_type == "search_keywords":
                old, new = None, self.search_keywords or ""
                body["search_keywords"] = new
            elif self.update_type == "tax_code":
                old, new = None, self.tax_code or ""
                body["product_tax_code"] = new

            try:
                config.dispatch(
                    product, "job_push_fields",
                    "[%s] %s | %s" % (run_id, self.update_type, product.sku or product.name),
                    run_id=run_id, body=body, update_type=self.update_type,
                    old=old, new=new)
            except Exception:  # noqa: BLE001 — job_push_fields already logged it
                failed += 1
                continue
            updated += 1
        return updated, failed

    def _push_variant_level(self, Log, run_id):
        updated = failed = 0
        for variant in self.variant_ids:
            config = variant.config_id
            body, old, new = {}, None, None
            odoo_product = variant.product_id

            if self.update_type == "price":
                old = variant.price
                new = self._derive_value(old, odoo_product.lst_price if odoo_product else None)
                body["price"] = new
            elif self.update_type == "sale_price":
                if self.value_mode == "odoo":
                    raise UserError(_(
                        "Sale price has no Odoo counterpart to copy from — pick a fixed "
                        "value or an increase/decrease instead."))
                old = variant.sale_price
                new = self._derive_value(old, None)
                body["sale_price"] = new
            elif self.update_type == "cost_price":
                old = None
                new = self._derive_value(0.0, odoo_product.standard_price if odoo_product else None)
                body["cost_price"] = new
            elif self.update_type == "quantity":
                old = variant.inventory_level
                new = (self._odoo_stock(variant) if self.value_mode == "odoo"
                       else int(self._derive_value(old, None)))
                body["inventory_level"] = new
            elif self.update_type == "weight":
                old = variant.weight
                new = self._derive_value(old, odoo_product.weight if odoo_product else None)
                body["weight"] = new
            elif self.update_type == "purchasing":
                old, new = None, not self.purchasing_enabled
                body["purchasing_disabled"] = new

            try:
                config.dispatch(
                    variant, "job_push_fields",
                    "[%s] %s | %s" % (run_id, self.update_type, variant.sku or variant.id),
                    run_id=run_id, body=body, update_type=self.update_type,
                    old=old, new=new)
            except Exception:  # noqa: BLE001 — job_push_fields already logged it
                failed += 1
                continue
            updated += 1
        return updated, failed
