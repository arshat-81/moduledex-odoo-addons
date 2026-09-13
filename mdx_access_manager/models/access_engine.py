"""Resolution engine shared by every feature in this module.

Everything here resolves access from a **set of group ids**, never from a user
record. That one decision is what makes comparison and dry-run previews
possible: a real user is just ``user.group_ids.all_implied_ids``, and a
hypothetical user is any group set you care to hand in. No savepoint, no
rollback and no write is needed to answer "what would happen if".

The SQL below deliberately mirrors ``ir.model.access._get_allowed_models`` and
``ir.rule._get_rules`` in Odoo 19 rather than reimplementing their intent. If
this module and the server ever disagree, the module is wrong and this is the
file to fix, so the rules it encodes are written out explicitly:

* A user may perform ``mode`` on a model if and only if there is at least one
  ``ir.model.access`` row that is ``active``, has ``perm_<mode>`` set, and whose
  ``group_id`` is either NULL (a global grant to everyone) or one of the user's
  groups. A model with no access control rows at all is denied, not allowed.
* Record rules are selected when ``active``, ``perm_<mode>`` is set, and the
  rule is global or carries one of the user's groups. Group rule domains are
  OR-ed together; that result is AND-ed with every global rule domain. So a
  single global rule can nullify any number of permissive group rules.
* ``res.groups.all_implied_ids`` is the reflexive transitive closure of
  ``implied_ids`` — the group itself plus everything it implies. Odoo 19
  resolves implication on read (``res.users.all_group_ids`` is computed as
  ``group_ids.all_implied_ids``) rather than materialising it onto the user, so
  the closure has to be walked here too.
"""

from odoo import api, models
from odoo.tools import SQL

MODES = ("read", "write", "create", "unlink")
MODE_LABELS = {
    "read": "Read",
    "write": "Write",
    "create": "Create",
    "unlink": "Delete",
}


class MdxAccessEngine(models.AbstractModel):
    _name = "mdx.access.engine"
    _description = "Access Rights Resolution Engine"

    # ------------------------------------------------------------------
    # group sets
    # ------------------------------------------------------------------
    @api.model
    def _expand_groups(self, groups):
        """Reflexive transitive closure of ``groups``.

        Mirrors what the server does for a user: ``all_group_ids`` is
        ``group_ids.all_implied_ids``. Accepts a recordset or an iterable of
        ids and always returns a ``res.groups`` recordset including the
        originals themselves.
        """
        Groups = self.env["res.groups"].sudo()
        if not isinstance(groups, models.BaseModel):
            groups = Groups.browse([gid for gid in (groups or []) if gid])
        if not groups:
            return Groups.browse()
        return groups.all_implied_ids

    @api.model
    def _user_group_sets(self, user):
        """Return ``(explicit, effective)`` group recordsets for a user."""
        user = user.sudo()
        explicit = user.group_ids
        return explicit, self._expand_groups(explicit)

    # ------------------------------------------------------------------
    # effective permissions
    # ------------------------------------------------------------------
    @api.model
    def _perms_by_model(self, group_ids, model_names=None):
        """Effective CRUD per model for a group set.

        Returns ``{model_name: {"read": bool, "write": bool, ...}}`` containing
        only models where at least one operation is granted. A model absent from
        the result is denied outright, which is the server's behaviour for a
        model with no access control rows.

        ``group_ids`` is used verbatim: expand it with ``_expand_groups`` first
        if it came from a user form, or implied groups will be missed.
        """
        group_ids = tuple(gid for gid in (group_ids or []) if gid)
        self.env["ir.model.access"].flush_model()
        where = [SQL("a.active")]
        # A NULL group_id is a grant to every user, so it must survive even when
        # the group set is empty (a brand-new user with no groups at all).
        if group_ids:
            where.append(SQL("(a.group_id IS NULL OR a.group_id IN %s)", group_ids))
        else:
            where.append(SQL("a.group_id IS NULL"))
        if model_names:
            where.append(SQL("m.model IN %s", tuple(model_names)))

        rows = self.env.execute_query(SQL(
            """
            SELECT m.model,
                   bool_or(a.perm_read)   AS perm_read,
                   bool_or(a.perm_write)  AS perm_write,
                   bool_or(a.perm_create) AS perm_create,
                   bool_or(a.perm_unlink) AS perm_unlink
              FROM ir_model_access a
              JOIN ir_model m ON (m.id = a.model_id)
             WHERE %s
          GROUP BY m.model
            """,
            SQL(" AND ").join(where),
        ))
        result = {}
        for model_name, read, write, create, unlink in rows:
            perms = {"read": bool(read), "write": bool(write),
                     "create": bool(create), "unlink": bool(unlink)}
            if any(perms.values()):
                result[model_name] = perms
        return result

    @api.model
    def _perms_for_user(self, user, model_names=None):
        _explicit, effective = self._user_group_sets(user)
        return self._perms_by_model(effective.ids, model_names=model_names)

    # ------------------------------------------------------------------
    # provenance
    # ------------------------------------------------------------------
    @api.model
    def _granting_acls(self, model_name, group_ids):
        """Access control rows that grant something on ``model_name``.

        Returns a list of dicts, each carrying the ACL, the group that carries
        it (empty for a global grant) and which operations it contributes.
        """
        group_ids = tuple(gid for gid in (group_ids or []) if gid)
        domain = [("active", "=", True), ("model_id.model", "=", model_name)]
        if group_ids:
            domain.append(("group_id", "in", [False] + list(group_ids)))
        else:
            domain.append(("group_id", "=", False))
        acls = self.env["ir.model.access"].sudo().search(domain)
        out = []
        for acl in acls:
            modes = [m for m in MODES if acl["perm_%s" % m]]
            if not modes:
                continue
            out.append({"acl": acl, "group": acl.group_id, "modes": modes})
        return out

    @api.model
    def _implication_paths(self, explicit_groups, target_group):
        """How ``target_group`` is reached from the explicitly assigned groups.

        Returns a list of label strings. A direct assignment yields the group's
        own name; an inherited one yields ``"Assigned Group -> Target"``, which
        is the answer to "I never gave them this, why do they have it".
        """
        if not target_group:
            return []
        paths = []
        for explicit in explicit_groups:
            if explicit == target_group:
                paths.append(self._group_label(target_group))
            elif target_group in explicit.all_implied_ids:
                paths.append("%s → %s" % (self._group_label(explicit),
                                               self._group_label(target_group)))
        return paths

    @api.model
    def _group_label(self, group):
        """Privilege-qualified group name.

        Odoo 19 orders ``res.groups`` by ``privilege_id`` and the native
        "groups with access" helper renders groups as ``privilege/group``, so a
        bare group name is ambiguous in exactly the places this module reports.
        """
        if not group:
            return ""
        privilege = group.privilege_id.name or (group.privilege_id.category_id.name or "")
        return "%s / %s" % (privilege, group.name) if privilege else group.name


    # ------------------------------------------------------------------
    # foreign enforcement layers
    # ------------------------------------------------------------------
    # Modules that extend ir.model.access.check or ir.rule._compute_domain
    # enforce restrictions this engine cannot see: the native tables still say
    # "allowed" while the server refuses. Detected structurally, from the model
    # classes actually in the registry (each carries ``_module``, set by the
    # loader - orm/models.py:239), so a module we have never heard of is found
    # just as reliably as a known one.
    OWN_MODULES = {"base", "mdx_access_manager"}

    # method -> the model whose enforcement it performs
    ENFORCEMENT_METHODS = {
        "ir.model.access": ("check",),
        "ir.rule": ("_compute_domain", "_get_rules"),
    }

    @api.model
    def _foreign_enforcement(self):
        """Module names that actually override the access checks.

        Testing for "inherits ir.model.access" is useless: ten core addons
        (mail, web, hr, sms, ...) extend those models to add fields and were all
        reported as enforcement layers. What matters is whether a module
        *redefines the decision*, so this looks for the enforcement methods in a
        class's own ``__dict__`` - inherited attributes do not count.
        """
        found = set()
        for model_name, methods in self.ENFORCEMENT_METHODS.items():
            model = self.env.get(model_name)
            if model is None:
                continue
            for klass in type(model).__mro__:
                module = getattr(klass, "_module", None)
                if not module or module in self.OWN_MODULES:
                    continue
                if any(m in vars(klass) for m in methods):
                    found.add(module)
        return sorted(found)

    @api.model
    def _foreign_enforcement_note(self):
        modules = self._foreign_enforcement()
        if not modules:
            return ""
        return self.env._(
            "Heads up: %(mods)s also extend Odoo's access checks. This matrix resolves the native "
            "access controls and record rules only, so the server may refuse something shown as "
            "allowed here.",
            mods=", ".join(modules),
        )

    # ------------------------------------------------------------------
    # record rules
    # ------------------------------------------------------------------
    @api.model
    def _rules_for(self, model_name, group_ids, mode="read"):
        """Rules the server would apply, split into global and group rules.

        Selection matches ``ir.rule._get_rules``: active, ``perm_<mode>`` set,
        and either global or carrying one of the given groups.
        """
        group_ids = set(gid for gid in (group_ids or []) if gid)
        rules = self.env["ir.rule"].sudo().search([
            ("active", "=", True),
            ("model_id.model", "=", model_name),
            ("perm_%s" % mode, "=", True),
        ])
        global_rules = self.env["ir.rule"].sudo().browse()
        group_rules = self.env["ir.rule"].sudo().browse()
        for rule in rules:
            if not rule.groups:
                global_rules |= rule
            elif set(rule.groups.ids) & group_ids:
                group_rules |= rule
        return global_rules, group_rules
