"""Preview: what a profile would take away from one user.

Works on the rule definitions, not on the generated restrictions, so it gives
the same answer whether the profile is a draft or already enforced. Record
counts are computed by asking the server as that user, with the rule's domain
added - the same arithmetic the restriction performs once it is live.
"""

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.tools.safe_eval import safe_eval

MENU_SAMPLE = 15


class MdxAccessPreview(models.TransientModel):
    _name = "mdx.access.preview"
    _description = "Access Profile Preview"

    profile_id = fields.Many2one("mdx.access.profile", required=True, ondelete="cascade")
    user_id = fields.Many2one(
        "res.users", string="Preview For",
        help="Any user. If the profile does not restrict them, the preview says so and why.")
    applies = fields.Boolean(compute="_compute_preview")
    report_html = fields.Html(compute="_compute_preview", sanitize=False)

    @api.depends("profile_id", "user_id")
    def _compute_preview(self):
        for wizard in self:
            applies, html = wizard._build()
            wizard.applies = applies
            wizard.report_html = html

    # ------------------------------------------------------------------
    def _build(self):
        self.ensure_one()
        profile = self.profile_id.sudo()
        user = self.user_id.sudo()
        _ = self.env._
        if not profile:
            return False, Markup("")
        if not user:
            return False, self._note(_("Choose a user to preview the profile for."))

        out = []
        applies, reason = self._applies(profile, user)
        if applies:
            state = (_("This profile is enforced: the restrictions below are live for %s.",
                       user.name) if profile.enforced else
                     _("This profile is a draft: nothing below applies to %s until you enforce it.",
                       user.name))
            out.append(self._note(state, "info" if profile.enforced else "warning"))
        else:
            out.append(self._note(reason, "warning"))

        out.append(self._section(_("Menus"), self._menu_rows(profile)))
        out.append(self._section(_("Models and records"), self._model_rows(profile, user)))
        out.append(self._section(_("Fields"), self._field_rows(profile)))
        out.append(self._section(_("Buttons, tabs, reports and actions"),
                                 self._element_rows(profile)))
        return applies, Markup("").join(out)

    def _applies(self, profile, user):
        _ = self.env._
        if user._is_superuser():
            return False, _("%s is the superuser, which bypasses every access rule.", user.name)
        groups = set(user._get_group_ids())
        direct = user in profile.user_ids
        through = profile.group_ids.filtered(lambda group: group.id in groups)
        if not (direct or through):
            return False, _(
                "%s is not restricted by this profile: they are not listed and hold none of its "
                "groups. The figures below show what would happen if they were.", user.name)
        if not profile.include_admins and profile._system_group_id() in groups:
            return False, _(
                "%s is an administrator, and this profile does not restrict administrators. "
                "The figures below show what would happen if it did.", user.name)
        return True, ""

    # ------------------------------------------------------------------
    def _menu_rows(self, profile):
        _ = self.env._
        if not profile.menu_ids:
            return []
        Menu = self.env["ir.ui.menu"].sudo().with_context(active_test=False)
        hidden = Menu.search([("id", "child_of", profile.menu_ids.ids)])
        rows = [(_("Hidden"), _("%(count)s menu(s), counting everything underneath",
                                count=len(hidden)))]
        for menu in profile.menu_ids[:MENU_SAMPLE]:
            rows.append(("", menu.complete_name))
        if len(profile.menu_ids) > MENU_SAMPLE:
            rows.append(("", _("... and %s more", len(profile.menu_ids) - MENU_SAMPLE)))
        return rows

    def _model_rows(self, profile, user):
        _ = self.env._
        rows = []
        context = self.env["ir.access"].with_user(user)._eval_context()
        for rule in profile.model_rule_ids:
            model_name = rule.model_id.model
            if model_name not in self.env:
                continue
            as_user = self.env[model_name].with_user(user).with_context(
                allowed_company_ids=user.company_ids.ids)
            label = "%s (%s)" % (rule.model_id.name, model_name)

            for flag, operation, verb in ((rule.no_create, "create", _("Create")),
                                          (rule.no_write, "write", _("Edit")),
                                          (rule.no_unlink, "unlink", _("Delete"))):
                if not flag:
                    continue
                had = as_user.has_access(operation) if not profile.enforced else None
                if had is False:
                    detail = _("blocked - they could not do this anyway")
                else:
                    detail = _("blocked")
                rows.append((label, "%s: %s" % (verb, detail)))

            for text, operation, verb in ((rule.read_domain, "read", _("Read")),
                                          (rule.write_domain, "write", _("Edit")),
                                          (rule.unlink_domain, "unlink", _("Delete"))):
                text = rule._clean(text)
                if not text:
                    continue
                if operation == "write" and rule.no_write:
                    continue
                if operation == "unlink" and rule.no_unlink:
                    continue
                rows.append((label, self._count_line(as_user, text, context, verb)))

            if rule.hide_chatter:
                rows.append((label, _("Chatter hidden")))
        return rows

    def _count_line(self, as_user, text, context, verb):
        """'Read: 12 of 340 records' - both counted as the user sees them."""
        _ = self.env._
        try:
            domain = Domain(safe_eval(text, context))
            if not as_user.has_access("read"):
                return _("%(verb)s: limited to %(domain)s (they cannot read this model, so no "
                         "count is available)", verb=verb, domain=text)
            total = as_user.search_count(Domain.TRUE)
            kept = as_user.search_count(domain)
        except Exception as error:  # noqa: BLE001 - a preview must not crash on a bad domain
            return _("%(verb)s: limited to %(domain)s (could not be counted: %(error)s)",
                     verb=verb, domain=text, error=error)
        return _("%(verb)s: %(kept)s of the %(total)s record(s) they can see now",
                 verb=verb, kept=kept, total=total)

    def _field_rows(self, profile):
        _ = self.env._
        rows = []
        for rule in profile.field_rule_ids:
            effects = [label for flag, label in (
                (rule.invisible, _("hidden")),
                (rule.readonly, _("read-only")),
                (rule.required, _("required")),
                (rule.strict, _("writes rejected on the server")),
            ) if flag]
            rows.append(("%s.%s" % (rule.model_id.model, rule.field_id.name),
                         "%s: %s" % (rule.field_id.field_description, ", ".join(effects))))
        return rows

    def _element_rows(self, profile):
        kinds = dict(profile.element_rule_ids._fields["kind"].selection)
        return [
            ("%s (%s)" % (rule.model_id.name, rule.model_id.model),
             "%s: %s" % (kinds.get(rule.kind, rule.kind), rule.label or rule.name))
            for rule in profile.element_rule_ids
        ]

    # ------------------------------------------------------------------
    def _section(self, title, rows):
        if not rows:
            return Markup("")
        body = Markup("").join(
            Markup("<tr><td class='text-muted' style='width:38%%'>%s</td><td>%s</td></tr>")
            % (escape(left), escape(right))
            for left, right in rows
        )
        return Markup("<h4 class='mt-3'>%s</h4><table class='table table-sm'>%s</table>") % (
            escape(title), body)

    def _note(self, text, level="info"):
        return Markup("<div class='alert alert-%s' role='alert'>%s</div>") % (level, escape(text))
