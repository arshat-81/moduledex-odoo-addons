from odoo import fields, models


class AiModuleMigrationFile(models.Model):
    _name = "ai.module.migration.file"
    _description = "AI Module Migration File"
    _order = "state desc, file_path asc"

    job_id = fields.Many2one(
        "ai.module.migration.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    file_path = fields.Char(string="File", required=True, readonly=True)
    file_kind = fields.Char(string="Kind", readonly=True)
    file_size = fields.Integer(string="Bytes", readonly=True)
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("migrated", "Migrated"),
            ("review", "Needs Review"),
            ("copied", "Copied"),
            ("skipped", "Skipped"),
            ("error", "Error"),
        ],
        required=True,
        readonly=True,
    )
    skip_reason = fields.Text(string="Note", readonly=True)
    migrated_content = fields.Text(readonly=True, string="Migrated Content")
