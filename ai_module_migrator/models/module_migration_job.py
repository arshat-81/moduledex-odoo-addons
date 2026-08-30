import ast
import base64
import hashlib
import io
import json
import logging
import os
import re
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

from markupsafe import Markup, escape as markup_escape

import odoo
from odoo import _, SUPERUSER_ID, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)


ODOO_VERSION_SELECTION = [(str(version), "Odoo %s" % version) for version in range(11, 20)]

TEXT_EXTENSIONS = {
    ".py", ".xml", ".js", ".scss", ".css", ".csv", ".json", ".txt", ".md", ".rst",
    ".po", ".pot", ".yml", ".yaml", ".html", ".jinja", ".j2",
}
IGNORED_DIRS = {".git", ".hg", ".svn", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}
MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")

# Module technical names that have shipped Enterprise-only across Odoo 11-19,
# used to tell an Enterprise addons_path entry apart from a Community one
# without relying on any particular directory naming convention.
#
# Every name here must be Enterprise-ONLY: a single false positive
# misclassifies a whole addons root, because _looks_like_enterprise_root()
# only needs one hit. Two names were removed for exactly that reason:
#   - "social_media" is Community ("Social media connectors for company
#     settings"), so it flipped the standard Community addons directory to
#     Enterprise and made every core dependency (web, mail, crm, ...) report
#     as "Enterprise-only" in the dependency summary and the AI prompt. The
#     Enterprise social app is "social", used below instead.
#   - "field_service" is an OCA module name, not an Odoo Enterprise one; the
#     Enterprise field-service app is "industry_fsm". The old name matched
#     any addons path carrying OCA's field_service.
ENTERPRISE_MARKER_MODULES = {
    "documents", "helpdesk", "sign", "voip", "appointment", "web_studio",
    "account_accountant", "quality_control", "industry_fsm", "planning",
    "knowledge", "social", "marketing_automation", "web_enterprise",
    "iot", "timesheet_grid", "account_consolidation", "mrp_plm", "approvals",
}


class AiModuleMigrationJob(models.Model):
    _name = "ai.module.migration.job"
    _description = "AI Module Migration Job"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "write_date desc, id desc"

    name = fields.Char(default=lambda self: _("New Migration Job"), required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("scanned", "Scanned"),
            ("generating", "Generating AI Plan"),
            ("generated", "Generated"),
            ("migrating", "Migrating"),
            ("migrated", "Migrated"),
            ("failed", "Failed"),
            ("archived", "Archived"),
        ],
        default="draft",
        required=True,
        tracking=True,
    )

    input_mode = fields.Selection(
        [("filesystem", "Server Path"), ("upload", "Uploaded Zip")],
        default="filesystem",
        required=True,
        tracking=True,
    )
    source_path = fields.Char(
        string="Source Addon Path",
        help="Server-side path to an Odoo addon directory containing __manifest__.py or __openerp__.py.",
    )
    source_archive = fields.Binary(string="Source Zip", attachment=True)
    source_archive_filename = fields.Char()
    dependency_archive = fields.Binary(
        string="Dependency Zip",
        attachment=True,
        help="Optional zip containing dependency addon folders. Used only as AI context, not migrated.",
    )
    dependency_archive_filename = fields.Char()
    source_fingerprint = fields.Char(readonly=True, copy=False)

    module_name = fields.Char(readonly=True, copy=False)
    manifest_name = fields.Char(readonly=True, copy=False)
    manifest_text = fields.Text(readonly=True, copy=False)
    manifest_text_html = fields.Html(
        string="Manifest (Formatted)",
        compute="_compute_manifest_text_html",
        sanitize=False,
    )
    manifest_data_json = fields.Text(readonly=True, copy=False)
    source_version_note = fields.Char(readonly=True, copy=False)
    source_version_confidence = fields.Selection(
        [
            ("exact", "Exact"),
            ("high", "High"),
            ("medium", "Medium"),
            ("low", "Low"),
        ],
        readonly=True,
        copy=False,
    )
    source_version_evidence = fields.Text(readonly=True, copy=False)
    source_version_evidence_html = fields.Html(
        string="Source Version Consensus (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )

    source_version = fields.Selection(ODOO_VERSION_SELECTION, default="16", required=True, tracking=True)
    target_version = fields.Selection(ODOO_VERSION_SELECTION, default="19", required=True, tracking=True)
    direction = fields.Selection(
        [("upgrade", "Upgrade"), ("downgrade", "Downgrade"), ("same_version", "Same Version")],
        compute="_compute_direction",
        store=True,
    )
    output_format = fields.Selection(
        [
            ("report", "Migration Report"),
            ("patch_plan", "Patch-Oriented Plan"),
            ("rewrite_plan", "File-by-File Rewrite Plan"),
            ("review", "Compatibility Review"),
        ],
        default="patch_plan",
        required=True,
    )
    provider = fields.Selection(
        selection=lambda self: self.env["ai.module.migrator.provider.service"].provider_selection(),
        default=lambda self: self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.provider") or "gemini",
        required=True,
    )
    model_override = fields.Char(
        help="Optional model override for this job. Leave empty to use Settings.",
    )
    custom_instruction = fields.Text(
        string="Extra AI Instruction",
        help="Optional project-specific constraints, coding style, or migration requirements.",
    )
    dependency_handling = fields.Selection(
        [
            ("review", "Review Dependencies"),
            ("include", "Include Dependency Context"),
            ("ignore", "Do Not Include Dependencies"),
        ],
        default="review",
        required=True,
        tracking=True,
        help="Choose whether dependency modules should be included as AI context after Load Source detects them.",
    )

    file_count = fields.Integer(readonly=True, copy=False)
    python_file_count = fields.Integer(readonly=True, copy=False)
    xml_file_count = fields.Integer(readonly=True, copy=False)
    js_file_count = fields.Integer(readonly=True, copy=False)
    total_bytes = fields.Integer(readonly=True, copy=False)
    source_edition = fields.Selection(
        [("community", "Community"), ("enterprise", "Enterprise"), ("mixed", "Mixed"), ("unknown", "Unknown")],
        default="unknown",
        readonly=True,
        copy=False,
    )
    source_summary = fields.Text(readonly=True, copy=False)
    source_summary_html = fields.Html(
        string="Source Summary (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    dependency_names = fields.Text(readonly=True, copy=False)
    dependency_summary = fields.Text(readonly=True, copy=False)
    dependency_summary_html = fields.Html(
        string="Dependency Summary (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    dependency_context = fields.Text(readonly=True, copy=False)
    dependency_context_html = fields.Html(
        string="Dependency Context (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    static_findings = fields.Text(readonly=True, copy=False)
    static_findings_html = fields.Html(
        string="Static Findings (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    file_inventory = fields.Text(readonly=True, copy=False)
    file_inventory_html = fields.Html(
        string="File Inventory (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    local_index_summary = fields.Text(readonly=True, copy=False)
    local_index_summary_html = fields.Html(
        string="Local Addon Index (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )
    module_symbol_map = fields.Text(
        readonly=True,
        copy=False,
        help="Whole-module model/field index built once from the original source and sent as "
        "extra context with every per-file migration prompt, so weaker models don't have to "
        "guess field/model names that live in a sibling file.",
    )
    module_symbol_map_html = fields.Html(
        string="Module Symbol Map (Formatted)",
        compute="_compute_report_html_fields",
        sanitize=False,
    )

    migration_report = fields.Text(copy=False)
    migration_report_html = fields.Html(
        string="Migration Report (Formatted)",
        compute="_compute_migration_report_html",
        sanitize=False,
    )
    ai_raw_response = fields.Text(copy=False)
    ai_raw_response_html = fields.Html(
        string="AI Raw Response (Formatted)",
        compute="_compute_migration_report_html",
        sanitize=False,
    )
    last_error = fields.Text(readonly=True, copy=False)
    last_scan_at = fields.Datetime(readonly=True, copy=False)
    last_generated_at = fields.Datetime(readonly=True, copy=False)
    report_attachment_id = fields.Many2one("ir.attachment", readonly=True, copy=False)

    # ── Code migration output ──────────────────────────────────────────────────
    output_server_path = fields.Char(
        string="Output Server Path",
        help="Optional server path where the migrated module folder will be written. "
             "Leave empty to use zip download only.",
    )
    output_zip_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Migrated Module Zip",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    migrated_file_count = fields.Integer(string="Migrated Files", readonly=True, copy=False)
    skipped_file_count = fields.Integer(string="Skipped", readonly=True, copy=False)
    skipped_file_list = fields.Text(string="Skipped Files", readonly=True, copy=False)
    review_file_count = fields.Integer(
        string="Needs Review",
        readonly=True,
        copy=False,
        help="Files that passed syntax validation but were flagged because the AI's output "
        "structurally differs from the original (e.g. a method, field, or XML record present "
        "before migration is missing after it). Not necessarily wrong — but not auto-trusted.",
    )
    review_file_list = fields.Text(string="Needs Review Files", readonly=True, copy=False)
    migration_file_ids = fields.One2many(
        "ai.module.migration.file",
        "job_id",
        string="Migration Files",
        readonly=True,
        copy=False,
    )
    migration_log_ids = fields.One2many(
        "ai.module.migration.log",
        "job_id",
        string="AI Logs",
        readonly=True,
        copy=False,
    )
    migration_terminal_log = fields.Text(
        string="AI Terminal",
        compute="_compute_migration_terminal_log",
    )
    # Anchor field for the JS auto-refresh widget. Not stored, never written to —
    # it only exists so a widget can attach to it and poll record.load() while
    # the job is actively scanning/migrating, so progress updates without a
    # manual reload of the page.
    ai_module_migrator_live_refresh_ping = fields.Char(string="Live Refresh", store=False)

    # Odoo 19 dropped _sql_constraints in favour of models.Constraint. The old
    # list form is not an error, it is simply ignored with a registry warning —
    # so both CHECK constraints silently never reached the database.
    _positive_file_count = models.Constraint(
        "CHECK(file_count >= 0)",
        "File count cannot be negative.",
    )
    _positive_total_bytes = models.Constraint(
        "CHECK(total_bytes >= 0)",
        "Total bytes cannot be negative.",
    )

    @api.depends("source_version", "target_version")
    def _compute_direction(self):
        for job in self:
            source = int(job.source_version or 0)
            target = int(job.target_version or 0)
            if source < target:
                job.direction = "upgrade"
            elif source > target:
                job.direction = "downgrade"
            else:
                job.direction = "same_version"

    @api.depends(
        "migration_log_ids.create_date",
        "migration_log_ids.event",
        "migration_log_ids.file_path",
        "migration_log_ids.level",
        "migration_log_ids.provider",
        "migration_log_ids.model",
        "migration_log_ids.max_tokens",
        "migration_log_ids.duration",
        "migration_log_ids.message",
    )
    def _compute_migration_terminal_log(self):
        for job in self:
            lines = []
            for log in job.migration_log_ids.sorted(lambda item: item.id):
                timestamp = ""
                if log.create_date:
                    local_dt = fields.Datetime.context_timestamp(job, log.create_date)
                    timestamp = local_dt.strftime("%H:%M:%S")
                parts = ["[%s]" % timestamp if timestamp else "[--:--:--]"]
                parts.append((log.level or "info").upper().ljust(7))
                parts.append("$ %s" % (log.event or "log"))
                if log.file_path:
                    parts.append(log.file_path)
                if log.provider:
                    model = "/%s" % log.model if log.model else ""
                    parts.append("provider=%s%s" % (log.provider, model))
                if log.max_tokens:
                    parts.append("max_tokens=%s" % log.max_tokens)
                if log.duration:
                    parts.append("duration=%.2fs" % log.duration)
                line = "  ".join(parts)
                if log.message:
                    line = "%s\n    %s" % (line, log.message.replace("\n", "\n    "))
                lines.append(line)
            job.migration_terminal_log = "\n\n".join(lines)

    @api.depends("migration_report", "ai_raw_response")
    def _compute_migration_report_html(self):
        for job in self:
            job.migration_report_html = self._markdown_to_html(job.migration_report or "")
            job.ai_raw_response_html = self._markdown_to_html(job.ai_raw_response or "")

    @api.depends(
        "source_summary",
        "source_version_evidence",
        "dependency_summary",
        "dependency_context",
        "static_findings",
        "file_inventory",
        "local_index_summary",
        "module_symbol_map",
    )
    def _compute_report_html_fields(self):
        for job in self:
            job.source_summary_html = self._markdown_to_html(job.source_summary or "")
            job.source_version_evidence_html = self._markdown_to_html(job.source_version_evidence or "")
            job.dependency_summary_html = self._markdown_to_html(job.dependency_summary or "")
            job.dependency_context_html = self._markdown_to_html(job.dependency_context or "")
            job.static_findings_html = self._markdown_to_html(job.static_findings or "")
            job.file_inventory_html = self._markdown_to_html(job.file_inventory or "")
            job.local_index_summary_html = self._markdown_to_html(job.local_index_summary or "")
            job.module_symbol_map_html = self._markdown_to_html(job.module_symbol_map or "")

    @api.depends("manifest_text")
    def _compute_manifest_text_html(self):
        for job in self:
            job.manifest_text_html = self._code_to_html(job.manifest_text or "", language="python")

    @api.model
    def _markdown_to_html(self, text):
        """Lightweight Markdown → HTML conversion (no external dependency).

        Handles the subset of Markdown the AI providers and the built-in
        report generators actually emit: #/##/### headers, **bold**,
        `inline code`, fenced ``` code blocks, - / 1. lists, and
        `col | col` pipe tables (used by the File Inventory report).
        Everything else is rendered as plain paragraphs. Output is escaped
        before any markup is applied, so raw text can never inject HTML.
        """
        if not text:
            return ""

        def inline(segment):
            segment = str(markup_escape(segment))
            segment = re.sub(r"`([^`]+)`", r"<code>\1</code>", segment)
            segment = re.sub(r"\*\*([^\*]+)\*\*", r"<strong>\1</strong>", segment)
            segment = re.sub(r"(?<!\*)\*([^\*]+)\*(?!\*)", r"<em>\1</em>", segment)
            return segment

        html_parts = []
        list_stack = []  # tuples of (tag, indent) currently open

        def close_lists(upto=0):
            while len(list_stack) > upto:
                tag, _indent = list_stack.pop()
                html_parts.append("</%s>" % tag)

        def is_table_separator(candidate):
            cells = [cell.strip() for cell in candidate.strip().strip("|").split("|")]
            return bool(cells) and all(re.match(r"^:?-{1,}:?$", cell) for cell in cells)

        lines = text.replace("\r\n", "\n").split("\n")
        in_code_block = False
        code_lines = []
        code_lang = ""
        index = 0
        total = len(lines)

        while index < total:
            raw_line = lines[index]
            fence = re.match(r"^```(\w*)\s*$", raw_line.strip())
            if fence:
                if in_code_block:
                    close_lists()
                    css_class = ' class="language-%s"' % code_lang if code_lang else ""
                    html_parts.append(
                        "<pre><code%s>%s</code></pre>" % (css_class, markup_escape("\n".join(code_lines)))
                    )
                    code_lines = []
                    code_lang = ""
                    in_code_block = False
                else:
                    in_code_block = True
                    code_lang = fence.group(1)
                index += 1
                continue
            if in_code_block:
                code_lines.append(raw_line)
                index += 1
                continue

            line = raw_line.rstrip()
            if not line.strip():
                close_lists()
                index += 1
                continue

            # Pipe table: a "|"-separated header line followed by a --- separator row.
            if "|" in line and index + 1 < total and is_table_separator(lines[index + 1]):
                close_lists()
                header_cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
                html_parts.append("<table class=\"o_ai_module_migrator_table\"><thead><tr>")
                for cell in header_cells:
                    html_parts.append("<th>%s</th>" % inline(cell))
                html_parts.append("</tr></thead><tbody>")
                index += 2
                while index < total and "|" in lines[index] and lines[index].strip():
                    row_cells = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                    html_parts.append("<tr>")
                    for cell in row_cells:
                        html_parts.append("<td>%s</td>" % inline(cell))
                    html_parts.append("</tr>")
                    index += 1
                html_parts.append("</tbody></table>")
                continue

            header = re.match(r"^(#{1,6})\s+(.*)$", line)
            if header:
                close_lists()
                level = len(header.group(1))
                html_parts.append("<h%d>%s</h%d>" % (level, inline(header.group(2).strip()), level))
                index += 1
                continue

            bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
            numbered = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
            if bullet or numbered:
                tag = "ul" if bullet else "ol"
                content = (bullet or numbered).group(2)
                if not list_stack or list_stack[-1][0] != tag:
                    close_lists()
                    html_parts.append("<%s>" % tag)
                    list_stack.append((tag, 0))
                html_parts.append("<li>%s</li>" % inline(content))
                index += 1
                continue

            close_lists()
            html_parts.append("<p>%s</p>" % inline(line.strip()))
            index += 1

        close_lists()
        if in_code_block and code_lines:
            html_parts.append("<pre><code>%s</code></pre>" % markup_escape("\n".join(code_lines)))

        return Markup("\n".join(html_parts))

    @api.model
    def _code_to_html(self, text, language=""):
        """Wrap raw source (e.g. a __manifest__.py) as an escaped, unparsed code block.

        Unlike _markdown_to_html, this never interprets '#' or other characters
        in the source as Markdown — a Python comment must not become a heading.
        """
        if not text:
            return ""
        css_class = ' class="language-%s"' % language if language else ""
        return Markup("<pre><code%s>%s</code></pre>" % (css_class, markup_escape(text)))

    @api.constrains("source_version", "target_version")
    def _check_versions(self):
        for job in self:
            if not job.source_version or not job.target_version:
                raise ValidationError(_("Source and target versions are required."))

    def action_load_source(self):
        self._check_manager_access()
        for job in self:
            try:
                files, root_label = job._load_source_files()
                scan = job._analyze_files(files, root_label)
                job.write(scan)
                message = _("Source addon loaded.")
                if job._dependency_list() and job.dependency_handling == "review":
                    message = _(
                        "Source addon loaded. Dependencies were detected; choose Include Dependency Context or Do Not Include Dependencies before AI actions."
                    )
                job.message_post(body=Markup("<p>%s</p>") % message)
            except Exception as error:
                job.write({"state": "failed", "last_error": str(error)})
                raise
        return self._notification(
            _("Source loaded"),
            _("The selected addon was loaded and analyzed."),
            "success",
            next_action={"type": "ir.actions.client", "tag": "soft_reload"},
        )

    def action_scan_source(self):
        return self.action_load_source()

    def action_generate_static_report(self):
        for job in self:
            if job.state == "draft":
                job.action_load_source()
            report = job._offline_report()
            job.write({
                "migration_report": report,
                "ai_raw_response": False,
                "last_generated_at": fields.Datetime.now(),
                "state": "generated",
            })
            job.message_post(body=Markup("<p>%s</p>") % _("Static migration report generated."))
        return self._notification(
            _("Report generated"),
            _("Static migration report is ready."),
            "success",
            next_action={"type": "ir.actions.client", "tag": "soft_reload"},
        )

    def action_generate_migration_plan(self):
        """Queue AI plan generation instead of calling the provider inline.

        The AI call can easily run longer than Odoo's request watchdog
        (limit_time_real, 120s by default) even though the provider
        timeout is configured much higher. When that happened here the
        watchdog killed the HTTP worker thread mid-request and forced a
        full server reload, which is why the button appeared to "hang"
        and then the whole instance restarted. Queueing it as a
        background task mirrors the pattern already used for code
        migration.
        """
        self._check_manager_access()
        self._check_queue_available()
        queued = 0
        for job in self:
            if job.state in ("migrating", "generating"):
                raise UserError(_("A job is already queued or running for %s.") % (job.display_name,))
            if job.state == "draft":
                job.action_load_source()
            job._check_dependency_decision()
            job.write({"state": "generating", "last_error": False})
            _logger.info(
                "[AI_MIGRATOR_DEBUG] action_generate_migration_plan ENQUEUE job_id=%s name=%s provider=%s model_override=%s",
                job.id, job.display_name, job.provider, job.model_override,
            )
            job._queue().enqueue(
                job,
                "job_generate_migration_plan",
                _("Generate AI migration plan: %s") % (job.display_name,),
                priority=5,
            )
            job.message_post(body=Markup("<p>%s</p>") % _("AI migration plan generation queued."))
            queued += 1
        return self._notification(
            _("AI report queued"),
            _("%s AI migration plan job(s) were queued. This form refreshes automatically when the report is ready.") % queued,
            "success",
            next_action={"type": "ir.actions.client", "tag": "soft_reload"},
        )

    def job_generate_migration_plan(self):
        """Queue-job worker: does the actual (slow) AI provider call."""
        self.ensure_one()
        self._check_manager_access()
        service = self.env["ai.module.migrator.provider.service"]
        params = self.env["ir.config_parameter"].sudo()
        t0 = time.monotonic()
        _logger.info(
            "[AI_MIGRATOR_DEBUG] job_generate_migration_plan STARTED job_id=%s name=%s",
            self.id, self.display_name,
        )
        try:
            prompt = self._build_ai_prompt()
            _logger.info(
                "[AI_MIGRATOR_DEBUG] job_generate_migration_plan prompt built job_id=%s prompt_chars=%s build_elapsed=%.2fs",
                self.id, len(prompt or ""), time.monotonic() - t0,
            )
            self._serialize_ai_provider_calls(self.provider or params.get_param("ai_module_migrator.provider") or "gemini")
            response = service.generate_text(
                prompt,
                provider=self.provider,
                model=self.model_override,
                max_tokens=params.get_param("ai_module_migrator.max_output_tokens") or 5000,
                temperature=params.get_param("ai_module_migrator.temperature") or 0.2,
            )
            self.write({
                "migration_report": response,
                "ai_raw_response": response,
                "last_generated_at": fields.Datetime.now(),
                "state": "generated",
                "last_error": False,
            })
            self.message_post(body=Markup("<p>%s</p>") % _("AI migration report generated."))
            _logger.info(
                "[AI_MIGRATOR_DEBUG] job_generate_migration_plan FINISHED job_id=%s total_elapsed=%.2fs",
                self.id, time.monotonic() - t0,
            )
        except Exception as error:
            _logger.exception(
                "[AI_MIGRATOR_DEBUG] job_generate_migration_plan FAILED job_id=%s total_elapsed=%.2fs",
                self.id, time.monotonic() - t0,
            )
            self.write({"state": "failed", "last_error": str(error)})
            raise

    def action_export_report(self):
        self.ensure_one()
        if not self.migration_report:
            raise UserError(_("Generate a migration report first."))
        filename = "%s_%s_to_%s_migration_report.md" % (
            self.module_name or "addon",
            self.source_version,
            self.target_version,
        )
        attachment = self.env["ir.attachment"].create({
            "name": filename,
            "type": "binary",
            "datas": base64.b64encode(self.migration_report.encode("utf-8")),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "text/markdown",
        })
        self.report_attachment_id = attachment
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % attachment.id,
            "target": "self",
        }

    def action_download_migrated_zip(self):
        self.ensure_one()
        if not self.output_zip_attachment_id:
            raise UserError(_("Run Code Migration first to generate the output zip."))
        return {
            "type": "ir.actions.act_url",
            "url": "/web/content/%s?download=true" % self.output_zip_attachment_id.id,
            "target": "self",
        }

    def action_reset_to_draft(self):
        self.write({"state": "draft", "last_error": False})

    def action_archive(self):
        self.write({"state": "archived", "active": False})

    def _check_manager_access(self):
        if not self.env.user.has_group("ai_module_migrator.group_ai_module_migrator_manager"):
            raise AccessError(_("Only AI Addon Migrator managers can scan server files or call AI providers."))

    def _check_queue_available(self):
        """Background work is run by this module's own cron, so nothing external
        needs installing — but a disabled cron would silently leave every queued
        task sitting in "pending" with no visible reason, which is exactly the
        confusing failure the old queue_job dependency used to produce.
        """
        self._warn_if_watchdog_too_low()
        cron = self.env.ref(
            "ai_module_migrator.ir_cron_ai_module_migrator_run_jobs", raise_if_not_found=False,
        )
        if cron and not cron.sudo().active:
            raise UserError(_(
                "The scheduled action \"AI Migrator: Run queued tasks\" is disabled, so queued "
                "migration tasks would never run. Enable it under Settings → Technical → "
                "Scheduled Actions."
            ))

    def _queue(self):
        return self.env["ai.module.migration.queue"].sudo()

    def _warn_if_watchdog_too_low(self):
        """Flag an odoo.conf watchdog that a single AI call can outlive.

        Odoo caps cron threads at limit_time_real_cron (falling back to
        limit_time_real), and reaching that cap makes ThreadedServer reload the
        whole server — the migration would then appear to die for no visible
        reason, mid-run. The queue survives it (each task commits, and the
        stale sweep requeues whatever was in flight), but the run stalls and
        the user has no idea why. Warning at queue time, in the job's own
        chatter, puts the cause where whoever clicked the button will see it.

        Deliberately a warning, not a block: plenty of setups migrate small
        files well inside the limit and must not be stopped from working.
        """
        watchdog = self._queue()._watchdog_seconds()
        if not watchdog:
            return False
        params = self.env["ir.config_parameter"].sudo()
        try:
            timeout = int(params.get_param("ai_module_migrator.request_timeout") or 600)
        except (TypeError, ValueError):
            timeout = 600
        if watchdog >= timeout + 60:
            return False
        message = _(
            "Heads-up: one AI request may take up to %(timeout)ss, but Odoo will kill and "
            "reload this server if a scheduled action runs longer than %(watchdog)ss. A long "
            "migration can be interrupted by that reload. Add this to odoo.conf under "
            "[options] and restart:\n\nlimit_time_real_cron = 3600"
        ) % {"timeout": timeout, "watchdog": watchdog}
        _logger.warning("[AI_MIGRATOR] %s", message.replace("\n", " "))
        for job in self:
            job.message_post(body=Markup("<p>%s</p>") % message)
        return True

    def action_run_code_migration(self):
        """Prepare file lines and queue one AI job per migratable file."""
        self._check_manager_access()
        self._check_queue_available()
        queued_files = 0
        for job in self:
            if job.state in ("migrating", "generating"):
                raise UserError(_("A job is already queued or running for %s.") % (job.display_name,))
            job._check_dependency_decision()

            files, root_label = job._load_source_files()
            scan = job._analyze_files(files, root_label)
            scan.update({
                "state": "migrating",
                "last_error": False,
                "migrated_file_count": 0,
                "skipped_file_count": 0,
                "skipped_file_list": False,
                "review_file_count": 0,
                "review_file_list": False,
            })
            job.write(scan)
            job._check_dependency_decision()
            job.migration_file_ids.unlink()
            job.migration_log_ids.unlink()

            run_id = uuid.uuid4().hex
            queued_lines = 0
            for path, file_data in sorted(files.items()):
                line = job._prepare_migration_file_line(path, file_data)
                if line.state == "pending":
                    job._queue().enqueue(
                        job,
                        "job_migrate_file",
                        _("AI migrate %(module)s/%(path)s") % {
                            "module": job.module_name or job.display_name,
                            "path": path,
                        },
                        priority=5,
                        run_id=run_id,
                        migration_file_id=line.id,
                    )
                    queued_lines += 1

            job._refresh_migration_counts()
            job._log_migration_event(
                "queue",
                _("Queued %s AI file task(s). Non-code/binary files were prepared for unchanged copy.") % queued_lines,
                level="info",
            )
            # The zip build waits on the whole run rather than being chained to
            # each file task: barrier_run_id makes it defer itself until no task
            # of this run is pending or running any more.
            job._queue().enqueue(
                job,
                "job_finalize_code_migration",
                _("Build migrated zip: %s") % (job.display_name,),
                priority=20,
                barrier_run_id=run_id if queued_lines else False,
            )

            queued_files += queued_lines
            job.message_post(
                body=Markup("<p>%s</p>") % _(
                    "Code migration queued: %s AI file task(s), then final zip build."
                ) % queued_lines
            )

        return self._notification(
            _("Migration queued"),
            _("%s AI file job(s) were added to the queue. Refresh the job to see committed progress.") % queued_files,
            "success",
            next_action={"type": "ir.actions.client", "tag": "soft_reload"},
        )

    def _has_active_migration_queue_jobs(self):
        """True if a job_migrate_file task for this record is genuinely in flight.

        Only job_migrate_file is checked (not job_finalize_code_migration): the
        finalize task legitimately sits pending, deferring itself on its barrier,
        until every file task of the run reaches a terminal state — including
        forever if one of them died (see _recover_stuck_pending_files). Treating
        that as "active" would make a dead task permanently unretryable, which is
        the bug this guards against.
        """
        self.ensure_one()
        return bool(
            self._queue().search_count([
                ("model_name", "=", "ai.module.migration.job"),
                ("res_id", "=", self.id),
                ("method", "=", "job_migrate_file"),
                ("state", "in", ("pending", "running")),
            ])
        )

    def _recover_stuck_pending_files(self):
        """Flip 'pending' file lines whose backing queue task died to 'error'.

        job_migrate_file() normally writes its own result (migrated/error/
        skipped/copied) even on failure, via its try/except. But if the worker
        process is killed or restarted mid-file, the queue task itself ends up
        'failed' after exhausting its attempts without that write ever
        happening, so the line stays 'pending' forever and the job never leaves
        the 'migrating' state. Detect that here so the file surfaces for Retry
        Skipped/Error Files instead of blocking forever.
        """
        self.ensure_one()
        pending_lines = self.migration_file_ids.filtered(lambda line: line.state == "pending")
        if not pending_lines:
            return
        # Map dead tasks to the file line they carried. Reading the id out of
        # the JSON payload is exact, unlike the LIKE match on queue_job's
        # func_string this replaces, which also matched line ids by prefix
        # (task for line 12 matching line 1).
        dead_by_line = {}
        for task in self._queue().search([
            ("model_name", "=", "ai.module.migration.job"),
            ("res_id", "=", self.id),
            ("method", "=", "job_migrate_file"),
            ("state", "in", ("failed", "cancelled")),
        ], order="id desc"):
            try:
                payload = json.loads(task.payload or "{}")
            except ValueError:
                continue
            line_id = payload.get("migration_file_id")
            if line_id and line_id not in dead_by_line:
                dead_by_line[line_id] = task
        for line in pending_lines:
            stuck_job = dead_by_line.get(line.id)
            if not stuck_job:
                continue
            line.write({
                "state": "error",
                "skip_reason": _(
                    "The AI file task for this file died before it could finish "
                    "(task #%(job)s: %(reason)s). Use Retry Skipped Files to requeue it."
                ) % {
                    "job": stuck_job.id,
                    "reason": (stuck_job.error or stuck_job.state or "")[:400],
                },
            })
            self._log_migration_event(
                "worker_died",
                _("Detected a dead queue job (#%(job)s) for %(path)s; marked for retry.") % {
                    "job": stuck_job.id,
                    "path": line.file_path,
                },
                level="error",
                migration_file=line,
                immediate=True,
            )

    def action_retry_skipped_files(self):
        self._check_manager_access()
        self._check_queue_available()
        queued_files = 0
        for job in self:
            job._recover_stuck_pending_files()
            if job.state in ("migrating", "generating") and job._has_active_migration_queue_jobs():
                raise UserError(_("A job is already queued or running for %s.") % (job.display_name,))

            retry_lines = job.migration_file_ids.filtered(lambda line: line.state in ("error", "skipped"))
            if not retry_lines:
                continue

            retry_lines.write({
                "state": "pending",
                "skip_reason": _("Queued for retry."),
                "migrated_content": False,
            })
            job.write({
                "state": "migrating",
                "last_error": False,
            })
            job._log_migration_event(
                "retry_queue",
                _("Queued retry for %s skipped/error file(s).") % len(retry_lines),
                level="warning",
            )

            run_id = uuid.uuid4().hex
            for line in retry_lines:
                job._queue().enqueue(
                    job,
                    "job_migrate_file",
                    _("AI retry %(module)s/%(path)s") % {
                        "module": job.module_name or job.display_name,
                        "path": line.file_path,
                    },
                    priority=4,
                    run_id=run_id,
                    migration_file_id=line.id,
                )
            job._queue().enqueue(
                job,
                "job_finalize_code_migration",
                _("Rebuild migrated zip after retry: %s") % (job.display_name,),
                priority=20,
                barrier_run_id=run_id,
            )
            queued_files += len(retry_lines)
            job.message_post(
                body=Markup("<p>%s</p>") % _(
                    "Retry queued for %s skipped/error file(s), then final zip rebuild."
                ) % len(retry_lines)
            )

        if not queued_files:
            raise UserError(_("There are no skipped or error files to retry."))
        return self._notification(
            _("Retry queued"),
            _("%s skipped/error file(s) were added to the queue.") % queued_files,
            "success",
            next_action={"type": "ir.actions.client", "tag": "soft_reload"},
        )

    def job_run_code_migration(self):
        """Compatibility entry point for stale monolithic queue jobs."""
        for job in self:
            job.write({
                "state": "failed",
                "last_error": _("This queued migration used the old monolithic runner. Reset and queue it again."),
            })
            job.message_post(
                body=Markup("<p>%s</p>") % _(
                    "Old monolithic migration job stopped. Reset and queue the migration again."
                )
            )

    def job_migrate_file(self, migration_file_id):
        self.ensure_one()
        self._check_manager_access()
        line = self.env["ai.module.migration.file"].browse(migration_file_id).exists()
        if not line or line.job_id != self:
            return
        if line.state != "pending":
            return

        started_at = time.monotonic()
        files, _root_label = self._load_source_files()
        file_data = files.get(line.file_path)
        if not file_data:
            line.write({
                "state": "error",
                "skip_reason": _("Source file was not found when this queued file job ran."),
            })
            self._log_migration_event(
                "source_missing",
                _("Source file was not found when this queued file job ran."),
                level="error",
                migration_file=line,
                immediate=True,
            )
            return

        content = file_data.get("content")
        if not (content or "").strip():
            line.write({
                "state": "copied",
                "skip_reason": _("Empty file copied unchanged."),
                "migrated_content": content or "",
            })
            self._log_migration_event(
                "copied",
                _("Empty file copied unchanged."),
                level="info",
                migration_file=line,
                duration=time.monotonic() - started_at,
                immediate=True,
            )
            return

        service = self.env["ai.module.migrator.provider.service"]
        params = self.env["ir.config_parameter"].sudo()
        configured_max_tokens = params.get_param("ai_module_migrator.max_output_tokens") or 8000
        max_tokens = self._file_migration_max_tokens(content, configured_max_tokens)
        temperature = service._safe_temperature(params.get_param("ai_module_migrator.temperature"))
        prompt = self._build_file_migration_prompt(line.file_path, content)
        provider = self.provider or params.get_param("ai_module_migrator.provider") or "gemini"
        model_name = self.model_override
        try:
            _endpoint, model_name, _api_key = service._credentials(provider, model=model_name)
        except Exception:
            model_name = model_name or False

        self._log_migration_event(
            "request_sent",
            _("Sending file to %(provider)s/%(model)s with %(tokens)s max output tokens.") % {
                "provider": provider,
                "model": model_name or _("default model"),
                "tokens": max_tokens,
            },
            level="info",
            migration_file=line,
            provider=provider,
            model=model_name,
            max_tokens=max_tokens,
            prompt_preview=prompt,
            immediate=True,
        )

        self._serialize_ai_provider_calls(provider)
        try:
            response = service.generate_text(
                prompt,
                provider=self.provider,
                model=self.model_override,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as error:
            if self._is_empty_response_error(error):
                # Not a transport failure — the model replied with nothing.
                # Treat it as a non-answer so the forcing loop re-asks.
                response = ""
            elif self._is_token_limit_error(error):
                retry_tokens = self._file_migration_max_tokens(content, configured_max_tokens, retry=True)
                if retry_tokens > max_tokens:
                    self._log_migration_event(
                        "retry_sent",
                        _("Token limit reached at %s output tokens. Retrying with %s.") % (max_tokens, retry_tokens),
                        level="warning",
                        migration_file=line,
                        provider=provider,
                        model=model_name,
                        max_tokens=retry_tokens,
                        duration=time.monotonic() - started_at,
                        immediate=True,
                    )
                    try:
                        response = service.generate_text(
                            prompt,
                            provider=self.provider,
                            model=self.model_override,
                            max_tokens=retry_tokens,
                            temperature=temperature,
                        )
                    except Exception as retry_error:
                        line.write({
                            "state": "error",
                            "skip_reason": _(
                                "AI token limit after retry (%(tokens)s output tokens): %(error)s. "
                                "Original file will be copied unchanged."
                            ) % {
                                "tokens": retry_tokens,
                                "error": str(retry_error)[:180],
                            },
                        })
                        self._log_migration_event(
                            "retry_failed",
                            _("Token-limit retry failed: %s") % str(retry_error)[:240],
                            level="error",
                            migration_file=line,
                            provider=provider,
                            model=model_name,
                            max_tokens=retry_tokens,
                            duration=time.monotonic() - started_at,
                            immediate=True,
                        )
                        return
                else:
                    line.write({
                        "state": "error",
                        "skip_reason": _(
                            "AI token limit at the provider cap (%s output tokens). "
                            "Use a model with a larger output limit or split this file manually. "
                            "Original file will be copied unchanged."
                        ) % max_tokens,
                    })
                    self._log_migration_event(
                        "token_cap",
                        _("Provider output cap reached at %s tokens. Original file will be copied unchanged.") % max_tokens,
                        level="error",
                        migration_file=line,
                        provider=provider,
                        model=model_name,
                        max_tokens=max_tokens,
                        duration=time.monotonic() - started_at,
                        immediate=True,
                    )
                    return
            else:
                line.write({
                    "state": "error",
                    "skip_reason": _("AI error: %s. Original file will be copied unchanged.") % str(error)[:240],
                })
                self._log_migration_event(
                    "failed",
                    _("AI error: %s") % str(error)[:500],
                    level="error",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    immediate=True,
                )
                return

        # A provider that answers SKIP / "I can't do this" / prose has not failed
        # technically — it declined. Push back and demand the file instead of
        # accepting the dodge, up to Max Migration Attempts.
        clean, non_answer_reason = self._force_file_migration(
            line, response, content, provider, model_name, max_tokens, temperature, started_at,
        )
        if non_answer_reason:
            attempts = self._max_migration_attempts()
            line.write({
                "state": "review",
                "skip_reason": _(
                    "The AI would not return this file after %(attempts)s attempts (%(reason)s). "
                    "The original file was kept unchanged and needs manual migration."
                ) % {"attempts": attempts, "reason": non_answer_reason},
                # Keep the ORIGINAL content so the file still ships in the output
                # module; "review" tells the user it was not actually migrated.
                "migrated_content": content,
            })
            self._log_migration_event(
                "force_exhausted",
                _("Gave up after %(attempts)s attempts — %(reason)s. Original file kept unchanged.") % {
                    "attempts": attempts, "reason": non_answer_reason,
                },
                level="error",
                migration_file=line,
                provider=provider,
                model=model_name,
                max_tokens=max_tokens,
                duration=time.monotonic() - started_at,
                response_preview=clean,
                immediate=True,
            )
        else:
            is_valid, validation_error = self._validate_migrated_content(line.file_path, clean)
            if not is_valid:
                self._log_migration_event(
                    "validation_failed",
                    _("Migrated content failed validation: %s. Asking %s to self-correct.") % (
                        validation_error, provider,
                    ),
                    level="warning",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    response_preview=clean,
                    immediate=True,
                )
                clean, is_valid, validation_error = self._retry_invalid_migration(
                    line, clean, validation_error, provider, model_name, max_tokens, temperature,
                )

            if not is_valid:
                line.write({
                    "state": "error",
                    "skip_reason": _(
                        "AI output failed syntax validation on every repair attempt (%s). "
                        "Original file will be copied unchanged."
                    ) % validation_error,
                    "migrated_content": clean,
                })
                self._log_migration_event(
                    "validation_failed_final",
                    _("AI output still invalid after every self-correction attempt (%s). "
                      "Original file will be copied unchanged.") % validation_error,
                    level="error",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    response_preview=clean,
                    immediate=True,
                )
                return

            legacy_markers = self._post_migration_legacy_markers(line.file_path, clean)
            if legacy_markers:
                self._log_migration_event(
                    "legacy_markers_found",
                    _(
                        "Migrated output still looks outdated for Odoo %(target)s: %(markers)s. "
                        "Asking %(provider)s for a targeted correction."
                    ) % {
                        "target": self.target_version,
                        "markers": "; ".join(legacy_markers),
                        "provider": provider,
                    },
                    level="warning",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    response_preview=clean,
                    immediate=True,
                )
                clean, legacy_markers = self._retry_legacy_markers(
                    line, clean, legacy_markers, provider, model_name, max_tokens, temperature,
                )

            has_drift, drift_note = self._check_structural_drift(line.file_path, content, clean)
            legacy_note = self._legacy_marker_note(legacy_markers)
            needs_review = has_drift or bool(legacy_markers)
            combined_note = " ".join(note for note in (drift_note, legacy_note) if note) or False
            line.write({
                "state": "review" if needs_review else "migrated",
                "skip_reason": combined_note,
                "migrated_content": clean,
            })
            self._log_migration_event(
                "review_needed" if needs_review else "migrated",
                combined_note or (
                    _("AI returned migrated content (%s characters), passed syntax and target-API validation.") % len(clean)
                ),
                level="warning" if needs_review else "success",
                migration_file=line,
                provider=provider,
                model=model_name,
                max_tokens=max_tokens,
                duration=time.monotonic() - started_at,
                response_preview=clean,
                immediate=True,
            )

    def _post_migration_legacy_markers(self, path, content):
        """Deterministic, provider-independent check for Odoo APIs/syntax that must not
        survive a migration to self.target_version.

        This is the core of "accurate no matter which AI is used": a regex either
        matches or it doesn't, so it flags exactly the same leftover legacy code
        whether the file was migrated by GPT-4.1, Gemini, Claude, or a free-tier
        local Ollama model. It runs AFTER syntax validation passes, since only
        syntactically valid output reaches here.
        """
        target = int(self.target_version or 0)
        basename = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()
        is_manifest = basename in MANIFEST_NAMES
        hits = []

        def add(pattern, message, flags=re.IGNORECASE):
            if re.search(pattern, content, flags):
                hits.append(message)

        if ext == ".py" or is_manifest:
            if target >= 13:
                add(r"@api\.(multi|one)\b", _("@api.multi/@api.one (removed in Odoo 13)"))
                add(r"\bosv\.osv\b|\bfrom\s+openerp\b|\bimport\s+openerp\b", _("legacy openerp/osv import"))
                add(r"\bcr\s*,\s*uid\s*,\s*ids\s*,\s*context\b", _("old (cr, uid, ids, context) method signature"))
            if target >= 13 and not is_manifest:
                add(r"\btrack_visibility\s*=", _("track_visibility= (use tracking= instead)"))
            if target >= 17 and not is_manifest:
                add(r"\bfields_view_get\s*\(", _("fields_view_get() (replaced by get_view in Odoo 17+)"))

        if is_manifest:
            expected_prefix = "%s.0" % target
            version_match = re.search(r"['\"]version['\"]\s*:\s*['\"]([^'\"]+)['\"]", content)
            if version_match and not version_match.group(1).startswith(expected_prefix):
                hits.append(_("manifest 'version' was not updated to start with %s") % expected_prefix)
            if target >= 15 and re.search(r"['\"]qweb['\"]\s*:", content):
                hits.append(_("legacy 'qweb' manifest key (use an assets bundle instead)"))

        if ext == ".xml":
            if target >= 17:
                add(r"\battrs\s*=\s*[\"']", _("attrs= (removed in Odoo 17+ views)"))
                add(r"\bstates\s*=\s*[\"']", _("states= view modifier (removed in Odoo 17+ views)"))
                add(
                    r"<field\s+name=[\"']view_type[\"']",
                    _("view_type field on ir.actions.act_window (field no longer exists — remove it)"),
                )
                # The single most damaging way to "convert" attrs=/states=: keep the
                # old domain as the new modifier's value. Odoo 17+ evaluates these
                # attributes as Python expressions (web/core/py_js/py.js does
                # bool(expr)), so a non-empty list literal is ALWAYS truthy — every
                # such button or field becomes permanently invisible/readonly. It
                # passes XML validation and leaves no attrs=/states= behind, so
                # nothing else here would catch it.
                add(
                    r"\b(?:column_invisible|invisible|readonly|required)\s*=\s*[\"']\s*\[",
                    _("a view modifier still holds a domain list (e.g. invisible=\"[('state','!=','draft')]\"); "
                      "Odoo 17+ evaluates these as Python expressions, so a list is always True and the "
                      "element is permanently hidden — use invisible=\"state != 'draft'\""),
                )
                add(
                    r"<field\s+name=[\"']groups_id[\"']",
                    _("groups_id field (renamed to group_ids across the framework)"),
                )
            if target >= 18:
                add(r"<tree(\s|>)", _("<tree> view tag (renamed to <list> in Odoo 18+)"))
                add(
                    r"<field\s+name=[\"'](?:view_mode|mobile_view_mode)[\"']>[^<]*\btree\b[^<]*</field>",
                    _("view_mode/mobile_view_mode still lists 'tree' (rename to 'list' for Odoo 18+)"),
                )

        if ext == ".js" and target >= 16:
            add(r"\bodoo\.define\s*\(", _("legacy odoo.define() AMD module (use /** @odoo-module **/ ES6 imports)"))

        return hits

    def _legacy_marker_note(self, markers):
        if not markers:
            return False
        return _(
            "Migrated output still looks outdated for Odoo %(target)s: %(markers)s. "
            "Verify before trusting this file."
        ) % {
            "target": self.target_version,
            "markers": "; ".join(markers),
        }

    def _max_migration_attempts(self):
        """How many times a single file may be sent to the provider before giving up.

        Covers the "the model would not do it" case, not the "the model got it
        wrong" case: each attempt is a fresh, firmer demand for the file after a
        refusal/SKIP/prose reply. Provider-independent by construction — it is
        enforced here, on whatever text came back, rather than relying on any
        provider's own retry or refusal semantics.
        """
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "ai_module_migrator.max_migration_attempts"
        )
        try:
            return max(1, min(6, int(raw or 3)))
        except (TypeError, ValueError):
            return 3

    def _is_empty_response_error(self, error):
        return "did not return text" in str(error or "").lower()

    def _looks_like_file_content(self, path, text):
        """Cheap structural check: does this text plausibly contain file content at all?

        Used to tell "the model returned the file" from "the model returned an
        apology". Deliberately permissive — its only job is to catch answers with
        no code in them, not to judge migration quality (that is what
        _validate_migrated_content and _post_migration_legacy_markers do).
        """
        if not text:
            return False
        ext = os.path.splitext(path)[1].lower()
        basename = os.path.basename(path)
        if ext == ".xml":
            return "<" in text and ">" in text
        if ext == ".py" or basename in MANIFEST_NAMES:
            return any(token in text for token in ("import ", "def ", "class ", "{", "=", "#"))
        if ext in (".js", ".scss", ".css"):
            return any(token in text for token in ("{", "}", "import ", "function", ";", ":"))
        if ext == ".csv":
            return "," in text
        return "\n" in text or len(text) > 80

    def _detect_non_answer(self, clean, source_content, path):
        """Return a human-readable reason if the provider dodged the task, else False.

        A "non-answer" is a reply that is not a migration attempt at all: SKIP, a
        refusal, an apology, a description of the work instead of the work, or a
        stub so short it cannot be the file. This is checked on the raw text, so
        it behaves identically for OpenAI, Gemini, Anthropic, Groq, DeepSeek,
        Ollama and any OpenAI-compatible endpoint — none of which report "I
        declined" in a machine-readable way.
        """
        if not clean:
            return _("the provider returned an empty response")
        if clean.upper().rstrip(".") == "SKIP":
            return _("the provider replied SKIP instead of returning the migrated file")
        if not self._looks_like_file_content(path, clean):
            return _("the provider returned prose or a refusal instead of file content")
        refusal = re.compile(
            r"\b(?:i (?:cannot|can't|can not|won't|will not|am unable|am not able)"
            r"|i'm (?:unable|not able|sorry)"
            r"|as an ai\b"
            r"|i apologi[sz]e"
            r"|unable to (?:migrate|convert|complete|process))",
            re.IGNORECASE,
        )
        # Only treat refusal phrasing as a refusal in a SHORT reply: the same
        # words legitimately occur inside a real file's comments or strings.
        if len(clean) < 400 and refusal.search(clean):
            return _("the provider replied with a refusal instead of the migrated file")
        source_length = len((source_content or "").strip())
        if source_length > 400 and len(clean) < max(60, int(source_length * 0.25)):
            return _(
                "the provider returned %(got)s characters for a %(expected)s character file, "
                "which is a stub rather than the migrated file"
            ) % {"got": len(clean), "expected": source_length}
        return False

    def _build_forced_retry_prompt(self, path, source_content, bad_response, reason, attempt, total):
        """Re-ask for the file after a non-answer, naming exactly what was wrong.

        The original source is re-sent because the previous reply carried none of
        it, so there is nothing to correct — this is a fresh attempt, not a patch.
        """
        file_type = self._file_type_label(path)
        return (
            "Your previous reply for this Odoo migration was REJECTED and not used.\n\n"
            "Reason it was rejected: %(reason)s.\n\n"
            "This is attempt %(attempt)s of %(total)s. An automated system — not a human — is "
            "consuming your answer, and it can only accept the full text of the migrated file. "
            "Anything else (SKIP, a refusal, an apology, an explanation, a summary, a diff, a "
            "partial excerpt, or a note about what you would change) is discarded and the file is "
            "left unmigrated. There is no reviewer on the other end who will read your reasoning.\n\n"
            "You MUST now return the complete content of the file below, migrated from Odoo "
            "%(source)s to Odoo %(target)s.\n\n"
            "How to handle uncertainty — this is the important part: you are NOT required to be "
            "confident about every line. Migrate every construct you are sure about, and copy any "
            "construct you are unsure about through EXACTLY as it appears in the source. Returning "
            "the file with only the changes you are certain of is a correct, expected answer. "
            "Returning nothing is the only wrong answer.\n\n"
            "Hard requirements:\n"
            "- Output ONLY the file content: no markdown fences, no preamble, no commentary.\n"
            "- Every line of the source must have a corresponding line in your output, in order.\n"
            "- The output must be syntactically valid %(file_type)s.\n"
            "- Do not invent fields, methods, models, XML ids or APIs that you are not sure exist "
            "in Odoo %(target)s — copy the original through instead.\n\n"
            "%(previous)s"
            "File: %(path)s\n"
            "Source Odoo: %(source)s\n"
            "Target Odoo: %(target)s\n\n"
            "--- BEGIN FILE ---\n%(content)s\n--- END FILE ---\n\n"
            "Return the complete migrated file content only, starting with its first line."
        ) % {
            "reason": reason,
            "attempt": attempt,
            "total": total,
            "source": self.source_version,
            "target": self.target_version,
            "file_type": file_type,
            "path": path,
            "content": source_content,
            "previous": (
                "Your rejected reply began:\n--- REJECTED REPLY ---\n%s\n--- END ---\n\n"
                % bad_response[:600]
            ) if bad_response else "",
        }

    def _force_file_migration(self, line, response, source_content, provider, model_name,
                              max_tokens, temperature, started_at):
        """Keep demanding the migrated file until we get one or run out of attempts.

        Returns (content, reason_still_refused_or_False).
        """
        clean = (response or "").strip()
        reason = self._detect_non_answer(clean, source_content, line.file_path)
        if not reason:
            return clean, False

        service = self.env["ai.module.migrator.provider.service"]
        total = self._max_migration_attempts()
        for attempt in range(2, total + 1):
            self._log_migration_event(
                "force_retry",
                _("Attempt %(attempt)s/%(total)s rejected — %(reason)s. Re-asking %(provider)s for the file.") % {
                    "attempt": attempt - 1, "total": total, "reason": reason, "provider": provider,
                },
                level="warning",
                migration_file=line,
                provider=provider,
                model=model_name,
                max_tokens=max_tokens,
                duration=time.monotonic() - started_at,
                response_preview=clean,
                immediate=True,
            )
            # A model that refused once often refuses identically at the same
            # temperature; nudge it up slightly from the third attempt on to
            # break a deterministic refusal, while staying low enough that the
            # migration itself does not become creative.
            retry_temperature = temperature if attempt == 2 else min(0.6, temperature + 0.2)
            try:
                retry_response = service.generate_text(
                    self._build_forced_retry_prompt(
                        line.file_path, source_content, clean, reason, attempt, total,
                    ),
                    provider=self.provider,
                    model=self.model_override,
                    max_tokens=max_tokens,
                    temperature=retry_temperature,
                )
            except Exception as error:
                self._log_migration_event(
                    "force_retry_failed",
                    _("Attempt %(attempt)s/%(total)s could not reach the provider: %(error)s") % {
                        "attempt": attempt, "total": total, "error": str(error)[:240],
                    },
                    level="error",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    immediate=True,
                )
                continue

            retry_clean = (retry_response or "").strip()
            retry_reason = self._detect_non_answer(retry_clean, source_content, line.file_path)
            if not retry_reason:
                self._log_migration_event(
                    "force_retry_success",
                    _("Attempt %(attempt)s/%(total)s returned the file (%(chars)s characters).") % {
                        "attempt": attempt, "total": total, "chars": len(retry_clean),
                    },
                    level="success",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    duration=time.monotonic() - started_at,
                    immediate=True,
                )
                return retry_clean, False
            clean, reason = retry_clean, retry_reason

        return clean, reason

    def _retry_legacy_markers(self, line, content, markers, provider, model_name, max_tokens, temperature):
        """One extra, laser-targeted retry when syntactically valid output still contains
        constructs _post_migration_legacy_markers() flagged.

        This is what makes accuracy independent of model strength: even a free/local
        model that "forgot" a well-known conversion (attrs=, @api.multi, tree->list, ...)
        gets a second, very specific chance naming exactly what's wrong, instead of the
        outdated code silently shipping as "migrated".
        """
        service = self.env["ai.module.migrator.provider.service"]
        fix_prompt = self._build_legacy_marker_fix_prompt(line.file_path, content, markers)
        try:
            retry_response = service.generate_text(
                fix_prompt,
                provider=self.provider,
                model=self.model_override,
                max_tokens=max_tokens,
                temperature=min(temperature, 0.1),
            )
        except Exception:
            return content, markers

        retry_clean = (retry_response or "").strip()
        if not retry_clean or self._detect_non_answer(retry_clean, content, line.file_path):
            return content, markers

        is_valid, _validation_error = self._validate_migrated_content(line.file_path, retry_clean)
        if not is_valid:
            return content, markers

        return retry_clean, self._post_migration_legacy_markers(line.file_path, retry_clean)

    def _build_legacy_marker_fix_prompt(self, path, content, markers):
        """Ask the same AI to remove specific outdated API usage its own output still contains."""
        return (
            "Your previous migration for this file is syntactically valid, but a deterministic, "
            "non-AI check found it still contains constructs that Odoo %(target)s does not use. "
            "This check runs the same way regardless of which AI produced the output, so it must "
            "be fixed now rather than left for a human to notice later.\n\n"
            "Outdated construct(s) still present:\n%(markers)s\n\n"
            "Fix ONLY these outdated constructs, converting each to its correct Odoo %(target)s "
            "equivalent. Do not touch any other part of the file, do not reformat unrelated lines, "
            "and do not second-guess other migration decisions you already made. Before answering, "
            "re-check that none of the listed constructs remain anywhere in the file.\n\n"
            "Return ONLY the corrected full file content — no explanations, no markdown fences, no "
            "preamble. Returning the file is mandatory: do not reply SKIP and do not refuse. If you are "
            "unsure about one of the listed constructs, convert the ones you are sure about and leave that "
            "single construct as-is — but still return the whole file.\n\n"
            "File: %(path)s\n\n"
            "--- YOUR PREVIOUS OUTPUT ---\n%(content)s\n--- END ---\n\n"
            "Return the complete corrected file content only."
        ) % {
            "target": self.target_version,
            "markers": "\n".join("- %s" % marker for marker in markers),
            "path": path,
            "content": content,
        }

    def _retry_invalid_migration(self, line, broken_content, validation_error, provider, model_name, max_tokens, temperature):
        """Make the AI repair syntactically invalid output, re-validating after each try.

        Returns (content, is_valid, error_message_or_False). Works identically for every
        configured provider since it goes through the same generate_text() call.

        Retries up to Max Migration Attempts times rather than exactly once: a model
        that produced broken output on its first pass frequently fixes it on the
        second or third when told precisely what the parser rejected, and one shot
        was leaving files marked Error that a further attempt would have salvaged.
        """
        service = self.env["ai.module.migrator.provider.service"]
        attempts = self._max_migration_attempts()
        content = broken_content
        for attempt in range(2, attempts + 1):
            fix_prompt = self._build_validation_fix_prompt(line.file_path, content, validation_error)
            try:
                retry_response = service.generate_text(
                    fix_prompt,
                    provider=self.provider,
                    model=self.model_override,
                    max_tokens=max_tokens,
                    temperature=min(temperature, 0.1),
                )
            except Exception as error:
                return content, False, _("%s (retry request failed: %s)") % (
                    validation_error, str(error)[:180],
                )

            retry_clean = (retry_response or "").strip()
            # A refusal here is not a usable repair, but it is also not a reason to
            # stop: re-ask with the same instruction until attempts run out.
            if not retry_clean or self._detect_non_answer(retry_clean, broken_content, line.file_path):
                self._log_migration_event(
                    "validation_retry_refused",
                    _("Repair attempt %(attempt)s/%(total)s did not return a file; re-asking.") % {
                        "attempt": attempt, "total": attempts,
                    },
                    level="warning",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    immediate=True,
                )
                continue

            is_valid, new_error = self._validate_migrated_content(line.file_path, retry_clean)
            if is_valid:
                self._log_migration_event(
                    "validation_repaired",
                    _("Repair attempt %(attempt)s/%(total)s produced valid content.") % {
                        "attempt": attempt, "total": attempts,
                    },
                    level="success",
                    migration_file=line,
                    provider=provider,
                    model=model_name,
                    max_tokens=max_tokens,
                    immediate=True,
                )
                return retry_clean, True, False
            content, validation_error = retry_clean, new_error

        return content, False, validation_error

    def job_finalize_code_migration(self):
        self.ensure_one()
        self._check_manager_access()
        started_at = time.monotonic()
        self._log_migration_event(
            "finalize_start",
            _("All file jobs finished; building migrated zip."),
            level="info",
            immediate=True,
        )
        pending = self.migration_file_ids.filtered(lambda line: line.state == "pending")
        if pending:
            self.write({
                "last_error": _("%s file job(s) are still pending; final zip was not built.") % len(pending),
            })
            self._log_migration_event(
                "finalize_blocked",
                _("%s file job(s) are still pending; final zip was not built.") % len(pending),
                level="warning",
                duration=time.monotonic() - started_at,
                immediate=True,
            )
            raise UserError(self.last_error)

        files, _root_label = self._load_source_files()
        migrated_contents = {}
        skipped = []
        needs_review = []
        for line in self.migration_file_ids.sorted("file_path"):
            file_data = files.get(line.file_path)
            if not file_data:
                skipped.append("%s  [%s]" % (line.file_path, _("Source file missing.")))
                continue

            content = file_data.get("content")
            # The source file, unchanged, in the form _build_migrated_zip wants:
            # None means "binary, copy raw bytes from source".
            original = None if not file_data.get("readable") or content is False else content or ""
            if line.state == "skipped":
                # A "SKIP" verdict from the AI means "this file needs no
                # changes for the target version" — NOT "drop this file".
                # Leaving it out of migrated_contents excluded it from both
                # the zip and the output directory, so the migrated module
                # shipped incomplete: the file simply vanished, with nothing
                # but a line in the skipped list to say so. That breaks the
                # module whenever something else still references it (an OWL
                # .xml template whose .js registers it, a data file listed in
                # __manifest__.py, ...). Carry the original through unchanged,
                # exactly like a "copied" file, and still report it as skipped.
                skipped.append(line.file_path)
                migrated_contents[line.file_path] = original
                continue
            if line.state == "review":
                migrated_contents[line.file_path] = self._with_trailing_newline(line.migrated_content or "")
                needs_review.append("%s  [%s]" % (line.file_path, line.skip_reason or _("Needs manual review.")))
                continue
            if line.state == "migrated":
                migrated_contents[line.file_path] = self._with_trailing_newline(line.migrated_content or "")
                continue
            if line.state in ("copied", "error"):
                if line.state == "error":
                    skipped.append("%s  [%s]" % (line.file_path, line.skip_reason or _("AI error.")))
                migrated_contents[line.file_path] = original

        zip_bytes, zip_name = self._build_migrated_zip(files, migrated_contents)
        if self.output_server_path:
            self._write_migrated_to_path(files, migrated_contents, self.output_server_path)
        if self.output_zip_attachment_id:
            self.output_zip_attachment_id.unlink()

        attachment = self.env["ir.attachment"].create({
            "name": zip_name,
            "type": "binary",
            "datas": base64.b64encode(zip_bytes).decode("ascii"),
            "res_model": self._name,
            "res_id": self.id,
            "mimetype": "application/zip",
        })

        self.write({
            "state": "migrated",
            "output_zip_attachment_id": attachment.id,
            "migrated_file_count": len(migrated_contents),
            "skipped_file_count": len(skipped),
            "skipped_file_list": "\n".join(skipped) if skipped else False,
            "review_file_count": len(needs_review),
            "review_file_list": "\n".join(needs_review) if needs_review else False,
            "last_generated_at": fields.Datetime.now(),
            "last_error": False,
        })
        self.message_post(
            body=Markup("<p>%s</p>") % _(
                "Code migration complete — %s files output, %s skipped, %s flagged for manual review."
            ) % (len(migrated_contents), len(skipped), len(needs_review))
        )
        self._log_migration_event(
            "finalize_complete",
            _("Migrated zip is ready: %s files output, %s skipped/error file(s), %s flagged for review.") % (
                len(migrated_contents), len(skipped), len(needs_review),
            ),
            level="success" if not skipped and not needs_review else "warning",
            duration=time.monotonic() - started_at,
            immediate=True,
        )

    def _prepare_migration_file_line(self, path, file_data):
        content = file_data.get("content")
        if not file_data.get("readable") or content is False:
            return self._record_migration_file(
                path,
                file_data,
                "copied",
                _("Binary, oversized, or unreadable file copied unchanged."),
            )
        if not self._should_migrate_file_with_ai(path, file_data):
            return self._record_migration_file(
                path,
                file_data,
                "copied",
                _("Non-code text file copied unchanged."),
                content or "",
            )
        if not (content or "").strip():
            return self._record_migration_file(
                path,
                file_data,
                "copied",
                _("Empty file copied unchanged."),
                content or "",
            )
        return self._record_migration_file(path, file_data, "pending", _("Queued for AI migration."))

    def _should_migrate_file_with_ai(self, path, file_data):
        if not file_data.get("readable") or file_data.get("content") is False:
            return False
        migratable_ext = {".py", ".xml", ".js", ".scss", ".css", ".html", ".jinja", ".j2"}
        return os.path.basename(path) in MANIFEST_NAMES or os.path.splitext(path)[1].lower() in migratable_ext

    def _refresh_migration_counts(self):
        lines = self.migration_file_ids
        skipped_lines = lines.filtered(lambda line: line.state in ("skipped", "error"))
        output_lines = lines.filtered(lambda line: line.state in ("migrated", "copied", "error"))
        self.write({
            "migrated_file_count": len(output_lines),
            "skipped_file_count": len(skipped_lines),
            "skipped_file_list": "\n".join(
                "%s  [%s]" % (
                    line.file_path,
                    line.skip_reason or dict(line._fields["state"].selection).get(line.state, line.state),
                )
                for line in skipped_lines.sorted("file_path")
            ) or False,
        })

    def _log_migration_event(
        self,
        event,
        message,
        level="info",
        migration_file=False,
        file_path=False,
        provider=False,
        model=False,
        max_tokens=0,
        duration=0.0,
        prompt_preview=False,
        response_preview=False,
        immediate=False,
    ):
        self.ensure_one()

        def _trim(value, limit):
            if not value:
                return False
            value = str(value)
            return value if len(value) <= limit else value[:limit] + "\n...[truncated]"

        values = {
            "job_id": self.id,
            "migration_file_id": migration_file.id if migration_file else False,
            "file_path": file_path or (migration_file.file_path if migration_file else False),
            "level": level or "info",
            "event": str(event or "log"),
            "provider": provider or False,
            "model": model or False,
            "max_tokens": int(max_tokens or 0),
            "duration": round(float(duration or 0.0), 2),
            "message": _trim(message, 2000) or "",
            "prompt_preview": _trim(prompt_preview, 6000),
            "response_preview": _trim(response_preview, 6000),
        }
        try:
            if immediate:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env["ai.module.migration.log"].sudo().create(values)
            else:
                return self.env["ai.module.migration.log"].sudo().create(values)
        except Exception:
            return False
        return True

    def _load_source_files(self):
        self.ensure_one()
        if self.input_mode == "upload":
            return self._load_archive_files()
        return self._load_filesystem_files()

    def _load_filesystem_files(self):
        source_path = (self.source_path or "").strip()
        if not source_path:
            raise UserError(_("Set a source addon path."))
        root = os.path.abspath(os.path.expanduser(source_path))
        self._assert_allowed_path(root)
        if not os.path.isdir(root):
            raise UserError(_("Source path is not a directory: %s") % root)
        manifest_path = self._find_manifest_path(root)
        if not manifest_path:
            raise UserError(_("No __manifest__.py or __openerp__.py found in %s.") % root)

        max_file_bytes = self._max_file_bytes()
        files = {}
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [dirname for dirname in dirnames if dirname not in IGNORED_DIRS]
            for filename in filenames:
                full_path = os.path.join(current_root, filename)
                relative = os.path.relpath(full_path, root).replace(os.sep, "/")
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                content = self._read_text_file(full_path, size, max_file_bytes)
                files[relative] = {
                    "size": size,
                    "content": content,
                    "readable": content is not False,
                }
        return files, root

    def _load_archive_files(self):
        if not self.source_archive:
            raise UserError(_("Upload a zip file containing one Odoo addon."))
        max_file_bytes = self._max_file_bytes()
        raw = base64.b64decode(self.source_archive)
        files = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    normalized = item.filename.replace("\\", "/").lstrip("/")
                    if ".." in normalized.split("/"):
                        continue
                    relative = self._strip_archive_root(normalized, archive.namelist())
                    if not relative:
                        continue
                    content = False
                    if self._is_text_path(relative) and item.file_size <= max_file_bytes:
                        content = archive.read(item).decode("utf-8", errors="replace")
                    files[relative] = {
                        "size": item.file_size,
                        "content": content,
                        "readable": content is not False,
                    }
        except zipfile.BadZipFile as error:
            raise UserError(_("The uploaded file is not a valid zip archive.")) from error
        if not any(name in files for name in MANIFEST_NAMES):
            raise UserError(_("The uploaded zip must contain an addon manifest at its root or inside one top-level folder."))
        return files, self.source_archive_filename or _("uploaded archive")

    def _strip_archive_root(self, path, all_names):
        names = [name.replace("\\", "/").lstrip("/") for name in all_names if name and not name.endswith("/")]
        roots = {name.split("/", 1)[0] for name in names if "/" in name}
        root_manifest = any(name in MANIFEST_NAMES for name in names)
        if root_manifest:
            return path
        if len(roots) == 1 and "/" in path:
            return path.split("/", 1)[1]
        return path

    def _analyze_files(self, files, root_label):
        manifest_name = next((name for name in MANIFEST_NAMES if name in files), False)
        if not manifest_name:
            raise UserError(_("No manifest file was found."))
        manifest_text = files[manifest_name]["content"] or ""
        manifest_data = self._parse_manifest(manifest_text)
        detected_version, version_note, version_evidence, version_confidence = self._detect_source_version(
            manifest_name,
            manifest_data,
            files,
        )
        source_version = detected_version or self.source_version
        module_name = self._module_name_from_source(root_label, manifest_data)
        dependencies = manifest_data.get("depends") or []
        dependency_info = self._dependency_info(dependencies)
        dependency_handling = self.dependency_handling
        if dependencies and dependency_handling not in ("include", "ignore"):
            dependency_handling = "review"
        elif not dependencies:
            dependency_handling = "ignore"
        dependency_context = (
            self._dependency_context_excerpts(dependencies)
            if dependency_handling == "include"
            else False
        )
        inventory = self._inventory(files)
        symbol_map = self._build_module_symbol_map(files)
        findings = self._static_findings(files, manifest_data, dependency_info, source_version=source_version)
        counts = Counter(self._file_kind(path) for path in files)
        total_bytes = sum(file_data["size"] for file_data in files.values())
        source_edition = self._detect_source_edition(root_label, dependency_info)
        digest = self._source_fingerprint(files, manifest_text)
        summary = self._source_summary(
            module_name,
            manifest_name,
            manifest_data,
            counts,
            total_bytes,
            source_edition,
            root_label,
            source_version=source_version,
        )
        return {
            "name": "%s: Odoo %s to %s" % (module_name, source_version, self.target_version),
            "source_version": source_version,
            "source_version_note": version_note,
            "source_version_confidence": version_confidence,
            "source_version_evidence": version_evidence,
            "module_name": module_name,
            "manifest_name": manifest_name,
            "manifest_text": manifest_text,
            "manifest_data_json": json.dumps(manifest_data, indent=2, sort_keys=True),
            "dependency_names": "\n".join(dependencies) if dependencies else False,
            "dependency_handling": dependency_handling,
            "dependency_summary": dependency_info["summary"],
            "dependency_context": dependency_context,
            "local_index_summary": dependency_info["index_summary"],
            "module_symbol_map": symbol_map,
            "source_edition": source_edition,
            "source_summary": summary,
            "static_findings": findings,
            "file_inventory": inventory,
            "file_count": len(files),
            "python_file_count": counts.get("python", 0),
            "xml_file_count": counts.get("xml", 0),
            "js_file_count": counts.get("javascript", 0),
            "total_bytes": total_bytes,
            "source_fingerprint": digest,
            "last_scan_at": fields.Datetime.now(),
            "state": "scanned",
            "last_error": False,
        }

    def _parse_manifest(self, manifest_text):
        try:
            data = ast.literal_eval(manifest_text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _detect_source_version(self, manifest_name, manifest_data, files):
        valid_versions = dict(ODOO_VERSION_SELECTION)
        scores = Counter()
        evidence = []

        def add(version, weight, reason):
            version = str(version)
            if version not in valid_versions:
                return
            scores[version] += weight
            evidence.append("+%s Odoo %s: %s" % (weight, version, reason))

        version = str(manifest_data.get("version") or "").strip()
        match = re.match(r"^(1[1-9])(?:\.|$)", version)
        if match and match.group(1) in valid_versions:
            detected = match.group(1)
            add(detected, 100, _("manifest version is %s") % version)

        if manifest_name == "__openerp__.py":
            add("11", 90, _("legacy __openerp__.py manifest"))

        all_text = "\n".join(
            file_data.get("content") or ""
            for file_data in files.values()
            if file_data.get("content")
        )
        # Markers below are split into two tiers. "Narrow" markers are
        # reasonably specific to one Odoo version (or version boundary) and
        # can, on their own, justify picking a specific version. "Wide"
        # markers reflect syntax that stayed valid across many Odoo
        # releases (e.g. attrs=/states= worked from Odoo 8 through 16) — by
        # themselves they only rule a version range in or out, they do NOT
        # prove a specific version, so their weight is intentionally small
        # and they cannot push the result past "inconclusive" alone.
        marker_rules = [
            ("11", 35, r"\b(from|import)\s+openerp\b|\bosv\.osv\b|\bcr,\s*uid,\s*ids,\s*context\b", _("OpenERP/old API markers"), "narrow"),
            ("11", 25, r"@api\.(multi|one)\b", _("legacy @api.multi/@api.one decorators (removed in Odoo 13)"), "narrow"),
            ("13", 25, r"\btrack_visibility\s*=", _("track_visibility field parameter (removed in Odoo 13)"), "narrow"),
            ("14", 15, r"\btracking\s*=", _("tracking field parameter"), "narrow"),
            ("14", 10, r"\bcompute_sudo\s*=", _("modern field parameter compute_sudo"), "narrow"),
            ("15", 25, r"['\"]assets['\"]\s*:", _("manifest assets dictionary (introduced Odoo 15)"), "narrow"),
            ("15", 15, r"\bweb\.assets_(backend|frontend|qweb)\b", _("modern web asset bundle keys"), "narrow"),
            ("16", 12, r"\battrs\s*=|\bstates\s*=", _("legacy XML attrs/states modifiers — valid Odoo 8 through 16, not specific to 16 alone"), "wide"),
            ("16", 6, r"<tree[\s>]", _("tree views — valid Odoo 6 through 17, not specific to 16 alone"), "wide"),
            ("17", 15, r"\bCommand\.(create|update|delete|unlink|link|clear|set)\b", _("fields.Command helpers"), "narrow"),
            ("18", 15, r"<list[\s>]", _("list views"), "narrow"),
            ("19", 25, r"\bmodels\.Constraint\b", _("Odoo 19 model Constraint API"), "narrow"),
            ("19", 20, r"@http\.route\([^)]*type=['\"]jsonrpc['\"]", _("jsonrpc route type"), "narrow"),
        ]
        narrow_scores = Counter()
        for version_key, weight, pattern, reason, tier in marker_rules:
            if re.search(pattern, all_text):
                add(version_key, weight, reason)
                if tier == "narrow":
                    narrow_scores[version_key] += weight

        # Minimum total score needed before we're willing to assert a
        # specific version rather than say "inconclusive". This also
        # requires at least some narrow (version-specific) evidence —
        # wide/ambiguous markers alone never clear this bar.
        MIN_CONFIDENT_SCORE = 40

        if scores:
            detected, score = scores.most_common(1)[0]
            exact_match = bool(match and detected == match.group(1))
            has_narrow_support = narrow_scores.get(detected, 0) > 0
            scoreboard = ", ".join(
                "Odoo %s=%s" % (version_key, score)
                for version_key, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
            )
            if exact_match:
                confidence = "exact"
                note = _("Odoo %s detected exactly from manifest version %s.") % (detected, version)
            elif score >= 80 and has_narrow_support:
                confidence = "high"
                note = _("Odoo %s selected by source-version consensus with high confidence.") % detected
            elif score >= MIN_CONFIDENT_SCORE and has_narrow_support:
                confidence = "medium"
                note = _("Odoo %s selected by source-version consensus with medium confidence.") % detected
            else:
                # Evidence is too thin, or comes only from wide/ambiguous
                # markers that don't actually pin a specific version.
                # Do NOT assert a specific version here — that would show
                # a confident-looking number the evidence doesn't support.
                # Leave the job's current source_version untouched and
                # tell the user this needs manual confirmation.
                confidence = "low"
                note = _(
                    "Could not confidently detect the source version from the manifest or code. "
                    "The strongest signal found (Odoo %s, score %s) is too weak or too generic to "
                    "trust — please confirm the real source version yourself (check the module's "
                    "App Store listing, its changelog, or the Odoo instance it was installed on) "
                    "and set it manually."
                ) % (detected, score)
                details = "\n".join([
                    "Weak/leading candidate: Odoo %s (not applied)" % detected,
                    "Confidence: low (inconclusive)",
                    "Scores: %s" % scoreboard,
                    "Evidence:",
                    "\n".join("- %s" % item for item in evidence),
                    "",
                    "Note: manifest 'version' field did not contain a recognizable Odoo version "
                    "prefix, and no strong version-specific code markers were found. This is common "
                    "for older third-party modules (e.g. from the Odoo App Store) that just use "
                    "'version': '0.1' style. Static analysis cannot reliably tell Odoo 11 from "
                    "Odoo 12-16 in that case — please set the source version manually.",
                ])
                return False, note, details, confidence

            details = "\n".join([
                "Selected: Odoo %s" % detected,
                "Confidence: %s" % confidence,
                "Scores: %s" % scoreboard,
                "Evidence:",
                "\n".join("- %s" % item for item in evidence),
            ])
            return detected, note, details, confidence

        details = "\n".join([
            "Selected: Odoo %s" % (self.source_version or "unknown"),
            "Confidence: low",
            "Scores: no version-specific markers found",
            "Evidence:",
            "- No manifest version or strong source markers were detected; kept the current source version field.",
        ])
        return False, _("Source version could not be detected from manifest or source markers."), details, "low"

    def _module_name_from_source(self, root_label, manifest_data):
        technical = manifest_data.get("technical_name") or manifest_data.get("name")
        if self.input_mode == "filesystem" and self.source_path:
            return os.path.basename(os.path.abspath(os.path.expanduser(self.source_path)))
        if technical:
            return self._slugify(str(technical))
        return "unknown_addon"

    def _dependency_info(self, dependencies):
        dependencies = [dep for dep in dependencies if dep]
        community_modules, enterprise_modules = self._local_module_indexes()
        enterprise_only = enterprise_modules - community_modules
        lines = []
        unknown = []
        enterprise_hits = []
        for dep in dependencies:
            if dep in community_modules:
                status = _("Community")
            elif dep in enterprise_only:
                status = _("Enterprise-only in local Odoo 19")
                enterprise_hits.append(dep)
            elif dep in enterprise_modules:
                status = _("Enterprise root")
                enterprise_hits.append(dep)
            else:
                status = _("Not found in configured local roots")
                unknown.append(dep)
            lines.append("- %s: %s" % (dep, status))
        if not lines:
            lines.append("- No manifest dependencies were detected.")
        index_summary = "\n".join([
            "Community modules indexed: %s" % len(community_modules),
            "Enterprise modules indexed: %s" % len(enterprise_modules),
            "Enterprise-only modules indexed: %s" % len(enterprise_only),
        ])
        return {
            "summary": "\n".join(lines),
            "unknown": unknown,
            "enterprise_hits": enterprise_hits,
            "index_summary": index_summary,
        }

    def _dependency_list(self):
        if self.dependency_names:
            return [dep.strip() for dep in re.split(r"[\n,]", self.dependency_names) if dep.strip()]
        if self.manifest_data_json:
            try:
                manifest_data = json.loads(self.manifest_data_json)
            except json.JSONDecodeError:
                manifest_data = {}
            return [dep for dep in manifest_data.get("depends") or [] if dep]
        return []

    def _check_dependency_decision(self):
        dependencies = self._dependency_list()
        if dependencies and self.dependency_handling == "review":
            raise UserError(_(
                "This module depends on: %s\n\n"
                "Choose either 'Include Dependency Context' or 'Do Not Include Dependencies' before generating an AI plan or migration."
            ) % ", ".join(dependencies))

    def _dependency_context_for_prompt(self):
        dependencies = self._dependency_list()
        if not dependencies:
            return _("No manifest dependencies were detected.")
        if self.dependency_handling != "include":
            return _("Dependency code context is disabled for this job.")
        context = self._dependency_context_excerpts(dependencies)
        if context != (self.dependency_context or ""):
            self.dependency_context = context
        return context

    def _dependency_context_excerpts(self, dependencies, budget=45000):
        dependencies = [dep for dep in dependencies if dep]
        if not dependencies:
            return _("No manifest dependencies were detected.")

        archive_modules = self._dependency_archive_modules()
        chunks = []
        used = 0
        for dependency in dependencies:
            if used >= budget:
                chunks.append("Dependency context budget exhausted.")
                break
            header = "\n### Dependency: %s\n" % dependency
            chunks.append(header)
            used += len(header)

            files = archive_modules.get(dependency)
            source_label = _("uploaded dependency archive")
            include_code = True
            if not files:
                module_path = self._find_dependency_module_path(dependency)
                if not module_path:
                    line = "Not found in local roots or dependency archive.\n"
                    chunks.append(line)
                    used += len(line)
                    continue
                files = self._read_dependency_local_files(module_path)
                source_label = module_path
                include_code = self._is_custom_dependency_path(module_path)

            source_line = "Source: %s\n" % source_label
            chunks.append(source_line)
            used += len(source_line)

            preferred = self._dependency_preferred_files(files, include_code=include_code)
            for path in preferred:
                content = files[path].get("content") or ""
                if not content:
                    continue
                remaining = budget - used
                if remaining <= 0:
                    break
                excerpt = content[:min(len(content), remaining, 7000)]
                block = "\n#### %s\n```%s\n%s\n```\n" % (path, self._fence_language(path), excerpt)
                chunks.append(block)
                used += len(block)
        return "".join(chunks).strip()

    def _dependency_archive_modules(self):
        if not self.dependency_archive:
            return {}
        raw_archive = base64.b64decode(self.dependency_archive)
        modules = {}
        try:
            with zipfile.ZipFile(io.BytesIO(raw_archive)) as archive:
                names = [name.replace("\\", "/").lstrip("/") for name in archive.namelist() if name and not name.endswith("/")]
                roots = {}
                for name in names:
                    if os.path.basename(name) in MANIFEST_NAMES and "/" in name:
                        root = name.rsplit("/", 1)[0]
                        roots[root.split("/")[-1]] = root
                for module_name, root in roots.items():
                    files = {}
                    prefix = root + "/"
                    for item in archive.infolist():
                        if item.is_dir():
                            continue
                        normalized = item.filename.replace("\\", "/").lstrip("/")
                        if ".." in normalized.split("/") or not normalized.startswith(prefix):
                            continue
                        relative = normalized[len(prefix):]
                        if not relative:
                            continue
                        content = False
                        if self._is_text_path(relative) and item.file_size <= self._max_file_bytes():
                            content = archive.read(item).decode("utf-8", errors="replace")
                        files[relative] = {
                            "size": item.file_size,
                            "content": content,
                            "readable": content is not False,
                        }
                    modules[module_name] = files
        except zipfile.BadZipFile:
            return {}
        return modules

    def _find_dependency_module_path(self, dependency):
        roots = self._existing_unique_roots(
            self._allowed_roots() + self._community_roots() + self._enterprise_roots()
        )
        for root in roots:
            path = os.path.join(root, dependency)
            if os.path.isdir(path) and self._find_manifest_path(path):
                return path
        return False

    def _read_dependency_local_files(self, module_path):
        files = {}
        max_file_bytes = self._max_file_bytes()
        for current_root, dirnames, filenames in os.walk(module_path):
            dirnames[:] = [dirname for dirname in dirnames if dirname not in IGNORED_DIRS]
            for filename in filenames:
                full_path = os.path.join(current_root, filename)
                relative = os.path.relpath(full_path, module_path).replace(os.sep, "/")
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                content = self._read_text_file(full_path, size, max_file_bytes)
                files[relative] = {
                    "size": size,
                    "content": content,
                    "readable": content is not False,
                }
        return files

    def _is_custom_dependency_path(self, module_path):
        return "/custom-addons/" in os.path.abspath(module_path)

    def _dependency_preferred_files(self, files, include_code=True):
        preferred = [name for name in MANIFEST_NAMES if name in files]
        if include_code:
            preferred.extend(sorted(path for path in files if path.endswith(".py") and path not in preferred))
            preferred.extend(sorted(path for path in files if path.endswith(".xml") and path not in preferred))
        return preferred[:18]

    def _local_module_indexes(self):
        community = self._module_names_from_roots(self._community_roots())
        enterprise = self._module_names_from_roots(self._enterprise_roots())
        return community, enterprise

    def _module_names_from_roots(self, roots):
        modules = set()
        for root in roots:
            if not os.path.isdir(root):
                continue
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if os.path.isdir(path) and self._find_manifest_path(path):
                    modules.add(name)
        return modules

    def _inventory(self, files):
        rows = ["Path | Kind | Bytes | Read", "--- | --- | ---: | ---"]
        for path in sorted(files):
            file_data = files[path]
            rows.append("%s | %s | %s | %s" % (
                path,
                self._file_kind(path),
                file_data["size"],
                "yes" if file_data["readable"] else "no",
            ))
        return "\n".join(rows)

    def _build_module_symbol_map(self, files):
        """One-time whole-module model/field index, built from the ORIGINAL source.

        Every per-file migration prompt sends this alongside the single file being
        migrated. Without it, each file is migrated in total isolation — fine for a
        strong model, but a weak/free model (e.g. a local Ollama model) is much more
        likely to invent a field name, drop a cross-file reference, or rename
        something inconsistently with a sibling file it never sees. This index is
        pure regex/text extraction (no AI involved), so its accuracy never depends
        on which provider or model is configured.
        """
        model_name_re = re.compile(r"_name\s*=\s*['\"]([\w.]+)['\"]")
        inherit_re = re.compile(r"_inherit\s*=\s*['\"]([\w.]+)['\"]")
        field_re = re.compile(r"^\s{4,}(\w+)\s*=\s*fields\.\w+\(", re.MULTILINE)
        models_map = {}
        for path in sorted(files):
            if not path.endswith(".py"):
                continue
            content = files[path].get("content") or ""
            if not content:
                continue
            name_match = model_name_re.search(content)
            inherit_match = inherit_re.search(content)
            model_name = name_match.group(1) if name_match else (inherit_match.group(1) if inherit_match else None)
            if not model_name:
                continue
            entry = models_map.setdefault(model_name, {"files": [], "fields": set()})
            entry["files"].append(path)
            entry["fields"].update(field_re.findall(content))

        if not models_map:
            return ""

        lines = []
        for model_name in sorted(models_map):
            entry = models_map[model_name]
            field_list = ", ".join(sorted(entry["fields"])[:25]) or "(no new fields declared here)"
            lines.append("- %s (in %s): fields %s" % (model_name, ", ".join(entry["files"]), field_list))
        return "\n".join(lines)

    def _static_findings(self, files, manifest_data, dependency_info, source_version=False):
        findings = []
        source = int(source_version or self.source_version or 0)
        target = int(self.target_version or 0)
        manifest_version = manifest_data.get("version")
        if manifest_version:
            findings.append("Manifest version: %s" % manifest_version)
        if self.manifest_name == "__openerp__.py" or "__openerp__.py" in files:
            findings.append("Legacy __openerp__.py manifest detected; Odoo 11+ uses __manifest__.py.")
        if dependency_info["enterprise_hits"]:
            findings.append("Enterprise dependency risk: %s" % ", ".join(dependency_info["enterprise_hits"]))
        if dependency_info["unknown"]:
            findings.append("Dependencies not found in configured local roots: %s" % ", ".join(dependency_info["unknown"]))

        pattern_rules = [
            (r"@api\.(multi|one)\b", "Old @api.multi/@api.one decorators need review for modern recordset APIs."),
            (r"\btrack_visibility\s*=", "track_visibility should become tracking=True in supported modern versions."),
            (r"\bfields_view_get\s*\(", "fields_view_get was replaced by get_view in newer Odoo versions."),
            (r"\bview_type\b", "view_type is obsolete on window actions."),
            (r"<tree(\s|>)", "Tree views should be checked for Odoo 18/19 list-view naming."),
            (
                r"<field\s+name=[\"'](?:view_mode|mobile_view_mode)[\"']>[^<]*\btree\b[^<]*</field>",
                "view_mode/mobile_view_mode still lists 'tree' — rename to 'list' for Odoo 18/19.",
            ),
            (
                r"<field\s+name=[\"']groups_id[\"']",
                "groups_id field was renamed to group_ids across the framework (ir.actions.act_window, "
                "ir.actions.server, ir.ui.menu, res.users, ...).",
            ),
            (r"\battrs\s*=", "attrs modifiers need conversion for Odoo 17+ XML views."),
            (r"\bstates\s*=", "states modifiers need conversion for Odoo 17+ XML views."),
            (r"odoo\.define\s*\(", "Legacy AMD JavaScript should be reviewed for modern @odoo-module style."),
            (r"\bqweb\s*:", "Legacy manifest qweb asset keys should be migrated to assets bundles."),
            (r"\bosv\.osv\b|from\s+openerp\b|import\s+openerp", "Very old API imports were detected."),
            (r"\bcr,\s*uid,\s*ids\b|\buid,\s*ids,\s*context\b", "Old API method signatures were detected."),
            (r"selection_add\s*=", "selection_add fields need ondelete policies in newer Odoo versions."),
        ]
        for pattern, message in pattern_rules:
            matches = self._matching_paths(files, pattern)
            if matches:
                findings.append("%s Files: %s" % (message, ", ".join(matches[:12])))

        if source <= 12 and target >= 17:
            findings.append("Large-version jump detected; split work into manifest/data, ORM, views, JS/assets, and tests.")
        if source < target and target >= 19:
            findings.append("Target is Odoo 19; validate list views, assets, OWL services, manifest version, and removed APIs.")
        if source > target:
            findings.append("Downgrade requested; identify target-missing APIs and avoid generating data that older Odoo cannot load.")
        if not findings:
            findings.append("No obvious static migration warnings were found. AI review is still recommended.")
        return "\n".join("- %s" % finding for finding in findings)

    def _matching_paths(self, files, pattern):
        regex = re.compile(pattern, re.IGNORECASE)
        paths = []
        for path, file_data in files.items():
            content = file_data.get("content")
            if content and regex.search(content):
                paths.append(path)
        return sorted(paths)

    def _detect_source_edition(self, root_label, dependency_info):
        enterprise_roots = self._enterprise_roots()
        root = os.path.abspath(root_label) if root_label and os.path.exists(root_label) else ""
        in_enterprise_root = any(root and self._is_child_path(root, enterprise_root) for enterprise_root in enterprise_roots)
        has_enterprise_dep = bool(dependency_info["enterprise_hits"])
        if in_enterprise_root and has_enterprise_dep:
            return "enterprise"
        if in_enterprise_root:
            return "enterprise"
        if has_enterprise_dep:
            return "mixed"
        if self.input_mode == "filesystem" and self.source_path:
            return "community"
        return "unknown"

    def _source_summary(self, module_name, manifest_name, manifest_data, counts, total_bytes, source_edition, root_label, source_version=False):
        source_version = source_version or self.source_version
        source_int = int(source_version or 0)
        target_int = int(self.target_version or 0)
        if source_int < target_int:
            direction = _("Upgrade")
        elif source_int > target_int:
            direction = _("Downgrade")
        else:
            direction = _("Same Version")
        lines = [
            "Module: %s" % module_name,
            "Input: %s" % root_label,
            "Manifest: %s" % manifest_name,
            "Manifest name: %s" % (manifest_data.get("name") or ""),
            "Source version: Odoo %s" % source_version,
            "Target version: Odoo %s" % self.target_version,
            "Direction: %s" % direction,
            "Detected edition: %s" % dict(self._fields["source_edition"].selection).get(source_edition, source_edition),
            "Files: %s" % sum(counts.values()),
            "Python/XML/JS: %s/%s/%s" % (counts.get("python", 0), counts.get("xml", 0), counts.get("javascript", 0)),
            "Total bytes: %s" % total_bytes,
        ]
        return "\n".join(lines)

    def _offline_report(self):
        return "\n\n".join([
            "# %s Migration Report" % (self.module_name or "Addon"),
            "## Source",
            self.source_summary or "Not scanned.",
            "## Local Addon Index",
            self.local_index_summary or "",
            "## Dependencies",
            self.dependency_summary or "",
            "## Static Findings",
            self.static_findings or "",
            "## Next Steps",
            self._offline_next_steps(),
        ])

    def _offline_next_steps(self):
        direction = dict(self._fields["direction"].selection).get(self.direction, self.direction)
        return "\n".join([
            "- Review Enterprise-only dependencies before deciding Community compatibility.",
            "- Convert the manifest to version %s.0.1.0.0 style if targeting Odoo %s." % (self.target_version, self.target_version),
            "- Update Python APIs, XML views, assets, and security files based on the static findings.",
            "- Install in a disposable database first, then run module update and targeted workflow tests.",
            "- Migration direction: %s." % direction,
        ])

    def _build_ai_prompt(self):
        self.ensure_one()
        files, root_label = self._load_source_files()
        excerpts = self._prompt_file_excerpts(files)
        dependency_context = self._dependency_context_for_prompt()
        output_instruction = {
            "report": "Return a structured migration report with risk levels and implementation order.",
            "patch_plan": "Return a patch-oriented plan. Include exact files to edit and representative code snippets, but do not invent unavailable files.",
            "rewrite_plan": "Return a file-by-file rewrite plan with the intended final structure for each changed file.",
            "review": "Return a compatibility review focused on blocking issues, risky APIs, and missing tests.",
        }.get(self.output_format, "Return a structured migration report.")
        return (
            "You are an expert Odoo migration engineer producing a report that a real developer will "
            "follow to migrate and test a production module. They will implement your plan and then run "
            "the module for real, so every claim you make must be something you are actually confident is "
            "correct for these exact Odoo versions — do not pad the report with plausible-sounding but "
            "unverified advice, and clearly flag anything you are uncertain about as needing manual "
            "verification rather than stating it as fact.\n"
            "Use only the scanned addon facts below. Do not assume dependencies exist unless they are in the local index summary.\n"
            "The task can be an upgrade, downgrade, or same-version compatibility migration.\n"
            "When downgrading, explicitly call out APIs, XML syntax, assets, or data constructs that the target version may not support.\n"
            "When targeting Odoo 18 or 19, pay special attention to list views, assets, OWL/web client code, manifest format, and removed ORM APIs.\n"
            "Prefer conservative, reviewable changes over broad rewrites.\n\n"
            "Source version: Odoo %(source)s\n"
            "Target version: Odoo %(target)s\n"
            "Direction: %(direction)s\n"
            "Requested output: %(output)s\n"
            "Extra instruction: %(instruction)s\n\n"
            "Source summary:\n%(summary)s\n\n"
            "Local Community/Enterprise index:\n%(index)s\n\n"
            "Manifest JSON:\n%(manifest_json)s\n\n"
            "Dependency assessment:\n%(dependencies)s\n\n"
            "Dependency handling: %(dependency_handling)s\n"
            "Dependency context:\n%(dependency_context)s\n\n"
            "Static findings:\n%(findings)s\n\n"
            "File inventory:\n%(inventory)s\n\n"
            "Important file excerpts:\n%(excerpts)s\n\n"
            "Required answer sections:\n"
            "1. Migration verdict\n"
            "2. Dependency and edition decisions\n"
            "3. Manifest changes\n"
            "4. Python ORM/API changes\n"
            "5. XML view/action/security/data changes\n"
            "6. JavaScript/assets changes\n"
            "7. Downgrade-specific or upgrade-specific traps\n"
            "8. Ordered implementation plan\n"
            "9. Install/update/test commands\n"
            "10. Open questions that require real runtime validation\n"
        ) % {
            "source": self.source_version,
            "target": self.target_version,
            "direction": dict(self._fields["direction"].selection).get(self.direction, self.direction),
            "output": output_instruction,
            "instruction": self.custom_instruction or "None",
            "summary": self.source_summary or "",
            "index": self.local_index_summary or "",
            "manifest_json": self.manifest_data_json or "{}",
            "dependencies": self.dependency_summary or "",
            "dependency_handling": dict(self._fields["dependency_handling"].selection).get(self.dependency_handling, self.dependency_handling),
            "dependency_context": dependency_context,
            "findings": self.static_findings or "",
            "inventory": self.file_inventory or "",
            "excerpts": excerpts,
        }

    def _prompt_file_excerpts(self, files):
        budget = self._prompt_max_chars()
        preferred = []
        for name in MANIFEST_NAMES:
            if name in files:
                preferred.append(name)
        preferred.extend(sorted(path for path in files if path.endswith(".py") and path not in preferred))
        preferred.extend(sorted(path for path in files if path.endswith(".xml") and path not in preferred))
        preferred.extend(sorted(path for path in files if path.endswith(".js") and path not in preferred))
        preferred.extend(sorted(path for path in files if path not in preferred and files[path].get("readable")))

        chunks = []
        used = 0
        for path in preferred:
            content = files[path].get("content")
            if not content:
                continue
            remaining = budget - used
            if remaining <= 0:
                break
            excerpt = content[:min(len(content), remaining, 12000)]
            chunks.append("\n### %s\n```%s\n%s\n```" % (path, self._fence_language(path), excerpt))
            used += len(excerpt)
        return "\n".join(chunks) or "No readable source excerpts were available."

    def _assert_allowed_path(self, path):
        allowed_roots = self._allowed_roots()
        if not allowed_roots:
            raise UserError(_("No allowed source roots are configured in AI Addon Migrator settings."))
        if not any(self._is_child_path(path, root) for root in allowed_roots):
            raise UserError(_("Path is outside configured allowed roots: %s") % path)

    def _allowed_roots(self):
        configured = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.allowed_roots")
        return self._split_roots(configured) or self._default_allowed_roots()

    def _community_roots(self):
        configured = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.community_roots")
        return self._split_roots(configured) or self._default_community_roots()

    def _enterprise_roots(self):
        configured = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.enterprise_roots")
        return self._split_roots(configured) or self._default_enterprise_roots()

    @api.model
    def _default_allowed_roots_text(self):
        return "\n".join(self._default_allowed_roots())

    @api.model
    def _default_community_roots_text(self):
        return "\n".join(self._default_community_roots())

    @api.model
    def _default_enterprise_roots_text(self):
        return "\n".join(self._default_enterprise_roots())

    @api.model
    def _default_allowed_roots(self):
        roots = []
        roots.extend(self._default_community_roots())
        roots.extend(self._default_enterprise_roots())
        roots.append("/tmp")
        roots.extend(self._root_values(tools.config.get("addons_path")))
        return self._existing_unique_roots(roots)

    @api.model
    def _default_community_roots(self):
        return [root for root in self._addons_path_roots() if not self._looks_like_enterprise_root(root)]

    @api.model
    def _default_enterprise_roots(self):
        return [root for root in self._addons_path_roots() if self._looks_like_enterprise_root(root)]

    @api.model
    def _addons_path_roots(self):
        """Community/Enterprise root candidates, derived from this server's own
        --addons-path rather than a hardcoded directory layout.

        The previous implementation guessed relative paths from this module's
        own file location (e.g. "../../../odoo_19.0+e.20260606/.../addons"),
        which only ever resolved on the one development machine that folder
        name came from — on any other server (a standard apt/deb install,
        Odoo.sh, a Docker image, a plain "sudo pip install odoo" layout) it
        silently found nothing, leaving Settings' Community/Enterprise Roots
        empty by default and dependency classification useless out of the
        box. Reading the addons_path Odoo itself was started with works
        anywhere, because it's the one piece of layout information every
        Odoo install already has correct.
        """
        return self._existing_unique_roots(
            self._root_values(tools.config.get("addons_path")) + [self._core_addons_root()]
        )

    @api.model
    def _core_addons_root(self):
        """Odoo's own bundled addons directory (the one holding "base").

        This is never part of --addons-path: the server adds it implicitly.
        Leaving it out meant every scanned module reported its "base"
        dependency as "Not found in configured local roots" — on every job,
        for every module, since practically all of them depend on base — and
        that false "missing dependency" went straight into the AI prompt.
        """
        return os.path.join(odoo.__path__[0], "addons")

    @api.model
    def _looks_like_enterprise_root(self, root):
        """Best-effort Enterprise/Community classification of an addons_path entry.

        Odoo Enterprise ships no machine-readable marker for "this directory
        is the Enterprise addons root" — so this checks for a handful of
        module names that have shipped Enterprise-only across Odoo 11-19
        (accounting, helpdesk, e-sign, timesheets grid, ...). A root counting
        as Enterprise only needs to contain ONE of them.
        """
        try:
            entries = set(os.listdir(root))
        except OSError:
            return False
        return bool(entries & ENTERPRISE_MARKER_MODULES)

    @api.model
    def _existing_unique_roots(self, roots):
        unique = []
        for root in roots:
            if not root:
                continue
            root = os.path.abspath(os.path.expanduser(root))
            if root not in unique and os.path.isdir(root):
                unique.append(root)
        return unique

    def _split_roots(self, value):
        if not value:
            return []
        roots = []
        for line in self._root_values(value):
            root = os.path.abspath(os.path.expanduser(line))
            if root and os.path.isdir(root):
                roots.append(root)
        return self._existing_unique_roots(roots)

    def _root_values(self, value):
        if not value:
            return []
        if isinstance(value, str):
            candidates = re.split(r"[\n,]", value)
        elif isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            candidates = [value]
        return [str(candidate).strip() for candidate in candidates if str(candidate).strip()]

    def _find_manifest_path(self, root):
        for manifest_name in MANIFEST_NAMES:
            path = os.path.join(root, manifest_name)
            if os.path.isfile(path):
                return path
        return False

    def _read_text_file(self, full_path, size, max_file_bytes):
        if not self._is_text_path(full_path) or size > max_file_bytes:
            return False
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(max_file_bytes)
        except OSError:
            return False

    def _is_text_path(self, path):
        return os.path.basename(path) in MANIFEST_NAMES or os.path.splitext(path)[1].lower() in TEXT_EXTENSIONS

    def _file_kind(self, path):
        ext = os.path.splitext(path)[1].lower()
        if os.path.basename(path) in MANIFEST_NAMES:
            return "manifest"
        if ext == ".py":
            return "python"
        if ext == ".xml":
            return "xml"
        if ext == ".js":
            return "javascript"
        if ext in (".scss", ".css"):
            return "style"
        if ext == ".csv":
            return "csv"
        if ext in (".md", ".rst", ".txt"):
            return "docs"
        if ext in TEXT_EXTENSIONS:
            return "text"
        return "binary"

    def _fence_language(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            ".py": "python",
            ".xml": "xml",
            ".js": "javascript",
            ".scss": "scss",
            ".css": "css",
            ".csv": "csv",
            ".json": "json",
            ".md": "markdown",
        }.get(ext, "")

    def _source_fingerprint(self, files, manifest_text):
        digest = hashlib.sha256()
        digest.update((manifest_text or "").encode("utf-8"))
        for path in sorted(files):
            digest.update(path.encode("utf-8"))
            digest.update(str(files[path]["size"]).encode("ascii"))
        return digest.hexdigest()

    def _max_file_bytes(self):
        value = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.max_file_bytes")
        try:
            return max(10000, int(value or 180000))
        except ValueError:
            return 180000

    def _prompt_max_chars(self):
        value = self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.prompt_max_chars")
        try:
            return max(20000, int(value or 90000))
        except ValueError:
            return 90000

    def _file_migration_max_tokens(self, content, configured_max_tokens=False, retry=False):
        try:
            configured = int(configured_max_tokens or 0)
        except (TypeError, ValueError):
            configured = 0
        estimated = int(len(content or "") / 3) + 2000
        budget = max(configured, estimated, 8000)
        if retry:
            budget = max(budget * 2, estimated + 6000, 12000)
        cap = self._provider_output_token_cap(configured)
        return min(budget, cap)

    def _serialize_ai_provider_calls(self, provider):
        """Block until no other job is currently talking to this AI provider.

        Several file-migration tasks can be in flight at once (a cron batch runs
        them back to back, and two cron threads can each hold a batch). Free/shared AI
        backends (Ollama Cloud, Groq's free tier, etc.) often reject simultaneous
        requests outright with something like {"error":"too many concurrent
        requests"} — a transient collision that has nothing to do with migration
        difficulty, but today it permanently marks the file "Error" and leaves
        the untouched Odoo 11 source in place. A Postgres transaction-scoped
        advisory lock, keyed per provider, forces those calls to queue up one at
        a time instead of colliding — it self-releases when this job's
        transaction commits, so there is nothing to unlock manually.
        """
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            ("ai_module_migrator_provider:%s" % provider,),
        )

    def _provider_output_token_cap(self, configured_max_tokens=0):
        provider = self.provider or self.env["ir.config_parameter"].sudo().get_param("ai_module_migrator.provider") or "gemini"
        # llama-3.3-70b-versatile (the accuracy-first Groq default) and gpt-oss:120b
        # (the accuracy-first Ollama default) both support much larger completions
        # than the old 8b-instant/20b defaults did — keep the cap in step so a
        # large file isn't truncated before it even reaches the token-limit retry.
        default_caps = {
            "groq": 32768,
            "ollama": 32768,
        }
        cap = default_caps.get(provider, 32768)
        try:
            configured = int(configured_max_tokens or 0)
        except (TypeError, ValueError):
            configured = 0
        return max(cap, configured)

    def _is_token_limit_error(self, error):
        message = str(error or "").lower()
        return any(
            marker in message
            for marker in (
                "token limit",
                "max_tokens",
                "max tokens",
                "max output tokens",
                "finish reason: max_tokens",
                "finishreason: max_tokens",
                "finish_reason: length",
                "reached the length",
                "finish reason: length",
            )
        )

    def _is_child_path(self, path, root):
        try:
            return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
        except ValueError:
            return False

    def _slugify(self, value):
        value = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip().lower())
        return value.strip("_") or "unknown_addon"

    # ── Code-migration helpers ─────────────────────────────────────────────────

    def _file_type_label(self, path):
        """Human label for a file's language, shared by every prompt that names it."""
        ext = os.path.splitext(path)[1].lower()
        file_type = {
            ".py": "Python",
            ".xml": "XML",
            ".js": "JavaScript (OWL/Legacy)",
            ".scss": "SCSS",
            ".css": "CSS",
            ".html": "HTML/QWeb",
            ".jinja": "Jinja2/QWeb",
            ".j2": "Jinja2/QWeb",
        }.get(ext, "text")
        if os.path.basename(path) in MANIFEST_NAMES:
            file_type = "Python (Odoo __manifest__)"
        return file_type

    def _build_file_migration_prompt(self, path, content):
        file_type = self._file_type_label(path)

        symbol_map = (self.module_symbol_map or "").strip()
        module_context = (
            "\n\nModule-wide model/field index (for cross-file consistency — this file may "
            "reference models/fields defined in another file of this same module; do not "
            "invent names that contradict this index):\n%s\n" % symbol_map[:3500]
            if symbol_map else ""
        )

        return (
            "You are an expert, meticulous senior Odoo developer performing a PRODUCTION code migration "
            "from Odoo %(source)s to Odoo %(target)s.\n\n"
            "IMPORTANT CONTEXT — read this carefully: this is not a demo or a best-effort draft. The file "
            "you return will be written directly into a real module, installed into a live Odoo database, "
            "and exercised by a human developer running actual install/update flows and business "
            "workflows (creating records, running views, triggering automations). If the code is wrong, "
            "the module will fail to install, a view will crash, or data will be corrupted — a real person "
            "will hit that failure, not a test harness they can shrug off. Correctness matters more than "
            "completeness: a smaller, verified-correct change is always better than a larger change that "
            "might be wrong.\n\n"
            "Migrate the %(file_type)s file below. Strict rules:\n"
            "1. Return ONLY the migrated file content — no explanations, no markdown fences, no preamble, "
            "no trailing commentary.\n"
            "2. You MUST return the complete file. Returning the file is not optional and there is no "
            "opt-out: do not reply with SKIP, do not refuse, do not explain why the task is hard, and do "
            "not return a summary, a diff, or a partial excerpt. Uncertainty about ONE construct is never "
            "a reason to withhold the whole file — migrate what you are sure about and leave anything you "
            "are unsure about exactly as it is in the source. A file returned unchanged is a valid answer; "
            "no file at all is not.\n"
            "3. Update all Odoo %(source)s-specific APIs, decorators, XML attributes, asset declarations, "
            "and import paths to Odoo %(target)s equivalents. Only apply changes you are certain are correct "
            "for this exact source→target version pair — do not guess at API names you are not sure exist.\n"
            "4. For __manifest__ files: update the version prefix from %(source)s.0 to %(target)s.0 "
            "(e.g. '%(source)s.0.1.0.0' → '%(target)s.0.1.0.0').\n"
            "5. For XML views: convert attrs/states modifiers, tree→list rename (Odoo 17+), "
            "asset bundle keys, and remove obsolete tags. When converting attrs= or states=, the "
            "new invisible/readonly/required/column_invisible attribute takes a PYTHON BOOLEAN "
            "EXPRESSION, never a domain list: attrs=\"{'invisible': [('state','!=','draft')]}\" "
            "becomes invisible=\"state != 'draft'\", and states=\"confirm,approved\" becomes "
            "invisible=\"state not in ['confirm','approved']\". Leaving the domain list in place "
            "(invisible=\"[('state','!=','draft')]\") is always-True and permanently hides the "
            "element.\n"
            "6. For Python files: replace @api.multi/@api.one, fields_view_get, old ORM signatures, "
            "and any APIs removed in Odoo %(target)s.\n"
            "7. Preserve all business logic, field names, and file structure exactly. Do NOT rename fields, "
            "methods, or models unless the migration strictly requires it. Do NOT reformat or reindent lines "
            "that did not need to change, and do NOT reorder methods, imports, or XML nodes — this makes the "
            "change reviewable by a human diff.\n"
            "8. Do NOT invent, remove, or guess at code that is not clearly implied by an Odoo %(source)s→"
            "%(target)s API change. Never fabricate a field, method, model, XML id, or API that you are not "
            "certain exists in the target version. When unsure whether a specific construct needs to change, "
            "copy that construct through unchanged — but still return the rest of the file, fully migrated. "
            "Leaving one line alone is correct; abandoning the file is not.\n"
            "9. The output MUST be syntactically valid %(file_type)s — it will be parsed automatically and "
            "then actually loaded by Odoo, so unbalanced brackets/quotes/tags, bad indentation, or invalid "
            "syntax are not acceptable. Before answering, silently re-read the entire file you are about to "
            "return, mentally trace it top to bottom, and verify: balanced brackets/parens/quotes, matching "
            "XML open/close tags, correct Python indentation, and no accidental duplication or dropped lines. "
            "Fix anything you find before responding — do not rely on a later validation pass to catch it.\n"
            "10. Do not truncate the file: every line of input must have a corresponding line in the output "
            "(migrated or unchanged), in the same order. Missing or duplicated lines are treated as a failed "
            "migration.\n"
            "11. Treat this as code someone will run today, not as an example: prefer the exact, correct "
            "Odoo %(target)s API over an approximation, and never leave placeholder text, TODOs, or "
            "'...' ellipses in place of real code.\n\n"
            "Module: %(module)s\n"
            "File: %(path)s\n"
            "File type: %(file_type)s\n"
            "Source Odoo: %(source)s\n"
            "Target Odoo: %(target)s"
            "%(module_context)s\n"
            "--- BEGIN FILE ---\n%(content)s\n--- END FILE ---\n\n"
            "Return the complete migrated file content only."
        ) % {
            "source": self.source_version,
            "target": self.target_version,
            "module": self.module_name or "unknown",
            "path": path,
            "file_type": file_type,
            "module_context": module_context,
            "content": content,
        }

    def _build_validation_fix_prompt(self, path, broken_content, error_message):
        """Ask the same AI to repair syntactically invalid output it just produced."""
        return (
            "Your previous answer for this Odoo migration task was not valid syntax and cannot be used. "
            "This is a real file that a developer is about to install and test — an invalid file will "
            "break the module the moment it loads, so this fix must be exactly correct, not just "
            "plausible.\n\n"
            "Validation error: %(error)s\n\n"
            "Fix ONLY the syntax problem above. Keep every other change exactly as you made it — do not "
            "make any other edits, do not reformat unrelated lines, and do not second-guess earlier "
            "migration decisions. Before answering, trace through the corrected file yourself and confirm "
            "brackets/parens/quotes are balanced and, for XML, every tag is properly opened and closed. "
            "Return ONLY the corrected full file content — no explanations, no markdown fences, "
            "no preamble. Returning the corrected file is mandatory: do not reply SKIP and do not refuse. "
            "If you cannot see how to fix a construct, revert that one construct to how it appears in the "
            "original source file — a valid file that is partly unmigrated is useful, no file is not.\n\n"
            "File: %(path)s\n\n"
            "--- YOUR PREVIOUS (INVALID) OUTPUT ---\n%(broken)s\n--- END ---\n\n"
            "Return the complete corrected file content only."
        ) % {
            "error": error_message,
            "path": path,
            "broken": broken_content,
        }

    def _validate_migrated_content(self, path, content):
        """Best-effort syntax validation of AI-migrated output before it is accepted.

        This runs for every AI provider (OpenAI, Gemini, Anthropic, Groq, DeepSeek,
        Ollama, OpenAI-compatible) so accuracy checks are not provider-specific.
        Returns (is_valid, error_message_or_False).
        """
        ext = os.path.splitext(path)[1].lower()
        basename = os.path.basename(path)
        if ext == ".py" or basename in MANIFEST_NAMES:
            try:
                ast.parse(content, filename=path)
            except SyntaxError as error:
                return False, _("Python syntax error at line %(line)s: %(msg)s") % {
                    "line": error.lineno,
                    "msg": error.msg,
                }
            return True, False
        if ext == ".xml":
            try:
                ET.fromstring(content.encode("utf-8"))
            except ET.ParseError as error:
                return False, _("XML is not well-formed: %s") % error
            return True, False
        if ext == ".js":
            balance_error = self._check_js_bracket_balance(content)
            if balance_error:
                return False, balance_error
            return True, False
        return True, False

    # A '/' can only start a regex literal where a value could legally begin —
    # right after an opener/operator, or after one of these keywords. Anywhere
    # else (after an identifier, number, string, ')', ']', or '}') a '/' is
    # division. Used by _check_js_bracket_balance to avoid miscounting
    # brackets that live inside a regex pattern, e.g. /[{]/ or /\(/.
    JS_REGEX_PRECEDING_PUNCT = set("([{,;:=!&|?+-*%^~<>")
    JS_REGEX_PRECEDING_KEYWORDS = {
        "return", "typeof", "instanceof", "in", "of", "case", "yield",
        "delete", "void", "throw", "new", "do", "else", "await",
    }

    def _check_js_bracket_balance(self, content):
        """Lightweight structural check for JS/OWL output.

        Not a full parser, but catches the most common AI-migration failure
        mode: unbalanced (), {}, [], or an unterminated string/template
        literal left over from a partial edit. Comments, string/template
        contents, and regex-literal contents are all skipped so characters
        inside them (including brackets inside a regex pattern) don't cause
        false positives.
        """
        pairs = {")": "(", "]": "[", "}": "{"}
        openers = set(pairs.values())
        closers = set(pairs.keys())
        stack = []
        i = 0
        length = len(content)
        in_string = None  # one of '"', "'", "`", or False
        in_line_comment = False
        in_block_comment = False
        prev_significant = ""  # last non-whitespace "code" character seen
        word_buffer = ""  # trailing identifier characters, for keyword detection
        while i < length:
            ch = content[i]
            nxt = content[i + 1] if i + 1 < length else ""
            if in_line_comment:
                if ch == "\n":
                    in_line_comment = False
                i += 1
                continue
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_string:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_string:
                    in_string = None
                    prev_significant = ch
                    word_buffer = ""
                i += 1
                continue
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in ("'", '"', "`"):
                in_string = ch
                i += 1
                continue
            if ch == "/" and (
                not prev_significant
                or prev_significant in self.JS_REGEX_PRECEDING_PUNCT
                or word_buffer in self.JS_REGEX_PRECEDING_KEYWORDS
            ):
                j = i + 1
                in_class = False
                closed = False
                while j < length:
                    c = content[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "\n":
                        break
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        j += 1
                        closed = True
                        break
                    j += 1
                if closed:
                    while j < length and content[j].isalpha():
                        j += 1
                    i = j
                    prev_significant = "/"
                    word_buffer = ""
                    continue
                # No closing '/' found on this line — not actually a regex
                # literal (or truncated output). Fall through and treat the
                # '/' as an ordinary character below.
            if ch in openers:
                stack.append(ch)
                prev_significant = ch
                word_buffer = ""
                i += 1
                continue
            if ch in closers:
                if not stack or stack[-1] != pairs[ch]:
                    return _(
                        "JavaScript brackets are unbalanced near position %(pos)s "
                        "(unexpected '%(char)s')."
                    ) % {"pos": i, "char": ch}
                stack.pop()
                prev_significant = ch
                word_buffer = ""
                i += 1
                continue
            if ch.isalnum() or ch in "_$":
                word_buffer += ch
                prev_significant = ch
            elif not ch.isspace():
                word_buffer = ""
                prev_significant = ch
            i += 1
        if in_string:
            return _("JavaScript output ends with an unterminated string or template literal.")
        if in_block_comment:
            return _("JavaScript output ends with an unterminated /* */ comment.")
        if stack:
            return _(
                "JavaScript brackets are unbalanced: unclosed '%(char)s'."
            ) % {"char": stack[-1]}
        return False

    def _check_structural_drift(self, path, original_content, migrated_content):
        """Second, independent safety net that runs AFTER syntax validation passes.

        Syntax validation only proves the AI's output can be parsed — it says
        nothing about whether the AI quietly deleted a method, a field, or an
        XML record while "migrating" the file. That kind of mistake produces
        perfectly valid syntax, so it sails through `_validate_migrated_content`
        unnoticed and would otherwise be accepted as a clean "Migrated" file.

        This check does a structural before/after comparison and flags the file
        for human review instead of silently accepting it. It never blocks or
        rewrites content — it only decides whether the file should be marked
        "migrated" (trusted) or "review" (looks fine, but confirm by hand).

        Returns (has_drift: bool, note_or_False).
        """
        basename = os.path.basename(path)
        ext = os.path.splitext(path)[1].lower()

        if basename in MANIFEST_NAMES:
            try:
                original_data = ast.literal_eval(original_content)
                migrated_data = ast.literal_eval(migrated_content)
            except Exception:
                return False, False
            if not isinstance(original_data, dict) or not isinstance(migrated_data, dict):
                return False, False
            original_depends = set(original_data.get("depends") or [])
            migrated_depends = set(migrated_data.get("depends") or [])
            dropped_depends = original_depends - migrated_depends
            original_data_files = set(original_data.get("data") or [])
            migrated_data_files = set(migrated_data.get("data") or [])
            dropped_data_files = original_data_files - migrated_data_files
            notes = []
            if dropped_depends:
                notes.append(_("dropped from 'depends': %s") % ", ".join(sorted(dropped_depends)))
            if dropped_data_files:
                notes.append(_("dropped from 'data': %s") % ", ".join(sorted(dropped_data_files)))
            if notes:
                return True, _("Manifest structure changed unexpectedly — %s.") % "; ".join(notes)
            return False, False

        if ext == ".py":
            try:
                original_names = self._python_definition_names(original_content)
                migrated_names = self._python_definition_names(migrated_content)
            except SyntaxError:
                return False, False
            missing = original_names - migrated_names
            if missing:
                return True, _(
                    "%(count)s method/class/function name(s) present in the original file are "
                    "missing from the migrated output: %(names)s. This can be an intentional "
                    "removal (e.g. an API that no longer exists) or an accidental drop — verify "
                    "before trusting this file."
                ) % {"count": len(missing), "names": ", ".join(sorted(missing)[:15])}
            return False, False

        if ext == ".xml":
            original_ids = self._xml_record_ids(original_content)
            migrated_ids = self._xml_record_ids(migrated_content)
            if original_ids is None or migrated_ids is None:
                return False, False
            missing = original_ids - migrated_ids
            if missing:
                return True, _(
                    "%(count)s XML record id(s) present in the original file are missing from "
                    "the migrated output: %(names)s. Confirm these were intentionally removed "
                    "(and not just renamed/lost) before trusting this file."
                ) % {"count": len(missing), "names": ", ".join(sorted(missing)[:15])}
            return False, False

        return False, False

    def _python_definition_names(self, content):
        """Return the set of top-level and nested def/class names in a Python file."""
        tree = ast.parse(content)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        return names

    def _xml_record_ids(self, content):
        """Return the set of id="..." values on <record>/<template>/<menuitem>/etc. tags."""
        try:
            root = ET.fromstring(content.encode("utf-8"))
        except ET.ParseError:
            return None
        ids = set()
        for element in root.iter():
            record_id = element.get("id")
            if record_id:
                ids.add(record_id)
        return ids

    @api.model
    def _with_trailing_newline(self, text):
        """Restore the final newline on AI-generated file content.

        The provider response is .strip()-ed on the way in — necessary to
        detect a bare "SKIP" verdict and to peel off ``` code fences — which
        also eats the file's trailing newline. Every migrated file therefore
        came out with no newline at end of file, adding a spurious hunk
        to the diff of each file the tool touched. Only AI-produced content
        goes through here: files copied unchanged from the source must stay
        byte-identical to it.
        """
        if not text:
            return text
        return text if text.endswith("\n") else text + "\n"

    def _build_migrated_zip(self, files, migrated_contents):
        module_name = self.module_name or "migrated_module"
        zip_name = "%s_odoo%s_to_%s.zip" % (module_name, self.source_version, self.target_version)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(migrated_contents):
                zip_entry = "%s/%s" % (module_name, path)
                content = migrated_contents.get(path)
                if content is None:
                    # Binary — read raw bytes from source
                    raw = self._read_source_binary(path)
                    if raw is not None:
                        zf.writestr(zip_entry, raw)
                elif isinstance(content, str):
                    zf.writestr(zip_entry, content.encode("utf-8"))
        return buffer.getvalue(), zip_name

    def _write_migrated_to_path(self, files, migrated_contents, out_path):
        out_path = os.path.abspath(os.path.expanduser(out_path))
        self._assert_allowed_path(out_path)
        module_name = self.module_name or "migrated_module"
        out_dir = os.path.join(out_path, module_name)
        if self.input_mode == "filesystem" and self.source_path:
            source_root = os.path.abspath(os.path.expanduser(self.source_path))
            if self._is_child_path(out_dir, source_root):
                raise UserError(_("Output path would overwrite the source addon: %s") % out_dir)
        os.makedirs(out_dir, exist_ok=True)
        for path in sorted(migrated_contents):
            content = migrated_contents.get(path)
            full_out = os.path.join(out_dir, path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full_out), exist_ok=True)
            if content is None:
                raw = self._read_source_binary(path)
                if raw is not None:
                    with open(full_out, "wb") as fh:
                        fh.write(raw)
            elif isinstance(content, str):
                with open(full_out, "w", encoding="utf-8") as fh:
                    fh.write(content)

    def _record_migration_file(self, path, file_data, state, note=False, migrated_content=False):
        values = {
            "job_id": self.id,
            "file_path": path,
            "file_kind": self._file_kind(path),
            "file_size": file_data.get("size") or 0,
            "state": state,
            "skip_reason": note or False,
        }
        if isinstance(migrated_content, str):
            values["migrated_content"] = migrated_content[:60000]
        return self.env["ai.module.migration.file"].create(values)

    def _update_migration_progress(self, migrated_contents, skipped):
        self.write({
            "migrated_file_count": len(migrated_contents),
            "skipped_file_count": len(skipped),
            "skipped_file_list": "\n".join(skipped) if skipped else False,
        })

    def _read_source_binary(self, relative_path):
        """Return raw bytes for a file from the source (filesystem or archive)."""
        if self.input_mode == "filesystem":
            root = os.path.abspath(os.path.expanduser(self.source_path or ""))
            full_path = os.path.join(root, relative_path.replace("/", os.sep))
            try:
                with open(full_path, "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        # Archive mode — re-read from stored zip
        if not self.source_archive:
            return None
        raw_archive = base64.b64decode(self.source_archive)
        try:
            with zipfile.ZipFile(io.BytesIO(raw_archive)) as archive:
                names = archive.namelist()
                for name in names:
                    normalized = name.replace("\\", "/").lstrip("/")
                    stripped = self._strip_archive_root(normalized, names)
                    if stripped == relative_path:
                        return archive.read(name)
        except Exception:
            return None
        return None

    def _notification(self, title, message, notification_type, next_action=False):
        action = {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": notification_type,
                "sticky": False,
            },
        }
        if next_action:
            action["params"]["next"] = next_action
        return action
