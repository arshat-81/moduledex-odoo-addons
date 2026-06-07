from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval, time


OPERATIONS = [
    ("read", "Read"),
    ("write", "Write"),
    ("create", "Create"),
    ("unlink", "Delete"),
]

STATUS = [
    ("allowed", "Allowed"),
    ("blocked_acl", "Blocked by ACL"),
    ("blocked_rule", "Blocked by Record Rule"),
    ("conditional", "Conditional"),
    ("missing_record", "Missing Record"),
    ("error", "Error"),
]

RULE_RESULT = [
    ("pass", "Pass"),
    ("fail", "Fail"),
    ("not_applicable", "Not Applicable"),
    ("not_checked", "Not Checked"),
    ("error", "Error"),
]


class MdxSecuritySimulator(models.Model):
    _name = "mdx.security.simulator"
    _description = "Security Simulator"
    _rec_name = "name"
    _order = "write_date desc, id desc"

    name = fields.Char(
        compute="_compute_name",
        store=True,
        readonly=True,
    )
    user_id = fields.Many2one(
        "res.users",
        string="Simulated User",
        required=True,
        default=lambda self: self.env.user,
    )
    company_ids = fields.Many2many(
        "res.company",
        string="Allowed Companies",
        default=lambda self: self.env.user.company_ids,
        help="Companies passed through allowed_company_ids during the simulation.",
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        domain=[("transient", "=", False)],
        ondelete="cascade",
    )
    model = fields.Char(related="model_id.model", readonly=True)
    record_id = fields.Integer(string="Record ID")
    target_record = fields.Reference(
        string="Target Record",
        selection="_selection_target_model",
        compute="_compute_target_record",
    )
    domain_text = fields.Text(
        string="Optional Search Domain",
        default="[]",
        help="Python domain evaluated with user, uid, time, company_id, and company_ids.",
    )
    active_test = fields.Boolean(default=True)
    sample_limit = fields.Integer(default=80)
    operation_line_ids = fields.One2many(
        "mdx.security.simulator.operation.line",
        "simulation_id",
        string="Operation Results",
    )
    access_line_ids = fields.One2many(
        "mdx.security.simulator.access.line",
        "simulation_id",
        string="Access Rights",
    )
    rule_line_ids = fields.One2many(
        "mdx.security.simulator.rule.line",
        "simulation_id",
        string="Record Rules",
    )
    group_line_ids = fields.One2many(
        "mdx.security.simulator.group.line",
        "simulation_id",
        string="User Groups",
    )
    field_line_ids = fields.One2many(
        "mdx.security.simulator.field.line",
        "simulation_id",
        string="Restricted Fields",
    )
    action_line_ids = fields.One2many(
        "mdx.security.simulator.action.line",
        "simulation_id",
        string="Menus and Actions",
    )
    summary = fields.Text(readonly=True)
    summary_user = fields.Char(readonly=True)
    summary_model = fields.Char(readonly=True)
    summary_companies = fields.Char(readonly=True)
    summary_allowed = fields.Char(readonly=True)
    summary_blocked = fields.Char(readonly=True)
    summary_acl_count = fields.Integer(readonly=True)
    summary_rule_count = fields.Integer(readonly=True)
    last_run_at = fields.Datetime(readonly=True)
    state_message = fields.Char(readonly=True)

    @api.model
    def _selection_target_model(self):
        return [
            (model.model, model.name)
            for model in self.env["ir.model"].sudo().search([("transient", "=", False)])
        ]

    @api.depends("model", "record_id")
    def _compute_target_record(self):
        for wizard in self:
            if wizard.model and wizard.record_id and wizard.model in self.env:
                record = self.env[wizard.model].sudo().browse(wizard.record_id).exists()
                wizard.target_record = "%s,%s" % (wizard.model, record.id) if record else False
            else:
                wizard.target_record = False

    @api.onchange("user_id")
    def _onchange_user_id(self):
        if self.user_id:
            self.company_ids = self.user_id.company_ids

    @api.onchange("model_id")
    def _onchange_model_id(self):
        self.record_id = 0

    @api.depends("model_id", "user_id")
    def _compute_name(self):
        for simulation in self:
            if simulation.model_id and simulation.user_id:
                simulation.name = _("%(model)s as %(user)s") % {
                    "model": simulation.model_id.display_name,
                    "user": simulation.user_id.display_name,
                }
            elif simulation.model_id:
                simulation.name = simulation.model_id.display_name
            else:
                simulation.name = _("New Security Simulation")

    def _clear_lines(self):
        self.operation_line_ids.unlink()
        self.access_line_ids.unlink()
        self.rule_line_ids.unlink()
        self.group_line_ids.unlink()
        self.field_line_ids.unlink()
        self.action_line_ids.unlink()

    def _selected_companies(self):
        self.ensure_one()
        companies = self.company_ids or self.user_id.company_ids
        invalid = companies - self.user_id.company_ids
        if invalid:
            raise UserError(
                _("The simulated user is not allowed in these companies: %s")
                % ", ".join(invalid.mapped("display_name"))
            )
        if not companies and self.user_id.company_id:
            companies = self.user_id.company_id
        return companies

    def _simulation_context(self):
        self.ensure_one()
        companies = self._selected_companies()
        context = dict(self.env.context)
        context["active_test"] = self.active_test
        if companies:
            context["allowed_company_ids"] = companies.ids
        return context

    def _eval_context(self):
        self.ensure_one()
        companies = self._selected_companies()
        company = companies[:1] or self.user_id.company_id
        return {
            "user": self.user_id.with_context({}),
            "uid": self.user_id.id,
            "time": time,
            "company_id": company.id if company else False,
            "company_ids": companies.ids,
        }

    def _parse_domain(self):
        self.ensure_one()
        domain_text = (self.domain_text or "[]").strip() or "[]"
        try:
            domain = safe_eval(domain_text, self._eval_context())
        except Exception as error:
            raise UserError(_("The search domain could not be evaluated: %s") % error)

        if isinstance(domain, tuple):
            domain = list(domain)
        if not isinstance(domain, list):
            raise UserError(_("The optional search domain must evaluate to a list domain."))

        try:
            domain = Domain(domain)
            domain.validate(self.env[self.model].sudo())
        except Exception as error:
            raise UserError(_("The search domain is not valid for model %s: %s") % (self.model, error))
        return domain

    def _simulated_model(self):
        self.ensure_one()
        if not self.model or self.model not in self.env:
            raise UserError(_("Choose an available model."))
        return self.env[self.model].with_user(self.user_id).with_context(self._simulation_context())

    def _sudo_model(self):
        self.ensure_one()
        return self.env[self.model].sudo().with_context(self._simulation_context())

    def _rule_model(self):
        self.ensure_one()
        return self.env["ir.rule"].with_user(self.user_id).with_context(self._simulation_context())

    def _record_for_check(self):
        self.ensure_one()
        if not self.record_id:
            return self.env[self.model].sudo().browse()
        return self._sudo_model().browse(self.record_id).exists()

    def action_simulate(self):
        self.ensure_one()
        self._clear_lines()
        if self.sample_limit <= 0:
            raise UserError(_("Sample Limit must be greater than zero."))

        domain = self._parse_domain()
        model = self._simulated_model()
        sudo_model = self._sudo_model()
        record = self._record_for_check()
        if self.record_id and not record:
            self.state_message = _("Record %s,%s does not exist.") % (self.model, self.record_id)

        operation_values = self._build_operation_values(model, sudo_model, domain, record)
        access_values = self._build_access_values()
        rule_values = self._build_rule_values(record)
        group_values = self._build_group_values()
        field_values = self._build_field_values()
        action_values = self._build_action_values()
        summary_values = self._build_summary_values(operation_values, access_values, rule_values)

        self.write(
            {
                "operation_line_ids": [(0, 0, values) for values in operation_values],
                "access_line_ids": [(0, 0, values) for values in access_values],
                "rule_line_ids": [(0, 0, values) for values in rule_values],
                "group_line_ids": [(0, 0, values) for values in group_values],
                "field_line_ids": [(0, 0, values) for values in field_values],
                "action_line_ids": [(0, 0, values) for values in action_values],
                "summary": self._build_summary(summary_values),
                **summary_values,
                "last_run_at": fields.Datetime.now(),
                "state_message": _("Simulation completed for %s as %s.")
                % (self.model_id.display_name, self.user_id.display_name),
            }
        )
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def action_clear(self):
        self.ensure_one()
        self._clear_lines()
        self.summary = False
        self.summary_user = False
        self.summary_model = False
        self.summary_companies = False
        self.summary_allowed = False
        self.summary_blocked = False
        self.summary_acl_count = 0
        self.summary_rule_count = 0
        self.last_run_at = False
        self.state_message = False
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def _build_operation_values(self, model, sudo_model, domain, record):
        values = []
        total_count = self._safe_search_count(sudo_model, domain)
        sample = self._safe_search(sudo_model, domain, self.sample_limit)
        for operation, label in OPERATIONS:
            values.append(
                self._operation_value(
                    model,
                    sudo_model,
                    operation,
                    label,
                    domain,
                    record,
                    total_count,
                    sample,
                )
            )
        return values

    def _operation_value(self, model, sudo_model, operation, label, domain, record, total_count, sample):
        acl_allowed, acl_message = self._check_acl(model, operation)
        rule_domain, rule_message = self._compute_rule_domain(operation)
        rule_allowed = False
        status = "conditional"
        final_allowed = False
        record_checked = bool(record)
        visible_count = 0
        sample_size = len(sample)
        sample_allowed_count = 0
        sample_allowed_ids = ""
        message_parts = []

        if acl_message:
            message_parts.append(acl_message)

        if not acl_allowed:
            status = "blocked_acl"
            message_parts.append(_("No active model access rule grants %s access.") % label.lower())
        elif self.record_id and not record:
            status = "missing_record"
            message_parts.append(_("The selected record does not exist; record rules could not be checked."))
        else:
            if record:
                rule_allowed, rule_check_message = self._check_record_rule(model, operation, record)
                final_allowed = acl_allowed and rule_allowed
                status = "allowed" if final_allowed else "blocked_rule"
                message_parts.append(rule_check_message)
            elif operation == "create":
                final_allowed = acl_allowed
                status = "allowed"
                message_parts.append(
                    _("Create is allowed by model ACL. Record rule domains are informational until new values exist.")
                )
            else:
                final_allowed = acl_allowed
                status = "conditional"
                message_parts.append(
                    _("No record was selected. ACL allows the operation, but record rules must be checked on records.")
                )

            sample_allowed = self._safe_filter_rules(model, sample, operation) if sample else model.browse()
            sample_allowed_count = len(sample_allowed)
            sample_allowed_ids = ", ".join(str(record_id) for record_id in sample_allowed.ids[:25])
            if operation == "read" and acl_allowed:
                visible_count = self._safe_search_count(model, domain)

        if rule_message:
            message_parts.append(rule_message)

        return {
            "operation": operation,
            "acl_allowed": acl_allowed,
            "record_rule_allowed": rule_allowed,
            "final_allowed": final_allowed,
            "status": status,
            "record_checked": record_checked,
            "total_count": total_count,
            "visible_count": visible_count,
            "sample_size": sample_size,
            "sample_allowed_count": sample_allowed_count,
            "sample_allowed_ids": sample_allowed_ids,
            "rule_domain": rule_domain,
            "message": "\n".join(part for part in message_parts if part),
        }

    def _check_acl(self, model, operation):
        try:
            return bool(model.has_access(operation)), False
        except Exception as error:
            return False, _("ACL check raised an error: %s") % error

    def _compute_rule_domain(self, operation):
        try:
            domain = self._rule_model()._compute_domain(self.model, operation)
            return repr(list(domain)), False
        except Exception as error:
            return "[]", _("Record rule domain could not be computed for %s: %s") % (operation, error)

    def _check_record_rule(self, model, operation, record):
        try:
            model.browse(record.id).check_access(operation)
            return True, _("The selected record passes %s record rules.") % operation
        except AccessError as error:
            return False, _("The selected record is blocked by %s record rules:\n%s") % (operation, error)
        except MissingError as error:
            return False, _("The selected record is missing:\n%s") % error
        except Exception as error:
            return False, _("Record rule check raised an error:\n%s") % error

    def _safe_search_count(self, model, domain):
        try:
            return model.search_count(domain)
        except Exception:
            return 0

    def _safe_search(self, model, domain, limit):
        try:
            return model.search(domain, limit=limit)
        except Exception:
            return model.browse()

    def _safe_filter_rules(self, model, records, operation):
        try:
            return model.browse(records.ids)._filtered_access(operation)
        except Exception:
            return model.browse()

    def _build_access_values(self):
        user_groups = self.user_id.all_group_ids
        accesses = self.env["ir.model.access"].sudo().with_context(active_test=False).search(
            [("model_id", "=", self.model_id.id)]
        )
        values = []
        for access in accesses:
            applies = bool(access.active and (not access.group_id or access.group_id in user_groups))
            grants = [
                label
                for perm, label in [
                    (access.perm_read, _("Read")),
                    (access.perm_write, _("Write")),
                    (access.perm_create, _("Create")),
                    (access.perm_unlink, _("Delete")),
                ]
                if perm
            ]
            values.append(
                {
                    "access_id": access.id,
                    "name": access.name,
                    "group_id": access.group_id.id,
                    "active": access.active,
                    "global_access": not bool(access.group_id),
                    "applies_to_user": applies,
                    "perm_read": access.perm_read,
                    "perm_write": access.perm_write,
                    "perm_create": access.perm_create,
                    "perm_unlink": access.perm_unlink,
                    "grant_summary": ", ".join(grants) if grants else _("No permissions"),
                }
            )
        return values

    def _build_rule_values(self, record):
        user_groups = self.user_id.all_group_ids
        rules = self.env["ir.rule"].sudo().with_context(active_test=False).search(
            [("model_id", "=", self.model_id.id)]
        )
        values = []
        for rule in rules:
            applies = bool(rule.active and (not rule.groups or rule.groups & user_groups))
            values.append(
                {
                    "rule_id": rule.id,
                    "name": rule.name,
                    "active": rule.active,
                    "global_rule": not bool(rule.groups),
                    "applies_to_user": applies,
                    "group_names": ", ".join(rule.groups.mapped("display_name")),
                    "domain_force": rule.domain_force or "[]",
                    "perm_read": rule.perm_read,
                    "perm_write": rule.perm_write,
                    "perm_create": rule.perm_create,
                    "perm_unlink": rule.perm_unlink,
                    "result_read": self._rule_record_result(rule, "read", record),
                    "result_write": self._rule_record_result(rule, "write", record),
                    "result_create": self._rule_record_result(rule, "create", record),
                    "result_unlink": self._rule_record_result(rule, "unlink", record),
                }
            )
        return values

    def _rule_record_result(self, rule, operation, record):
        if not rule.active or not getattr(rule, "perm_%s" % operation):
            return "not_applicable"
        if rule.groups and not (rule.groups & self.user_id.all_group_ids):
            return "not_applicable"
        if not record:
            return "not_checked"
        try:
            domain = Domain(safe_eval(rule.domain_force, self._rule_model()._eval_context())) if rule.domain_force else Domain.TRUE
            domain = domain & Domain("id", "=", record.id)
            return "pass" if self._sudo_model().search_count(domain) else "fail"
        except Exception:
            return "error"

    def _build_group_values(self):
        accesses = self.env["ir.model.access"].sudo().with_context(active_test=False).search(
            [("model_id", "=", self.model_id.id), ("group_id", "!=", False)]
        )
        rules = self.env["ir.rule"].sudo().with_context(active_test=False).search(
            [("model_id", "=", self.model_id.id), ("groups", "!=", False)]
        )
        acl_groups = accesses.mapped("group_id")
        rule_groups = rules.mapped("groups")
        return [
            {
                "group_id": group.id,
                "category": group.privilege_id.display_name or "Uncategorized",
                "used_by_acl": group in acl_groups,
                "used_by_rule": group in rule_groups,
                "technical_name": self._xmlid_for(group),
            }
            for group in self.user_id.all_group_ids.sorted(
                lambda group: (group.privilege_id.display_name or "", group.name or "")
            )
        ]

    def _build_field_values(self):
        fields_with_groups = self.env["ir.model.fields"].sudo().search(
            [("model_id", "=", self.model_id.id), ("groups", "!=", False)],
            order="name",
        )
        user_groups = self.user_id.all_group_ids
        return [
            {
                "field_id": field.id,
                "name": field.name,
                "field_description": field.field_description,
                "ttype": field.ttype,
                "group_names": ", ".join(field.groups.mapped("display_name")),
                "accessible": bool(field.groups & user_groups),
            }
            for field in fields_with_groups
        ]

    def _build_action_values(self):
        actions = self.env["ir.actions.act_window"].sudo().search(
            [("res_model", "=", self.model)],
            order="name",
        )
        user_groups = self.user_id.all_group_ids
        values = []
        Menu = self.env["ir.ui.menu"].sudo()
        for action in actions:
            menus = Menu.search([("action", "=", "ir.actions.act_window,%s" % action.id)])
            values.append(
                {
                    "action_id": action.id,
                    "name": action.name,
                    "view_mode": action.view_mode,
                    "group_names": ", ".join(action.group_ids.mapped("display_name")),
                    "accessible": bool(not action.group_ids or action.group_ids & user_groups),
                    "menu_count": len(menus),
                    "menu_names": "\n".join(menus.mapped("complete_name")),
                }
            )
        return values

    def _xmlid_for(self, record):
        xmlids = record.get_external_id()
        return xmlids.get(record.id, "")

    def _build_summary_values(self, operation_values, access_values, rule_values):
        allowed = [dict(OPERATIONS).get(line["operation"]) for line in operation_values if line["final_allowed"]]
        blocked = [dict(OPERATIONS).get(line["operation"]) for line in operation_values if not line["final_allowed"]]
        return {
            "summary_user": self.user_id.display_name,
            "summary_model": "%s (%s)" % (self.model_id.display_name, self.model),
            "summary_companies": ", ".join((self.company_ids or self.user_id.company_ids).mapped("display_name")),
            "summary_allowed": ", ".join(allowed) if allowed else _("None"),
            "summary_blocked": ", ".join(blocked) if blocked else _("None"),
            "summary_acl_count": len(access_values),
            "summary_rule_count": len(rule_values),
        }

    def _build_summary(self, values):
        return _(
            "User: %(user)s\nModel: %(model)s\nCompanies: %(companies)s\n"
            "Allowed operations: %(allowed)s\nBlocked or conditional operations: %(blocked)s\n"
            "ACL rows inspected: %(acl_count)s\nRecord rules inspected: %(rule_count)s"
        ) % {
            "user": values["summary_user"],
            "model": values["summary_model"],
            "companies": values["summary_companies"],
            "allowed": values["summary_allowed"],
            "blocked": values["summary_blocked"],
            "acl_count": values["summary_acl_count"],
            "rule_count": values["summary_rule_count"],
        }

    def action_open_user(self):
        self.ensure_one()
        return self._open_res_record("res.users", self.user_id.id, self.user_id.display_name)

    def action_open_model(self):
        self.ensure_one()
        return self._open_res_record("ir.model", self.model_id.id, self.model_id.display_name)

    def action_open_record(self):
        self.ensure_one()
        if not self.record_id or self.model not in self.env:
            raise UserError(_("Select a valid model and record ID first."))
        record = self.env[self.model].sudo().browse(self.record_id).exists()
        if not record:
            raise UserError(_("The selected record does not exist."))
        return self._open_res_record(self.model, record.id, record.display_name)

    def action_open_native_access(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Access Rights"),
            "res_model": "ir.model.access",
            "view_mode": "list,form",
            "domain": [("model_id", "=", self.model_id.id)],
            "target": "current",
        }

    def action_open_native_rules(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Record Rules"),
            "res_model": "ir.rule",
            "view_mode": "list,form",
            "domain": [("model_id", "=", self.model_id.id)],
            "target": "current",
        }

    def _open_res_record(self, model, res_id, name):
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "res_id": res_id,
            "view_mode": "form",
            "target": "current",
        }


class MdxSecuritySimulatorOperationLine(models.Model):
    _name = "mdx.security.simulator.operation.line"
    _description = "Security Simulator Operation Result"
    _order = "id"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    operation = fields.Selection(OPERATIONS, readonly=True)
    status = fields.Selection(STATUS, readonly=True)
    acl_allowed = fields.Boolean(readonly=True)
    record_rule_allowed = fields.Boolean(readonly=True)
    final_allowed = fields.Boolean(readonly=True)
    record_checked = fields.Boolean(readonly=True)
    total_count = fields.Integer(readonly=True)
    visible_count = fields.Integer(readonly=True)
    sample_size = fields.Integer(readonly=True)
    sample_allowed_count = fields.Integer(readonly=True)
    sample_allowed_ids = fields.Char(readonly=True)
    rule_domain = fields.Text(readonly=True)
    message = fields.Text(readonly=True)


class MdxSecuritySimulatorAccessLine(models.Model):
    _name = "mdx.security.simulator.access.line"
    _description = "Security Simulator Access Right"
    _order = "applies_to_user desc, active desc, group_id, name"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    access_id = fields.Many2one("ir.model.access", readonly=True)
    name = fields.Char(readonly=True)
    group_id = fields.Many2one("res.groups", readonly=True)
    active = fields.Boolean(readonly=True)
    global_access = fields.Boolean(readonly=True)
    applies_to_user = fields.Boolean(readonly=True)
    perm_read = fields.Boolean(readonly=True)
    perm_write = fields.Boolean(readonly=True)
    perm_create = fields.Boolean(readonly=True)
    perm_unlink = fields.Boolean(readonly=True)
    grant_summary = fields.Char(readonly=True)

    def action_open_access(self):
        self.ensure_one()
        if not self.access_id:
            raise UserError(_("This line is not linked to an access rule."))
        return {
            "type": "ir.actions.act_window",
            "name": self.access_id.display_name,
            "res_model": "ir.model.access",
            "res_id": self.access_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MdxSecuritySimulatorRuleLine(models.Model):
    _name = "mdx.security.simulator.rule.line"
    _description = "Security Simulator Record Rule"
    _order = "applies_to_user desc, active desc, global_rule desc, name"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    rule_id = fields.Many2one("ir.rule", readonly=True)
    name = fields.Char(readonly=True)
    active = fields.Boolean(readonly=True)
    global_rule = fields.Boolean(readonly=True)
    applies_to_user = fields.Boolean(readonly=True)
    group_names = fields.Char(readonly=True)
    domain_force = fields.Text(readonly=True)
    perm_read = fields.Boolean(readonly=True)
    perm_write = fields.Boolean(readonly=True)
    perm_create = fields.Boolean(readonly=True)
    perm_unlink = fields.Boolean(readonly=True)
    result_read = fields.Selection(RULE_RESULT, readonly=True)
    result_write = fields.Selection(RULE_RESULT, readonly=True)
    result_create = fields.Selection(RULE_RESULT, readonly=True)
    result_unlink = fields.Selection(RULE_RESULT, readonly=True)

    def action_open_rule(self):
        self.ensure_one()
        if not self.rule_id:
            raise UserError(_("This line is not linked to a record rule."))
        return {
            "type": "ir.actions.act_window",
            "name": self.rule_id.display_name,
            "res_model": "ir.rule",
            "res_id": self.rule_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MdxSecuritySimulatorGroupLine(models.Model):
    _name = "mdx.security.simulator.group.line"
    _description = "Security Simulator User Group"
    _order = "category, group_id"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    group_id = fields.Many2one("res.groups", readonly=True)
    category = fields.Char(readonly=True)
    technical_name = fields.Char(readonly=True)
    used_by_acl = fields.Boolean(readonly=True)
    used_by_rule = fields.Boolean(readonly=True)

    def action_open_group(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.group_id.display_name,
            "res_model": "res.groups",
            "res_id": self.group_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MdxSecuritySimulatorFieldLine(models.Model):
    _name = "mdx.security.simulator.field.line"
    _description = "Security Simulator Restricted Field"
    _order = "accessible desc, name"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    field_id = fields.Many2one("ir.model.fields", readonly=True)
    name = fields.Char(readonly=True)
    field_description = fields.Char(readonly=True)
    ttype = fields.Char(readonly=True)
    group_names = fields.Char(readonly=True)
    accessible = fields.Boolean(readonly=True)

    def action_open_field(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.field_id.display_name,
            "res_model": "ir.model.fields",
            "res_id": self.field_id.id,
            "view_mode": "form",
            "target": "current",
        }


class MdxSecuritySimulatorActionLine(models.Model):
    _name = "mdx.security.simulator.action.line"
    _description = "Security Simulator Model Action"
    _order = "accessible desc, name"

    simulation_id = fields.Many2one("mdx.security.simulator", required=True, ondelete="cascade")
    action_id = fields.Many2one("ir.actions.act_window", readonly=True)
    name = fields.Char(readonly=True)
    view_mode = fields.Char(readonly=True)
    group_names = fields.Char(readonly=True)
    accessible = fields.Boolean(readonly=True)
    menu_count = fields.Integer(readonly=True)
    menu_names = fields.Text(readonly=True)

    def action_open_action(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.action_id.display_name,
            "res_model": "ir.actions.act_window",
            "res_id": self.action_id.id,
            "view_mode": "form",
            "target": "current",
        }
