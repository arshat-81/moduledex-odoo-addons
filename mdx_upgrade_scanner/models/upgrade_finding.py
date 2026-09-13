"""One thing found, in one place.

A finding is only useful if someone can act on it without hunting, so it carries
the module, the path relative to that module, the line, and the line itself.
"""

from odoo import api, fields, models


class MdxUpgradeFinding(models.Model):
    _name = "mdx.upgrade.finding"
    _description = "Upgrade Finding"
    _order = "severity, module_name, file_path, line"
    _rec_name = "display_title"

    scan_id = fields.Many2one("mdx.upgrade.scan", required=True, ondelete="cascade",
                              index=True)
    rule_id = fields.Many2one("mdx.upgrade.rule", ondelete="set null", index=True)
    rule_title = fields.Char(
        help="Kept as text so a finding still reads correctly if the rule is "
             "later edited or deleted.")
    severity = fields.Selection(
        [("removed", "Already removed"), ("due", "Due for removal"),
         ("deprecated", "Deprecated"), ("advisory", "Advisory")],
        required=True, index=True)
    module_name = fields.Char(string="Module", index=True)
    file_path = fields.Char(string="File")
    # aggregator=False because a line number is a location, not a quantity:
    # grouped by module, the default sum reports "7,205" and means nothing.
    line = fields.Integer(aggregator=False)
    snippet = fields.Char(string="Code", help="The line as it appears in the file.")
    replacement = fields.Char(string="Use Instead")
    since_version = fields.Char(string="Deprecated In")
    source_ref = fields.Char(string="Declared In Odoo At")
    display_title = fields.Char(compute="_compute_display_title")

    @api.depends("rule_title", "file_path", "line")
    def _compute_display_title(self):
        for finding in self:
            where = "%s:%s" % (finding.file_path or "?", finding.line or 0)
            finding.display_title = "%s - %s" % (finding.rule_title or "", where)
