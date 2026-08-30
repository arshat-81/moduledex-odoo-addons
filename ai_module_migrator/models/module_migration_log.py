from odoo import fields, models


class AiModuleMigrationLog(models.Model):
    _name = "ai.module.migration.log"
    _description = "AI Module Migration Log"
    _order = "id desc"

    job_id = fields.Many2one(
        "ai.module.migration.job",
        required=True,
        ondelete="cascade",
        index=True,
    )
    migration_file_id = fields.Many2one(
        "ai.module.migration.file",
        ondelete="set null",
        index=True,
    )
    file_path = fields.Char(index=True)
    level = fields.Selection(
        [
            ("info", "Info"),
            ("success", "Success"),
            ("warning", "Warning"),
            ("error", "Error"),
        ],
        default="info",
        required=True,
        index=True,
    )
    event = fields.Char(required=True, index=True)
    provider = fields.Char(index=True)
    model = fields.Char()
    max_tokens = fields.Integer(string="Max Output Tokens")
    duration = fields.Float(string="Duration (s)", digits=(16, 2))
    message = fields.Text(required=True)
    prompt_preview = fields.Text()
    response_preview = fields.Text()
