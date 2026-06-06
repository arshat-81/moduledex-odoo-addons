from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class MdxExternalIdFinder(models.TransientModel):
    _name = "mdx.external.id.finder"
    _description = "External ID Finder"

    search_mode = fields.Selection(
        [
            ("xmlid", "XML ID"),
            ("record", "Model and ID"),
            ("text", "Text Search"),
        ],
        default="xmlid",
        required=True,
    )
    xml_id = fields.Char(string="XML ID")
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        domain=[("transient", "=", False)],
        ondelete="cascade",
    )
    model = fields.Char(related="model_id.model", readonly=True)
    res_id = fields.Integer(string="Database ID")
    text_query = fields.Char(string="Search Text")
    create_module = fields.Char(string="Module Prefix", default="__custom__")
    create_name = fields.Char(string="Identifier Name")
    create_noupdate = fields.Boolean(string="Non Updatable")
    line_ids = fields.One2many(
        "mdx.external.id.finder.line",
        "wizard_id",
        string="Results",
    )
    line_count = fields.Integer(compute="_compute_line_count")
    state_message = fields.Char(readonly=True)

    @api.depends("line_ids")
    def _compute_line_count(self):
        for wizard in self:
            wizard.line_count = len(wizard.line_ids)

    def _compute_display_name(self):
        for wizard in self:
            wizard.display_name = _("External ID Finder")

    def _clear_lines(self):
        self.line_ids.unlink()

    def _ensure_model_record(self):
        self.ensure_one()
        if not self.model:
            raise UserError(_("Choose a model first."))
        if self.model not in self.env:
            raise UserError(_("Model %s is not available in this database.") % self.model)
        if not self.res_id:
            raise UserError(_("Enter a database ID greater than zero."))

        record = self.env[self.model].browse(self.res_id).exists()
        if not record:
            raise UserError(
                _("No record exists for model %(model)s with ID %(res_id)s.")
                % {"model": self.model, "res_id": self.res_id}
            )
        return record

    def _line_values_from_xid(self, xid):
        record_name = False
        exists = False
        if xid.model in self.env and xid.res_id:
            record = self.env[xid.model].browse(xid.res_id).exists()
            exists = bool(record)
            if record:
                try:
                    record_name = record.display_name
                except Exception:
                    record_name = False
        return {
            "wizard_id": self.id,
            "external_id_id": xid.id,
            "xml_id": xid.complete_name,
            "module": xid.module,
            "name": xid.name,
            "model": xid.model,
            "res_id": xid.res_id,
            "record_name": record_name or xid.display_name,
            "noupdate": xid.noupdate,
            "record_exists": exists,
        }

    def _open_self(self, message=False):
        self.ensure_one()
        if message is not False:
            self.state_message = message
        return {
            "type": "ir.actions.act_window",
            "name": _("External ID Finder"),
            "res_model": self._name,
            "view_mode": "form",
            "res_id": self.id,
            "target": "current",
        }

    def action_find(self):
        self.ensure_one()
        self._clear_lines()
        IrModelData = self.env["ir.model.data"].sudo()

        if self.search_mode == "xmlid":
            xml_id = (self.xml_id or "").strip()
            if not xml_id:
                raise UserError(_("Enter a full XML ID, for example base.main_company."))
            if "." not in xml_id:
                raise UserError(_("A full XML ID must contain a module prefix and a name."))
            module, name = xml_id.split(".", 1)
            xids = IrModelData.search([("module", "=", module), ("name", "=", name)])
            if not xids:
                return self._open_self(_("No external identifier found for %s.") % xml_id)

        elif self.search_mode == "record":
            record = self._ensure_model_record()
            xids = IrModelData.search([("model", "=", record._name), ("res_id", "=", record.id)])
            if not xids:
                return self._open_self(
                    _("Record %(model)s,%(res_id)s has no external identifier.")
                    % {"model": record._name, "res_id": record.id}
                )

        else:
            query = (self.text_query or "").strip()
            if not query:
                raise UserError(_("Enter text to search."))
            xids = IrModelData.browse()
            if "." in query:
                module_query, name_query = query.split(".", 1)
                xids |= IrModelData.search(
                    [
                        ("module", "ilike", module_query),
                        ("name", "ilike", name_query),
                    ],
                    limit=200,
                )

            xids |= IrModelData.search(
                [
                    "|",
                    "|",
                    ("module", "ilike", query),
                    ("name", "ilike", query),
                    ("model", "ilike", query),
                ],
                limit=200,
            )
            if not xids:
                return self._open_self(_("No external identifiers matched %s.") % query)

        self.line_ids = [(0, 0, self._line_values_from_xid(xid)) for xid in xids]
        return self._open_self(_("%s external identifier(s) found.") % len(xids))

    def action_create_external_id(self):
        self.ensure_one()
        record = self._ensure_model_record()
        module = (self.create_module or "").strip()
        name = (self.create_name or "").strip()

        if not module or not name:
            raise UserError(_("Enter both Module Prefix and Identifier Name."))
        if " " in module or " " in name or "." in module:
            raise UserError(_("Module Prefix and Identifier Name cannot contain spaces. Prefix cannot contain dots."))

        IrModelData = self.env["ir.model.data"].sudo()
        existing = IrModelData.search([("module", "=", module), ("name", "=", name)], limit=1)
        if existing:
            raise ValidationError(
                _("The external identifier %(xmlid)s already exists.")
                % {"xmlid": existing.complete_name}
            )

        IrModelData.create(
            {
                "module": module,
                "name": name,
                "model": record._name,
                "res_id": record.id,
                "noupdate": self.create_noupdate,
            }
        )
        self.search_mode = "record"
        return self.action_find()


class MdxExternalIdFinderLine(models.TransientModel):
    _name = "mdx.external.id.finder.line"
    _description = "External ID Finder Result"
    _order = "module, model, name"

    @api.model
    def _selection_target_model(self):
        return [
            (model.model, model.name)
            for model in self.env["ir.model"].sudo().search([("transient", "=", False)])
        ]

    wizard_id = fields.Many2one(
        "mdx.external.id.finder",
        required=True,
        ondelete="cascade",
    )
    external_id_id = fields.Many2one("ir.model.data", string="External Identifier", readonly=True)
    xml_id = fields.Char(string="XML ID", readonly=True)
    module = fields.Char(readonly=True)
    name = fields.Char(readonly=True)
    model = fields.Char(readonly=True)
    res_id = fields.Integer(string="Database ID", readonly=True)
    record_name = fields.Char(readonly=True)
    noupdate = fields.Boolean(string="Non Updatable", readonly=True)
    record_exists = fields.Boolean(readonly=True)
    record_ref = fields.Reference(
        string="Record",
        selection="_selection_target_model",
        compute="_compute_record_ref",
    )

    @api.depends("model", "res_id", "record_exists")
    def _compute_record_ref(self):
        for line in self:
            if line.record_exists and line.model and line.res_id:
                line.record_ref = "%s,%s" % (line.model, line.res_id)
            else:
                line.record_ref = False

    def action_open_record(self):
        self.ensure_one()
        if not self.record_exists or self.model not in self.env:
            raise UserError(_("The target record is missing or its model is unavailable."))
        return {
            "type": "ir.actions.act_window",
            "name": self.record_name or self.xml_id,
            "res_model": self.model,
            "res_id": self.res_id,
            "view_mode": "form",
            "target": "current",
        }

    def action_open_external_id(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.xml_id,
            "res_model": "ir.model.data",
            "res_id": self.external_id_id.id,
            "view_mode": "form",
            "target": "current",
        }
