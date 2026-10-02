"""Resolution engine shared by every feature in this module.

Everything here resolves access from a **set of group ids**, never from a user
record. That one decision is what makes comparison and dry-run previews
possible: a real user is just ``user.group_ids.all_implied_ids``, and a
hypothetical user is any group set you care to hand in. No savepoint, no
rollback and no write is needed to answer "what would happen if".

The rules below deliberately mirror ``Model._access_domain`` and
``Model.has_access`` in Odoo 20 rather than reimplementing their intent. If
this module and the server ever disagree, the module is wrong and this is the
file to fix, so the rules it encodes are written out explicitly:

* Odoo 20 keeps every access rule in one model, ``ir.access``. A row carries a
  model, an optional group, a subset of ``crud`` and an optional domain.
* A row **with** a group is a *permission*. The domains of every permission the
  user's groups hold for an operation are OR-ed together. With no permission at
  all the result is FALSE: a model nobody was granted is denied, not allowed.
* A row **without** a group is a *restriction* and applies to everyone. Every
  restriction for the operation is AND-ed onto that result, so a restriction
  can only ever narrow access - it never grants anything.
* A model that ``_inherits`` another is also bound by its parent's access,
  unless the model opts out with ``_check_inherits_access = False``.
* A domain may delegate with the ``access`` operator, e.g.
  ``('channel_id', 'access', 'read')``. On the model level this is FALSE when
  the user has no access to the related model at all.
* ``res.groups.all_implied_ids`` is the reflexive transitive closure of
  ``implied_ids`` - the group itself plus everything it implies. Implication is
  resolved on read (``res.users.all_group_ids`` is computed as
  ``group_ids.all_implied_ids``), so the closure has to be walked here too.

What "allowed" means here is what ``has_access`` means on an empty recordset:
the operation is possible on the model for *some* record. Which records is the
job of the domains, and those are reported as provenance, not evaluated.
"""

from odoo import api, models
from odoo.fields import Domain

from odoo.addons.base.models.ir_access import IN_SELECTION

MODES = ("read", "write", "create", "unlink")
MODE_LABELS = {
    "read": "Read",
    "write": "Write",
    "create": "Create",
    "unlink": "Delete",
}


def modes_of(operation):
    """The modes an ``ir.access.operation`` value such as ``'cru'`` covers."""
    return [mode for mode in MODES if operation in IN_SELECTION[mode]]


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
        model no permission reaches.

        ``group_ids`` is used verbatim: expand it with ``_expand_groups`` first
        if it came from a user form, or implied groups will be missed.
        """
        group_ids = frozenset(gid for gid in (group_ids or []) if gid)
        all_access = self.env["ir.access"].sudo()._get_all_access()
        if model_names is None:
            names = all_access.keys()
        else:
            names = [name for name in model_names if name in all_access]

        memo = {}
        result = {}
        for model_name in names:
            perms = {
                mode: self._model_allows(model_name, mode, group_ids, all_access, memo)
                for mode in MODES
            }
            if any(perms.values()):
                result[model_name] = perms
        return result

    @api.model
    def _model_allows(self, model_name, mode, group_ids, all_access, memo):
        """Whether ``group_ids`` may perform ``mode`` on some record of the model."""
        key = (model_name, mode)
        if key in memo:
            return memo[key]
        # Optimistic while resolving: an 'access' condition may point back at a
        # model already being resolved, and a condition that cannot be proven
        # false does not deny anything.
        memo[key] = True

        model = self.env.get(model_name)
        if model is None:
            memo[key] = False
            return False

        allowed = True
        if model._check_inherits_access:
            for parent_name in model._inherits:
                if not self._model_allows(parent_name, mode, group_ids, all_access, memo):
                    allowed = False
                    break

        if allowed:
            operations = IN_SELECTION[mode]
            granted = False
            for info in all_access.get(model_name, ()):
                if info.operation not in operations:
                    continue
                if info.group_id and info.group_id not in group_ids:
                    continue
                possible = self._domain_possible(
                    model, info.domain, group_ids, all_access, memo)
                if info.group_id:
                    granted = granted or possible
                elif not possible:
                    # a restriction nothing can satisfy denies the operation
                    allowed = False
                    break
            allowed = allowed and granted

        memo[key] = allowed
        return allowed

    @api.model
    def _domain_possible(self, model, domain, group_ids, all_access, memo):
        """False only when the domain can be proven to match nothing.

        Domains that reference the user or the companies are kept by the server
        as source text and evaluated per user, so they cannot be decided for a
        bare group set; they count as possible, which is what the server
        concludes for all but a degenerate evaluation.
        """
        if not isinstance(domain, Domain):
            return True

        def resolve(condition):
            if condition.operator != "access":
                return condition
            if condition.field_expr == "id":
                comodel_name = model._name
            else:
                field = model._fields.get(condition.field_expr)
                comodel_name = getattr(field, "comodel_name", None)
            if not comodel_name or condition.value not in MODES:
                return condition
            if self._model_allows(comodel_name, condition.value, group_ids, all_access, memo):
                return Domain.TRUE
            return Domain.FALSE

        return not domain.map_conditions(resolve).is_false()

    @api.model
    def _perms_for_user(self, user, model_names=None):
        _explicit, effective = self._user_group_sets(user)
        return self._perms_by_model(effective.ids, model_names=model_names)

    # ------------------------------------------------------------------
    # provenance
    # ------------------------------------------------------------------
    @api.model
    def _accesses_for(self, model_name, group_ids, mode=None):
        """The ``ir.access`` rows the server would consult, split by kind.

        Returns ``(restrictions, permissions)``. Selection matches
        ``Model._access_domain``: active, covering ``mode`` when one is given,
        and either without a group (a restriction, applied to everyone) or
        carrying one of the given groups (a permission).
        """
        group_ids = [gid for gid in (group_ids or []) if gid]
        Access = self.env["ir.access"].sudo()
        domain = [("active", "=", True), ("model_id.model", "=", model_name)]
        if mode:
            domain.append(("operation", "in", list(IN_SELECTION[mode])))
        rows = Access.search(domain)
        restrictions = rows.filtered(lambda access: not access.group_id)
        permissions = rows.filtered(lambda access: access.group_id.id in group_ids)
        return restrictions, permissions

    @api.model
    def _granting_accesses(self, model_name, group_ids):
        """Permissions that grant something on ``model_name``.

        Returns a list of dicts, each carrying the access row, the group that
        carries it and which operations it contributes.
        """
        _restrictions, permissions = self._accesses_for(model_name, group_ids)
        return [
            {"access": access, "group": access.group_id, "modes": modes_of(access.operation)}
            for access in permissions
        ]

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

        ``res.groups`` is ordered by ``privilege_id`` and the native "groups
        with access" helper renders groups as ``privilege/group``, so a bare
        group name is ambiguous in exactly the places this module reports.
        """
        if not group:
            return ""
        privilege = group.privilege_id.name or (group.privilege_id.category_id.name or "")
        return "%s / %s" % (privilege, group.name) if privilege else group.name

    # ------------------------------------------------------------------
    # decisions made in code
    # ------------------------------------------------------------------
    # A model that overrides _access_domain decides its access in Python: the
    # ir.access rows still say what they say, but the server may answer
    # differently. Detected structurally, from the model classes actually in
    # the registry (each carries ``_module``, set by the loader), so a module
    # we have never heard of is found just as reliably as a known one.
    OWN_MODULES = {"mdx_access_manager"}

    @api.model
    def _models_with_coded_access(self):
        """``{model_name: [module, ...]}`` for models that redefine the decision.

        Testing for "inherits the model" is useless - most addons extend core
        models to add fields. What matters is whether a class *redefines the
        decision*, so this looks for ``_access_domain`` in a class's own
        ``__dict__``; inherited attributes do not count.
        """
        found = {}
        for model_name, model_class in self.env.registry.items():
            for klass in model_class.__mro__:
                module = getattr(klass, "_module", None)
                if not module or module in self.OWN_MODULES:
                    continue
                if "_access_domain" in vars(klass):
                    found.setdefault(model_name, [])
                    if module not in found[model_name]:
                        found[model_name].append(module)
        return found

    @api.model
    def _coded_access_note(self, model_names=None):
        coded = self._models_with_coded_access()
        if model_names is not None:
            coded = {name: mods for name, mods in coded.items() if name in model_names}
        if not coded:
            return ""
        names = sorted(coded)
        shown = ", ".join(names[:5])
        if len(names) > 5:
            shown = self.env._("%(shown)s and %(more)s more", shown=shown, more=len(names) - 5)
        return self.env._(
            "Heads up: %(models)s decide part of their access in code. This matrix resolves the "
            "access rules stored in the database, so the server may answer differently there.",
            models=shown,
        )
