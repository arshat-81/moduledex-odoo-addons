import base64
import csv
import hashlib
import io
import json
import logging
import re
import time
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import quote

from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import format_datetime


_logger = logging.getLogger(__name__)

PARAMETER_RE = re.compile(r"%\(([^)]+)\)s")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOCKING_RE = re.compile(r"\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", re.IGNORECASE)

ALLOWED_START_KEYWORDS = {"select", "with", "explain", "show"}
EXPLAINABLE_START_KEYWORDS = {"select", "with", "insert", "update", "delete", "merge", "values", "execute", "declare"}
FORBIDDEN_KEYWORDS = {
    "alter",
    "analyze",
    "attach",
    "call",
    "cluster",
    "comment",
    "copy",
    "create",
    "deallocate",
    "delete",
    "detach",
    "do",
    "drop",
    "execute",
    "grant",
    "insert",
    "listen",
    "lock",
    "merge",
    "notify",
    "prepare",
    "reassign",
    "refresh",
    "reindex",
    "reset",
    "revoke",
    "security",
    "set",
    "truncate",
    "unlisten",
    "update",
    "vacuum",
}
FORBIDDEN_FUNCTION_RE = re.compile(
    r"\b(pg_sleep|pg_advisory_lock|pg_read_file|pg_ls_dir|pg_stat_file|lo_import|lo_export|nextval|setval|set_config|dblink)\s*\(",
    re.IGNORECASE,
)


def _execution_timezone(env):
    return (
        env.context.get("tz")
        or env.user.tz
        or env.user.partner_id.tz
        or env.company.partner_id.tz
        or "UTC"
    )


def _format_execution_datetime(env, value):
    timestamp = fields.Datetime.to_datetime(value)
    if not timestamp:
        return ""

    timezone = _execution_timezone(env)
    formatted = format_datetime(env, timestamp, tz=timezone, dt_format=False)
    localized = fields.Datetime.context_timestamp(env.user.with_context(tz=timezone), timestamp)
    timezone_label = localized.tzname()
    return "%s %s" % (formatted, timezone_label) if timezone_label else formatted


class _ReadOnlyRollback(Exception):
    """Internal sentinel used to rollback query-side effects after rows are fetched."""


class DataInsightQuery(models.Model):
    _name = "data.insight.query"
    _description = "Data Insight Query"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "write_date desc, id desc"

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("reviewed", "Reviewed"),
            ("approved", "Approved"),
            ("archived", "Archived"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )
    category = fields.Selection(
        [
            ("audit", "Audit"),
            ("operations", "Operations"),
            ("reporting", "Reporting"),
            ("technical", "Technical"),
        ],
        default="reporting",
        required=True,
    )
    sql_text = fields.Text(required=True, tracking=True)
    description = fields.Text()
    owner_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)

    default_limit = fields.Integer(
        default=0,
        required=True,
        help="Maximum rows to fetch by default. Use 0 for no row limit.",
    )
    max_limit = fields.Integer(
        default=0,
        required=True,
        help="Hard cap applied to requested row limits. Use 0 for no cap.",
    )
    timeout_ms = fields.Integer(
        default=0,
        required=True,
        help="PostgreSQL statement timeout in milliseconds. Use 0 for no timeout.",
    )
    cell_max_chars = fields.Integer(default=500, required=True)

    parameter_ids = fields.One2many("data.insight.query.parameter", "query_id", string="Parameters")
    execution_ids = fields.One2many("data.insight.execution", "query_id", string="Executions")
    execution_count = fields.Integer(compute="_compute_execution_count")
    last_execution_id = fields.Many2one("data.insight.execution", copy=False, readonly=True)
    last_status = fields.Selection(related="last_execution_id.status", readonly=True, store=True)
    last_row_count = fields.Integer(related="last_execution_id.row_count", readonly=True, store=True)
    last_duration_ms = fields.Integer(related="last_execution_id.duration_ms", readonly=True, store=True)
    last_executed_at = fields.Datetime(related="last_execution_id.started_at", readonly=True, store=True)
    query_hash = fields.Char(compute="_compute_query_hash", store=True)

    _sql_constraints = [
        ("positive_default_limit", "CHECK(default_limit >= 0)", "Default limit cannot be negative."),
        ("positive_max_limit", "CHECK(max_limit >= 0)", "Maximum limit cannot be negative."),
        ("positive_timeout", "CHECK(timeout_ms >= 0)", "Timeout cannot be negative."),
        ("positive_cell_max_chars", "CHECK(cell_max_chars > 0)", "Cell character limit must be positive."),
    ]

    @api.depends("execution_ids")
    def _compute_execution_count(self):
        grouped = self.env["data.insight.execution"].read_group(
            [("query_id", "in", self.ids)],
            ["query_id"],
            ["query_id"],
        )
        counts = {row["query_id"][0]: row["query_id_count"] for row in grouped}
        for query in self:
            query.execution_count = counts.get(query.id, 0)

    @api.depends("sql_text")
    def _compute_query_hash(self):
        for query in self:
            normalized = (query.sql_text or "").strip().encode("utf-8")
            query.query_hash = hashlib.sha256(normalized).hexdigest() if normalized else False

    @api.constrains("default_limit", "max_limit")
    def _check_limit_order(self):
        for query in self:
            if query.max_limit and query.default_limit > query.max_limit:
                raise ValidationError(_("Default limit cannot be greater than maximum limit."))

    def _is_workbench_admin(self):
        return self.env.user.has_group("data_insight_workbench.group_data_insight_admin")

    def _has_full_sql_access(self):
        return self.env.is_admin() or self.env.user.has_group("data_insight_workbench.group_data_insight_full_access")

    def _check_can_execute(self):
        if not self.env.user.has_group("data_insight_workbench.group_data_insight_executor"):
            raise AccessError(_("You do not have permission to execute Data Insight queries."))
        for query in self:
            if query.state != "approved" and not query._has_full_sql_access():
                raise UserError(_("Only approved queries can be executed by non-admin workbench users."))

    def _check_can_unlink(self):
        if not self._is_workbench_admin():
            raise AccessError(_("Only Data Insight administrators can delete these records."))

    def unlink(self):
        self._check_can_unlink()
        return super().unlink()

    def action_validate_query(self):
        for query in self:
            if query._has_full_sql_access():
                query.message_post(body=Markup("<p>%s</p>") % _("Full SQL access is enabled for your user. This query will run without the read-only guard."))
            else:
                normalized, first_keyword = query._guard_sql(query.sql_text)
                query.message_post(
                    body=Markup("<p>%s</p>") % _(
                        "Query validated as read-only. Entry point: %s",
                        first_keyword.upper(),
                    )
                )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Validation complete"),
                "message": _("The query guard accepted the selected query."),
                "type": "success",
                "sticky": False,
            },
        }

    def action_mark_reviewed(self):
        self.write({"state": "reviewed"})

    def action_approve(self):
        if not self._is_workbench_admin():
            raise AccessError(_("Only Data Insight administrators can approve queries."))
        self.write({"state": "approved"})

    def action_archive_query(self):
        self.write({"state": "archived", "active": False})

    def action_open_runner(self):
        self.ensure_one()
        self._check_can_execute()
        return {
            "type": "ir.actions.act_window",
            "name": _("Run Query"),
            "res_model": "data.insight.query.run.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_query_id": self.id},
        }

    def action_view_executions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Execution History"),
            "res_model": "data.insight.execution",
            "view_mode": "tree,form",
            "domain": [("query_id", "=", self.id)],
            "context": {"default_query_id": self.id},
        }

    def action_open_last_execution(self):
        self.ensure_one()
        if not self.last_execution_id:
            raise UserError(_("This query has not been executed yet."))
        return self.last_execution_id.action_open_execution()

    def _guard_sql(self, sql_text):
        if not sql_text or not sql_text.strip():
            raise UserError(_("Enter a SQL statement first."))
        if "\x00" in sql_text:
            raise UserError(_("The query contains an invalid null byte."))

        sanitized, semicolons = self._sanitize_sql_for_guard(sql_text)
        self._ensure_single_statement(sanitized, semicolons)

        normalized = sql_text.strip()
        if normalized.endswith(";"):
            normalized = normalized[:-1].strip()
        sanitized_normalized = sanitized.strip()
        if sanitized_normalized.endswith(";"):
            sanitized_normalized = sanitized_normalized[:-1].strip()

        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", sanitized_normalized)
        first_keyword = match.group(1).lower() if match else ""
        if first_keyword not in ALLOWED_START_KEYWORDS:
            raise UserError(_("Only SELECT, WITH, EXPLAIN, and SHOW statements are allowed."))

        lower_sql = sanitized_normalized.lower()
        for keyword in sorted(FORBIDDEN_KEYWORDS):
            if re.search(r"\b%s\b" % re.escape(keyword), lower_sql):
                raise UserError(_("Keyword '%s' is not allowed in Data Insight queries.", keyword.upper()))
        if re.search(r"\binto\b", lower_sql):
            raise UserError(_("SELECT INTO is not allowed because it can create database objects."))
        if LOCKING_RE.search(sanitized_normalized):
            raise UserError(_("Row-locking clauses are not allowed."))
        if FORBIDDEN_FUNCTION_RE.search(sanitized_normalized):
            raise UserError(_("This query calls a blocked PostgreSQL function."))
        return normalized, first_keyword

    @api.model
    def _sanitize_sql_for_guard(self, sql_text):
        sanitized = []
        semicolons = []
        i = 0
        length = len(sql_text)
        while i < length:
            char = sql_text[i]
            nxt = sql_text[i + 1 : i + 2]

            if char == "-" and nxt == "-":
                end = sql_text.find("\n", i + 2)
                if end == -1:
                    sanitized.append(" " * (length - i))
                    break
                sanitized.append(" " * (end - i))
                sanitized.append("\n")
                i = end + 1
                continue

            if char == "/" and nxt == "*":
                end = sql_text.find("*/", i + 2)
                if end == -1:
                    raise UserError(_("The query contains an unterminated block comment."))
                sanitized.append(" " * (end + 2 - i))
                i = end + 2
                continue

            if char == "'":
                i = self._consume_quoted(sql_text, i, "'", sanitized)
                continue

            if char == '"':
                i = self._consume_quoted(sql_text, i, '"', sanitized)
                continue

            if char == "$":
                tag_match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql_text[i:])
                if tag_match:
                    tag = tag_match.group(0)
                    end = sql_text.find(tag, i + len(tag))
                    if end == -1:
                        raise UserError(_("The query contains an unterminated dollar-quoted string."))
                    sanitized.append(" " * (end + len(tag) - i))
                    i = end + len(tag)
                    continue

            if char == ";":
                semicolons.append(i)
            sanitized.append(char)
            i += 1

        return "".join(sanitized), semicolons

    @api.model
    def _consume_quoted(self, sql_text, index, quote_char, sanitized):
        i = index + 1
        length = len(sql_text)
        while i < length:
            if sql_text[i] == quote_char:
                if i + 1 < length and sql_text[i + 1] == quote_char:
                    i += 2
                    continue
                sanitized.append(" " * (i + 1 - index))
                return i + 1
            i += 1
        raise UserError(_("The query contains an unterminated quoted string."))

    @api.model
    def _ensure_single_statement(self, sanitized_sql, semicolons):
        if not semicolons:
            return
        first_semicolon = semicolons[0]
        if sanitized_sql[first_semicolon + 1 :].strip():
            raise UserError(_("Only one SQL statement is allowed."))

    def _extract_parameter_names(self, sql_text):
        return set(PARAMETER_RE.findall(sql_text or ""))

    def _coerce_parameters(self, parameter_json, sql_text=None):
        self.ensure_one()
        try:
            raw_params = json.loads(parameter_json or "{}")
        except json.JSONDecodeError as error:
            raise UserError(_("Parameter JSON is invalid: %s", error)) from error
        if not isinstance(raw_params, dict):
            raise UserError(_("Parameters must be a JSON object."))

        coerced = {}
        configured = {line.name: line for line in self.parameter_ids}
        required_names = self._extract_parameter_names(sql_text if sql_text is not None else self.sql_text)

        for key, value in raw_params.items():
            self._validate_parameter_name(key)
            line = configured.get(key)
            coerced[key] = line._coerce_value(value) if line else value

        for line in self.parameter_ids:
            if line.name not in coerced and line.default_value not in (False, None, ""):
                coerced[line.name] = line._coerce_value(line.default_value)
            if line.required and line.name not in coerced:
                raise UserError(_("Missing required parameter: %s", line.name))

        missing = sorted(name for name in required_names if name not in coerced)
        if missing:
            raise UserError(_("Missing SQL parameter values: %s", ", ".join(missing)))
        return coerced

    @api.model
    def _validate_parameter_name(self, name):
        if not NAME_RE.match(name or ""):
            raise UserError(_("Invalid parameter name: %s", name))
        if name.startswith("__diw_"):
            raise UserError(_("Parameter names starting with __diw_ are reserved."))

    def _execute_query(self, sql_text, parameters, limit, timeout_ms, mode="run"):
        self.ensure_one()
        self._check_can_execute()

        full_access = self._has_full_sql_access()
        limit = 0 if full_access else self._normalize_limit(limit)
        timeout_ms = 0 if full_access else self._normalize_timeout(timeout_ms)
        started_at = fields.Datetime.now()
        start_perf = time.perf_counter()

        status = "success"
        message = False
        headers = []
        rows = []
        truncated = False
        normalized_sql = False
        first_keyword = False

        try:
            if full_access:
                normalized_sql = (sql_text or "").strip()
                if not normalized_sql:
                    raise UserError(_("Enter a SQL statement first."))
                match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)", normalized_sql)
                first_keyword = match.group(1).lower() if match else ""
            else:
                normalized_sql, first_keyword = self._guard_sql(sql_text)
            execute_sql, execute_params = self._prepare_executable_sql(
                normalized_sql,
                first_keyword,
                parameters,
                limit,
                mode,
                full_access=full_access,
            )
            try:
                with self.env.cr.savepoint():
                    if timeout_ms:
                        self.env.cr.execute("SET LOCAL statement_timeout TO %s", [timeout_ms])
                    self.env.cr.execute(execute_sql, execute_params)
                    if self.env.cr.description:
                        headers = [description[0] for description in self.env.cr.description]
                        if limit:
                            raw_rows = self.env.cr.fetchmany(limit + 1)
                            truncated = len(raw_rows) > limit
                            rows = [self._serialize_row(row) for row in raw_rows[:limit]]
                        else:
                            rows = [self._serialize_row(row) for row in self.env.cr.fetchall()]
                    else:
                        message = self._completion_message(first_keyword, self.env.cr.rowcount)
                    if not full_access:
                        raise _ReadOnlyRollback()
            except _ReadOnlyRollback:
                pass
        except Exception as error:
            _logger.info("Data Insight query failed", exc_info=True)
            status = "error"
            message = str(error)

        duration_ms = int((time.perf_counter() - start_perf) * 1000)
        csv_content = self._build_csv(headers, rows) if headers else b""
        preview_html = self._build_preview_html(headers, rows, truncated, limit, message)
        execution = self.env["data.insight.execution"].create(
            {
                "query_id": self.id,
                "name": self._execution_name(started_at),
                "status": status,
                "started_at": started_at,
                "finished_at": fields.Datetime.now(),
                "duration_ms": duration_ms,
                "row_count": len(rows),
                "column_count": len(headers),
                "truncated": truncated,
                "limit": limit,
                "timeout_ms": timeout_ms,
                "mode": mode,
                "sql_text": sql_text,
                "normalized_sql": normalized_sql,
                "parameter_json": json.dumps(parameters or {}, indent=2, sort_keys=True, default=str),
                "headers_json": headers,
                "rows_json": rows,
                "preview_html": preview_html,
                "message": message,
                "csv_file": base64.b64encode(csv_content) if csv_content else False,
                "csv_filename": self._csv_filename(started_at) if csv_content else False,
            }
        )
        self.sudo().write({"last_execution_id": execution.id})
        self.sudo().message_post(
            body=Markup("<p>%s</p>") % _(
                "Execution finished with status '%s' in %s ms.",
                status,
                duration_ms,
            )
        )
        return execution

    def _execution_name(self, started_at):
        return _("%s at %s", self.name, _format_execution_datetime(self.env, started_at))

    def _completion_message(self, first_keyword, rowcount):
        command = (first_keyword or "statement").upper()
        if rowcount is None or rowcount < 0:
            return _("%s completed successfully. No result set was returned.", command)
        return _("%s completed successfully. Rows affected: %s", command, rowcount)

    def _normalize_limit(self, limit):
        value = self.default_limit if limit in (False, None, "") else limit
        value = max(0, int(value or 0))
        if self.max_limit and value:
            value = min(value, self.max_limit)
        return value

    def _normalize_timeout(self, timeout_ms):
        value = self.timeout_ms if timeout_ms in (False, None, "") else timeout_ms
        return max(0, int(value or 0))

    def _prepare_executable_sql(self, normalized_sql, first_keyword, parameters, limit, mode, full_access=False):
        execute_params = dict(parameters or {})
        if mode == "explain" and first_keyword != "explain":
            if first_keyword not in EXPLAINABLE_START_KEYWORDS:
                command = first_keyword.upper() if first_keyword else _("this statement")
                raise UserError(
                    _("Explain mode cannot be used for %s statements. Use Run mode to execute this statement.", command)
                )
            return "EXPLAIN (FORMAT TEXT) " + normalized_sql, execute_params
        if full_access:
            return normalized_sql, execute_params

        if limit and first_keyword in {"select", "with"} and mode == "run":
            execute_params["__diw_limit"] = limit + 1
            return (
                "SELECT * FROM ("
                + normalized_sql
                + ") AS data_insight_workbench_result LIMIT %(__diw_limit)s"
            ), execute_params

        return normalized_sql, execute_params

    @api.model
    def _serialize_row(self, row):
        return [self._serialize_value(value) for value in row]

    @api.model
    def _serialize_value(self, value):
        if value is None:
            return None
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, memoryview):
            return bytes(value).decode("utf-8", errors="replace")
        try:
            json.dumps(value)
            return value
        except TypeError:
            return str(value)

    @api.model
    def _build_csv(self, headers, rows):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])
        return buffer.getvalue().encode("utf-8")

    def _build_preview_html(self, headers, rows, truncated, limit, message):
        if message:
            return Markup("<div class='alert alert-warning'>%s</div>") % escape(message)
        if not headers:
            return Markup("<div class='alert alert-info'>%s</div>") % _("No rows returned.")

        header_cells = Markup("<th>#</th>") + Markup("").join(
            Markup("<th>%s</th>") % escape(header) for header in headers
        )
        body_rows = Markup("")
        max_chars = max(1, self.cell_max_chars)
        for index, row in enumerate(rows, 1):
            cells = [Markup("<td class='text-muted'>%s</td>") % index]
            for value in row:
                display = "" if value is None else str(value)
                if len(display) > max_chars:
                    display = display[:max_chars] + "..."
                cells.append(Markup("<td><code>%s</code></td>") % escape(display))
            body_rows += Markup("<tr>%s</tr>") % Markup("").join(cells)

        notice = Markup("")
        if truncated:
            notice = Markup("<div class='alert alert-info mb-2'>%s</div>") % _(
                "Preview truncated at %s rows.",
                limit,
            )
        return (
            notice
            + Markup("<div class='table-responsive'><table class='table table-sm table-hover o_list_table'>")
            + Markup("<thead><tr>%s</tr></thead>") % header_cells
            + Markup("<tbody>%s</tbody></table></div>") % body_rows
        )

    def _csv_filename(self, started_at):
        clean_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.name or "query").strip("_")
        timestamp_source = started_at if isinstance(started_at, str) else fields.Datetime.to_string(started_at)
        timestamp = timestamp_source.replace(" ", "_").replace(":", "")
        return "%s_%s.csv" % (clean_name or "query", timestamp)


class DataInsightQueryParameter(models.Model):
    _name = "data.insight.query.parameter"
    _description = "Data Insight Query Parameter"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    query_id = fields.Many2one("data.insight.query", required=True, ondelete="cascade")
    name = fields.Char(required=True)
    label = fields.Char()
    value_type = fields.Selection(
        [
            ("text", "Text"),
            ("integer", "Integer"),
            ("float", "Float"),
            ("boolean", "Boolean"),
            ("date", "Date"),
            ("datetime", "Datetime"),
        ],
        default="text",
        required=True,
    )
    default_value = fields.Char()
    required = fields.Boolean(default=True)
    help = fields.Char()

    _sql_constraints = [
        ("unique_query_parameter_name", "unique(query_id, name)", "Parameter names must be unique per query."),
    ]

    @api.constrains("name")
    def _check_name(self):
        for line in self:
            self.env["data.insight.query"]._validate_parameter_name(line.name)

    def unlink(self):
        if not self.env.user.has_group("data_insight_workbench.group_data_insight_admin"):
            raise AccessError(_("Only Data Insight administrators can delete query parameters."))
        return super().unlink()

    def _coerce_value(self, value):
        self.ensure_one()
        if (value is None or value == "" or (value is False and self.value_type != "boolean")) and not self.required:
            return None
        try:
            if self.value_type == "integer":
                return int(value)
            if self.value_type == "float":
                return float(value)
            if self.value_type == "boolean":
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
            if self.value_type == "date":
                return fields.Date.to_date(value)
            if self.value_type == "datetime":
                return fields.Datetime.to_datetime(value)
            return str(value)
        except Exception as error:
            raise UserError(_("Invalid value for parameter '%s': %s", self.name, error)) from error


class DataInsightExecution(models.Model):
    _name = "data.insight.execution"
    _description = "Data Insight Execution"
    _order = "started_at desc, id desc"

    name = fields.Char(string="Stored Name", required=True)
    local_name = fields.Char(string="Name", compute="_compute_local_name")
    query_id = fields.Many2one("data.insight.query", required=True, ondelete="cascade")
    status = fields.Selection(
        [
            ("success", "Success"),
            ("error", "Error"),
        ],
        required=True,
        default="success",
        index=True,
    )
    mode = fields.Selection(
        [
            ("run", "Run"),
            ("explain", "Explain"),
        ],
        default="run",
        required=True,
    )
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    duration_ms = fields.Integer(readonly=True)
    row_count = fields.Integer(readonly=True)
    column_count = fields.Integer(readonly=True)
    truncated = fields.Boolean(readonly=True)
    limit = fields.Integer(readonly=True)
    timeout_ms = fields.Integer(readonly=True)
    executed_by_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)

    sql_text = fields.Text(readonly=True)
    normalized_sql = fields.Text(readonly=True)
    parameter_json = fields.Text(readonly=True)
    headers_json = fields.Json(readonly=True)
    rows_json = fields.Json(readonly=True)
    preview_html = fields.Html(readonly=True, sanitize=False)
    message = fields.Text(readonly=True)
    csv_file = fields.Binary(readonly=True, attachment=True)
    csv_filename = fields.Char(readonly=True)

    @api.depends("name", "query_id.name", "started_at")
    @api.depends_context("lang", "tz")
    def _compute_local_name(self):
        for execution in self:
            if execution.query_id and execution.started_at:
                execution.local_name = _(
                    "%s at %s",
                    execution.query_id.name,
                    _format_execution_datetime(execution.env, execution.started_at),
                )
            else:
                execution.local_name = execution.name

    def unlink(self):
        if not self.env.user.has_group("data_insight_workbench.group_data_insight_admin"):
            raise AccessError(_("Only Data Insight administrators can delete execution history."))
        return super().unlink()

    def action_open_execution(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.local_name,
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_download_csv(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("This execution has no CSV export."))
        filename = quote(self.csv_filename or "query_result.csv")
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s/%s/csv_file/%s?download=true" % (self._name, self.id, filename),
            "target": "self",
        }

    def action_open_query(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.query_id.display_name,
            "res_model": "data.insight.query",
            "res_id": self.query_id.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_rerun(self):
        self.ensure_one()
        return self.query_id.action_open_runner()
