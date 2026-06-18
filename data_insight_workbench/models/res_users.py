from odoo import fields, models


class ResUsers(models.Model):
    _inherit = "res.users"

    data_insight_full_access = fields.Boolean(
        string="Data Insight Full SQL Access",
        compute="_compute_data_insight_full_access",
        inverse="_inverse_data_insight_full_access",
        groups="base.group_system",
        help="Allows this user to run unrestricted SQL in Data Insight Workbench.",
    )

    def _get_data_insight_full_access_group(self):
        return self.env.ref("data_insight_workbench.group_data_insight_full_access")

    def _compute_data_insight_full_access(self):
        for user in self:
            user.data_insight_full_access = user.has_group("data_insight_workbench.group_data_insight_full_access")

    def _inverse_data_insight_full_access(self):
        group = self._get_data_insight_full_access_group()
        for user in self:
            if user.data_insight_full_access:
                user.group_ids = [(4, group.id)]
            else:
                user.group_ids = [(3, group.id)]
