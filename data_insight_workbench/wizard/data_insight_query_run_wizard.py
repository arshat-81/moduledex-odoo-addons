import json

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class DataInsightQueryRunWizard(models.TransientModel):
    _name = "data.insight.query.run.wizard"
    _description = "Run Data Insight Query"

    query_id = fields.Many2one("data.insight.query", required=True, readonly=True)
    mode = fields.Selection(
        [
            ("run", "Run"),
            ("explain", "Explain"),
        ],
        default="run",
        required=True,
    )
    sql_text = fields.Text(required=True)
    parameter_json = fields.Text(default="{}")
    limit = fields.Integer(default=0, required=True)
    timeout_ms = fields.Integer(default=0, required=True)
    acknowledge = fields.Boolean(string="I understand this query will be executed against the live database")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        query = self.env["data.insight.query"].browse(values.get("query_id") or self.env.context.get("default_query_id"))
        if query:
            query._check_can_execute()
            values.update(
                {
                    "query_id": query.id,
                    "sql_text": query.sql_text,
                    "limit": query.default_limit,
                    "timeout_ms": query.timeout_ms,
                    "parameter_json": self._default_parameter_json(query),
                }
            )
        return values

    @api.onchange("query_id")
    def _onchange_query_id(self):
        if self.query_id:
            self.sql_text = self.query_id.sql_text
            self.limit = self.query_id.default_limit
            self.timeout_ms = self.query_id.timeout_ms
            self.parameter_json = self._default_parameter_json(self.query_id)

    def action_validate_only(self):
        self.ensure_one()
        if not self.query_id._has_full_sql_access():
            self.query_id._guard_sql(self.sql_text)
        self.query_id._coerce_parameters(self.parameter_json, sql_text=self.sql_text)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Validation complete"),
                "message": _("SQL guard and parameters are valid."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_execute(self):
        self.ensure_one()
        if not self.acknowledge:
            raise UserError(_("Confirm that you understand the query will run against the live database."))
        params = self.query_id._coerce_parameters(self.parameter_json, sql_text=self.sql_text)
        execution = self.query_id._execute_query(
            self.sql_text,
            params,
            self.limit,
            self.timeout_ms,
            mode=self.mode,
        )
        return execution.action_open_execution()

    @api.model
    def _default_parameter_json(self, query):
        payload = {}
        for line in query.parameter_ids:
            if line.default_value not in (False, None, ""):
                payload[line.name] = line.default_value
            else:
                payload[line.name] = ""
        return json.dumps(payload, indent=2, sort_keys=True)
