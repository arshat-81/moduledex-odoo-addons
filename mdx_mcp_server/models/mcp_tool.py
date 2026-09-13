"""The tool registry, and the implementations behind it.

Every tool an agent can call is a record rather than a hard-coded handler, so an
administrator can switch one off, restrict it to a group, cap how many rows it
may touch, or require a human to approve it - without touching code. That is the
whole difference between "an MCP server" and something you would point at a
production database.

Two rules hold throughout:

* **The key's user is the security boundary.** Every tool runs as the user the
  API key belongs to, never sudo, so Odoo's own access rules and record rules
  apply to each call exactly as they would in the web client. This module can
  only ever take access away, never add it.
* **Masking happens on the way out.** Fields listed as sensitive are replaced
  before the result leaves the server, so a model that asks for
  ``ir.config_parameter.value`` gets a mask instead of your API keys, whatever
  its access rights would otherwise allow.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import safe_eval

_logger = logging.getLogger(__name__)

# Replaced in any result. The defaults are the places Odoo itself keeps
# credentials; administrators can add their own on the settings page.
DEFAULT_MASKED = (
    "ir.config_parameter.value",
    "res.users.password",
    "ir.mail_server.smtp_pass",
    "res.users.apikeys.key",
)
MASK = "•••••••• (masked)"

OPERATIONS = [
    ("search", "Search records"),
    ("read", "Read records"),
    ("read_group", "Aggregate records"),
    ("fields", "Describe fields"),
    ("models", "List models"),
    ("create", "Create records"),
    ("write", "Update records"),
    ("unlink", "Delete records"),
]
WRITE_OPERATIONS = ("create", "write", "unlink")


class MdxMcpTool(models.Model):
    _name = "mdx.mcp.tool"
    _description = "MCP Tool"
    _order = "sequence, name"

    name = fields.Char(
        required=True, help="The tool name the model sees. Lower case, no spaces.")
    title = fields.Char(help="Human-readable name shown by the client.")
    description = fields.Text(
        required=True,
        help="The model chooses tools from this text, so it is worth writing carefully. "
             "Say what the tool does and when not to use it.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    operation = fields.Selection(OPERATIONS, required=True)
    is_write = fields.Boolean(compute="_compute_is_write", store=True)

    model_ids = fields.Many2many(
        "ir.model", string="Allowed Models",
        help="Leave empty to allow any model the calling user can already reach. Naming "
             "models here narrows the tool further, it never widens it.")
    group_ids = fields.Many2many(
        "res.groups", string="Restrict to Groups",
        help="Only keys belonging to a user in these groups may call this tool. Empty "
             "means any user with a key for this server.")
    max_records = fields.Integer(
        default=200,
        help="Upper bound on records returned or modified in one call. Keeps a vague "
             "question from dragging the whole table through an LLM.")
    requires_approval = fields.Boolean(
        help="The call is parked for a human to approve instead of running. The agent is "
             "told it is pending, which it can report back to the person who asked.")

    call_count = fields.Integer(compute="_compute_call_count")

    _name_uniq = models.Constraint("unique(name)", "Tool names must be unique.")

    @api.depends("operation")
    def _compute_is_write(self):
        for tool in self:
            tool.is_write = tool.operation in WRITE_OPERATIONS

    def _compute_call_count(self):
        data = self.env["mdx.mcp.call"]._read_group(
            [("tool_id", "in", self.ids)], groupby=["tool_id"], aggregates=["__count"])
        counts = {tool.id: count for tool, count in data}
        for tool in self:
            tool.call_count = counts.get(tool.id, 0)

    @api.constrains("name")
    def _check_name(self):
        for tool in self:
            if not tool.name or not tool.name.replace("_", "").isalnum():
                raise ValidationError(_(
                    "'%s' is not a usable tool name. Use letters, digits and underscores.",
                    tool.name))

    def action_view_calls(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window", "res_model": "mdx.mcp.call",
            "name": _("Calls to %s", self.name), "view_mode": "list,form",
            "domain": [("tool_id", "=", self.id)],
        }

    # ------------------------------------------------------------------
    # exposure
    # ------------------------------------------------------------------
    def _callable_by(self, user):
        """Tools this user's key may see and invoke."""
        return self.filtered(
            lambda t: not t.group_ids or (t.group_ids & user.group_ids.all_implied_ids))

    def _schema(self):
        """JSON Schema for the tool's arguments, per the MCP Tool object."""
        self.ensure_one()
        model_prop = {
            "type": "string",
            "description": "Technical model name, for example res.partner.",
        }
        if self.model_ids:
            model_prop["enum"] = sorted(self.model_ids.mapped("model"))

        if self.operation == "models":
            return {"type": "object", "properties": {
                "contains": {"type": "string",
                             "description": "Optional filter on the model name."}}}
        if self.operation == "fields":
            return {"type": "object", "properties": {"model": model_prop},
                    "required": ["model"]}
        if self.operation == "search":
            return {"type": "object", "properties": {
                "model": model_prop,
                "domain": {"type": "array",
                           "description": "Odoo domain, e.g. [[\"name\",\"ilike\",\"acme\"]]. "
                                          "Empty list means all records the user may see."},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "Fields to return. Keep it short."},
                "limit": {"type": "integer"},
                "order": {"type": "string"},
            }, "required": ["model"]}
        if self.operation == "read":
            return {"type": "object", "properties": {
                "model": model_prop,
                "ids": {"type": "array", "items": {"type": "integer"}},
                "fields": {"type": "array", "items": {"type": "string"}},
            }, "required": ["model", "ids"]}
        if self.operation == "read_group":
            return {"type": "object", "properties": {
                "model": model_prop,
                "domain": {"type": "array"},
                "groupby": {"type": "array", "items": {"type": "string"}},
                "aggregates": {"type": "array", "items": {"type": "string"},
                               "description": "e.g. [\"__count\", \"amount_total:sum\"]"},
            }, "required": ["model", "groupby"]}
        if self.operation == "create":
            return {"type": "object", "properties": {
                "model": model_prop,
                "values": {"type": "object", "description": "Field values for the new record."},
                "dry_run": {"type": "boolean",
                            "description": "Report what would be created without creating it."},
            }, "required": ["model", "values"]}
        if self.operation == "write":
            return {"type": "object", "properties": {
                "model": model_prop,
                "ids": {"type": "array", "items": {"type": "integer"}},
                "values": {"type": "object"},
                "dry_run": {"type": "boolean",
                            "description": "Report the before/after without writing."},
            }, "required": ["model", "ids", "values"]}
        if self.operation == "unlink":
            return {"type": "object", "properties": {
                "model": model_prop,
                "ids": {"type": "array", "items": {"type": "integer"}},
                "dry_run": {"type": "boolean"},
            }, "required": ["model", "ids"]}
        return {"type": "object", "properties": {}}

    def _as_mcp_tool(self):
        """The MCP Tool object for tools/list."""
        self.ensure_one()
        payload = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self._schema(),
        }
        if self.title:
            payload["title"] = self.title
        # Annotations are advisory, but a client that surfaces them gives the
        # person behind the agent a fighting chance of noticing a write.
        payload["annotations"] = {
            "readOnlyHint": not self.is_write,
            "destructiveHint": self.operation == "unlink",
            "idempotentHint": self.operation in ("read", "search", "read_group",
                                                 "fields", "models"),
        }
        return payload

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def _masked_fields(self):
        extra = self.env["ir.config_parameter"].sudo().get_param(
            "mdx_mcp_server.masked_fields", "")
        entries = list(DEFAULT_MASKED) + [e.strip() for e in extra.split(",") if e.strip()]
        masked = {}
        for entry in entries:
            if "." in entry:
                model_name, _sep, field_name = entry.rpartition(".")
                masked.setdefault(model_name, set()).add(field_name)
        return masked

    def _mask(self, model_name, rows):
        names = self._masked_fields().get(model_name)
        if not names:
            return rows
        for row in rows:
            for field_name in names & set(row):
                if row[field_name]:
                    row[field_name] = MASK
        return rows

    def _resolve_model(self, env, model_name):
        if not model_name:
            raise UserError(_("This tool needs a 'model' argument."))
        if self.model_ids and model_name not in self.model_ids.mapped("model"):
            raise AccessError(_(
                "%(tool)s is not allowed to touch %(model)s.",
                tool=self.name, model=model_name))
        if model_name not in env:
            raise UserError(_("There is no model called %s.", model_name))
        return env[model_name]

    def _limit(self, requested):
        cap = self.max_records or 200
        if not requested or requested > cap:
            return cap
        return requested

    def _validate(self, arguments):
        """Hold the call to the schema the tool advertised.

        Worth doing properly, because the failure it prevents is the quiet kind.
        A model that guesses ``group_by`` where the schema says ``groupby`` would
        otherwise have the argument ignored and get a confident, meaningless
        answer back - one group, no grouping - and report it to the user as
        fact. Naming the accepted arguments instead lets it correct itself on
        the next call, which is the whole point of returning readable errors.
        """
        schema = self._schema()
        allowed = set(schema.get("properties") or {})
        unknown = sorted(set(arguments) - allowed - {"dry_run"})
        if unknown:
            raise UserError(_(
                "%(tool)s does not take %(unknown)s. It accepts: %(allowed)s.",
                tool=self.name, unknown=", ".join(unknown),
                allowed=", ".join(sorted(allowed)) or _("no arguments")))
        missing = [name for name in schema.get("required") or []
                   if arguments.get(name) in (None, "", [], {})]
        if missing:
            raise UserError(_(
                "%(tool)s needs %(missing)s.",
                tool=self.name, missing=", ".join(missing)))

    def execute(self, env, arguments):
        """Run the tool as ``env``'s user. Returns (text, structured, touched)."""
        self.ensure_one()
        arguments = arguments or {}
        handler = getattr(self, "_run_%s" % self.operation, None)
        if handler is None:
            raise UserError(_("%s is not an operation this server knows.", self.operation))
        self._validate(arguments)
        return handler(env, arguments)

    # -- read side ----------------------------------------------------
    def _run_models(self, env, args):
        contains = (args.get("contains") or "").strip()
        domain = [("transient", "=", False)]
        if contains:
            domain.append(("model", "ilike", contains))
        # ir.model is the registry, not business data, and in 19 an ordinary
        # internal user cannot read it at all - so a literal read here makes the
        # one tool an agent is told to call first fail for every non-admin key.
        # The list is therefore built in sudo and then cut down to the models
        # this user may actually read, which is the answer the tool promises:
        # the user's own permissions are still the only thing that decides.
        records = env["ir.model"].sudo().search(
            domain, limit=None, order="model")
        allowed = env["ir.model.access"]._get_allowed_models("read")
        rows = [{"model": r.model, "name": r.name}
                for r in records if r.model in allowed][:self._limit(None)]
        return (_("%s model(s) this user can read.", len(rows)), {"models": rows}, len(rows))

    def _run_fields(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        # fields_get reads the registry and performs no access check of its own,
        # so without this a tool meant for discovery would happily describe the
        # shape of models the caller cannot read. Answer for the models it can.
        model.check_access("read")
        info = model.fields_get(attributes=["string", "type", "relation", "required",
                                            "readonly", "help"])
        masked = self._masked_fields().get(model._name, set())
        rows = []
        for name, spec in sorted(info.items()):
            rows.append({
                "name": name, "label": spec.get("string"), "type": spec.get("type"),
                "relation": spec.get("relation"), "required": spec.get("required", False),
                "readonly": spec.get("readonly", False),
                "masked": name in masked,
            })
        return (_("%(n)s field(s) on %(m)s.", n=len(rows), m=model._name),
                {"model": model._name, "fields": rows}, len(rows))

    def _run_search(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        domain = args.get("domain") or []
        if not isinstance(domain, list):
            raise UserError(_("'domain' must be a list."))
        field_names = args.get("fields") or ["display_name"]
        records = model.search(domain, limit=self._limit(args.get("limit")),
                               order=args.get("order") or None)
        rows = records.read(field_names)
        rows = self._mask(model._name, rows)
        return (_("%(n)s record(s) from %(m)s.", n=len(rows), m=model._name),
                {"model": model._name, "records": rows}, len(rows))

    def _run_read(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        ids = args.get("ids") or []
        rows = model.browse(ids[:self._limit(None)]).exists().read(
            args.get("fields") or ["display_name"])
        rows = self._mask(model._name, rows)
        return (_("%(n)s record(s) from %(m)s.", n=len(rows), m=model._name),
                {"model": model._name, "records": rows}, len(rows))

    def _run_read_group(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        groupby = args.get("groupby") or []
        aggregates = args.get("aggregates") or ["__count"]
        data = model._read_group(args.get("domain") or [], groupby=groupby,
                                 aggregates=aggregates, limit=self._limit(None))
        rows = []
        for entry in data:
            keys = list(entry[:len(groupby)])
            values = list(entry[len(groupby):])
            row = {}
            for name, value in zip(groupby, keys):
                row[name] = value.display_name if hasattr(value, "display_name") else value
            for name, value in zip(aggregates, values):
                row[name] = value
            rows.append(row)
        return (_("%s group(s).", len(rows)), {"model": model._name, "groups": rows}, len(rows))

    # -- write side ---------------------------------------------------
    def _run_create(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        values = args.get("values") or {}
        if args.get("dry_run"):
            return (_("Dry run: would create one %s. Nothing was written.", model._name),
                    {"dry_run": True, "model": model._name, "values": values}, 0)
        record = model.create(values)
        return (_("Created %(m)s #%(i)s.", m=model._name, i=record.id),
                {"model": model._name, "id": record.id,
                 "display_name": record.display_name}, 1)

    def _run_write(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        ids = (args.get("ids") or [])[:self._limit(None)]
        values = args.get("values") or {}
        records = model.browse(ids).exists()
        if not records:
            raise UserError(_("None of those ids exist, or they are not visible to you."))
        before = records.read(list(values)) if values else []
        if args.get("dry_run"):
            return (_("Dry run: would update %(n)s record(s) of %(m)s. Nothing was written.",
                      n=len(records), m=model._name),
                    {"dry_run": True, "model": model._name, "before": before,
                     "values": values}, 0)
        records.write(values)
        return (_("Updated %(n)s record(s) of %(m)s.", n=len(records), m=model._name),
                {"model": model._name, "ids": records.ids, "before": before,
                 "values": values}, len(records))

    def _run_unlink(self, env, args):
        model = self._resolve_model(env, args.get("model"))
        ids = (args.get("ids") or [])[:self._limit(None)]
        records = model.browse(ids).exists()
        if not records:
            raise UserError(_("None of those ids exist, or they are not visible to you."))
        names = records.mapped("display_name")
        if args.get("dry_run"):
            return (_("Dry run: would delete %(n)s record(s) of %(m)s. Nothing was deleted.",
                      n=len(records), m=model._name),
                    {"dry_run": True, "model": model._name, "records": names}, 0)
        count = len(records)
        records.unlink()
        return (_("Deleted %(n)s record(s) of %(m)s.", n=count, m=model._name),
                {"model": model._name, "deleted": names}, count)
