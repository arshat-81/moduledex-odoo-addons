"""Profile-wide policies: read-only access, no API, no export, no import.

Each one is enforced where the server makes the decision, not where the
interface offers the button:

* read-only - in ``_access_domain``, the method every access check resolves to;
* no API   - at authentication, where core tells an interactive login apart
  from a programmatic one;
* no export / import - in ``export_data`` and ``load``, which every export and
  import path ends in.
"""

from odoo import api, models
from odoo.exceptions import AccessDenied, AccessError
from odoo.fields import Domain

# What a read-only user must still be able to write for the web client to work
# and for their own session to behave: personal settings, saved filters and
# favourites. Nothing here is business data.
READ_ONLY_WRITABLE = frozenset({
    "res.users.settings",
    "res.users.settings.volumes",
    "ir.filters",
    "ir.default",
    "bus.presence",
    "mail.presence",
})


class Base(models.AbstractModel):
    _inherit = "base"

    @api.model
    def _access_domain(self, operation):
        domain = super()._access_domain(operation)
        if operation == "read" or self.env.su or domain.is_false():
            return domain
        Profile = self.env["mdx.access.profile"]
        if not Profile._rules_snapshot()["has_read_only"]:
            return domain
        # Wizards hold no business data, and almost every button opens one.
        if self._transient or self._name in READ_ONLY_WRITABLE:
            return domain
        if Profile._policy_profile(self.env.user, "read_only"):
            return Domain.FALSE
        return domain

    def export_data(self, fields_to_export):
        self._mdx_check_policy("block_export", self.env._("export"))
        return super().export_data(fields_to_export)

    @api.model
    def load(self, fields, data):
        self._mdx_check_policy("block_import", self.env._("import"))
        return super().load(fields, data)

    @api.model
    def _mdx_check_policy(self, policy, verb):
        if self.env.su:
            return
        profile_name = self.env["mdx.access.profile"]._policy_profile(self.env.user, policy)
        if profile_name:
            self.env["mdx.access.denial"]._log(
                self.env.user, self._name, policy, profile_name=profile_name)
            raise AccessError(self.env._(
                "You are not allowed to %(verb)s records. This is set by the access profile "
                "\"%(profile)s\"; ask your administrator if you need it changed.",
                verb=verb, profile=profile_name))


class ResUsers(models.Model):
    _inherit = "res.users"

    def _check_credentials(self, credential, env):
        # super() first: a wrong password must fail exactly as it always does,
        # and say nothing about whether the account is API-restricted.
        auth_info = super()._check_credentials(credential, env)
        if not (env or {}).get("interactive", True):
            self._mdx_refuse_api(self.env.user)
        return auth_info

    @api.model
    def _mdx_refuse_api(self, user):
        Profile = self.env["mdx.access.profile"].sudo()
        profile_name = Profile._policy_profile(user.sudo(), "block_api")
        if profile_name:
            self.env["mdx.access.denial"]._log(
                user, "res.users", "block_api", profile_name=profile_name)
            raise AccessDenied(self.env._("API access is disabled for this user."))


class ResUsersApikeys(models.Model):
    _inherit = "res.users.apikeys"

    def _check_credentials(self, *, scope, key):
        uid = super()._check_credentials(scope=scope, key=key)
        if uid and scope == "rpc":
            user = self.env["res.users"].sudo().browse(uid)
            if self.env["mdx.access.profile"].sudo()._policy_profile(user, "block_api"):
                self.env["mdx.access.denial"]._log(
                    user, "res.users.apikeys", "block_api",
                    profile_name=self.env["mdx.access.profile"].sudo()._policy_profile(
                        user, "block_api"))
                return None
        return uid


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        info = super().session_info()
        # The web client decides whether to show Export from this flag. The
        # server refuses the export either way; this only spares the user a
        # button that would end in an error.
        groups = info.get("groups")
        if groups and groups.get("base.group_allow_export") and not self.env.su:
            if self.env["mdx.access.profile"]._policy_profile(self.env.user, "block_export"):
                groups["base.group_allow_export"] = False
        return info
