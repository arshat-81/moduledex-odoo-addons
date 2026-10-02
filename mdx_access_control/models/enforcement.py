"""Where the non-native rules are applied.

Model and record restrictions need nothing here - they are ``ir.access`` rows
and core enforces them. This file covers what ``ir.access`` cannot express:
the shape of the interface.

Every hook follows the same order of work, because all of them sit on paths
that run constantly: ask the cached snapshot whether any rule could possibly
concern this model, return core's answer untouched if not, and only then work
out which profiles restrict the current user.
"""

import re

from lxml import etree

from odoo import api, models

# view types whose columns are hidden with column_invisible rather than invisible
LIST_TAGS = ("list",)
# sub-view containers that can sit inside an x2many <field>
SUBVIEW_TAGS = ("list", "form", "kanban", "graph", "pivot", "calendar", "activity")


class Base(models.AbstractModel):
    _inherit = "base"

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    @api.model
    def get_view(self, view_id=None, view_type="form", **options):
        result = super().get_view(view_id, view_type, **options)
        if self.env.su:
            return result
        Profile = self.env["mdx.access.profile"]
        if not Profile._rules_snapshot()["ui_models"]:
            return result
        # A form embeds the views of its x2many fields, so a rule on another
        # model can apply inside this one. Only the profiles are checked here;
        # the walk below decides model by model.
        if not Profile._entries_for(self.env.user):
            return result

        node = etree.fromstring(result["arch"])
        changed = self._mdx_restrict_node(node, self._name, Profile, {})
        if changed:
            result = dict(result)
            result["arch"] = etree.tostring(node, encoding="unicode")
        return result

    @api.model
    def _mdx_rules_for(self, model_name, Profile, memo):
        if model_name not in memo:
            if model_name in Profile._rules_snapshot()["ui_models"]:
                memo[model_name] = (
                    Profile._field_rules(self.env.user, model_name),
                    Profile._element_rules(self.env.user, model_name),
                )
            else:
                memo[model_name] = ({}, {})
        return memo[model_name]

    @api.model
    def _mdx_restrict_node(self, root, model_name, Profile, memo):
        """Apply the user's rules to ``root``, a view of ``model_name``.

        Walks the tree by hand instead of using one XPath per rule, because the
        model changes on the way down: the children of an x2many field are a
        view of the comodel and take the comodel's rules.
        """
        field_rules, element_rules = self._mdx_rules_for(model_name, Profile, memo)
        model = self.env.get(model_name)
        buttons = element_rules.get("button", ())
        pages = element_rules.get("page", ())
        hide_chatter = bool(element_rules.get("chatter"))
        in_list = root.tag in LIST_TAGS
        in_search = root.tag == "search"
        hidden_fields = {name for name, flags in field_rules.items() if flags[0]}
        changed = False

        def visit(node, in_list_view):
            nonlocal changed
            for child in list(node):
                if not isinstance(child.tag, str):
                    continue
                tag = child.tag
                if tag == "field":
                    name = child.get("name")
                    comodel = None
                    if model is not None and name in model._fields:
                        comodel = getattr(model._fields[name], "comodel_name", None)
                    flags = field_rules.get(name)
                    if flags:
                        changed = True
                        if in_search and flags[0]:
                            node.remove(child)
                            continue
                        self._mdx_apply_field_flags(child, flags, in_list_view)
                    # embedded views belong to the comodel
                    if comodel and any(sub.tag in SUBVIEW_TAGS for sub in child):
                        for sub in child:
                            if isinstance(sub.tag, str) and sub.tag in SUBVIEW_TAGS:
                                if self._mdx_restrict_node(sub, comodel, Profile, memo):
                                    changed = True
                        continue
                elif tag == "label" and child.get("for") in hidden_fields:
                    child.set("invisible", "True")
                    changed = True
                elif tag == "button" and child.get("name") in buttons:
                    node.remove(child)
                    changed = True
                    continue
                elif tag == "page" and child.get("name") in pages:
                    # Hidden, not removed. A tab holds fields, and conditions
                    # elsewhere on the form may depend on them: take the tab
                    # out and the client can no longer evaluate those
                    # conditions, which breaks the whole form. An invisible
                    # tab keeps its fields loaded and is simply not shown.
                    child.set("invisible", "True")
                    changed = True
                elif tag == "chatter" and hide_chatter:
                    node.remove(child)
                    changed = True
                    continue
                elif tag == "filter" and in_search and hidden_fields:
                    if self._mdx_filter_uses(child, hidden_fields):
                        node.remove(child)
                        changed = True
                        continue
                visit(child, in_list_view or tag in LIST_TAGS)

        visit(root, in_list)
        return changed

    @api.model
    def _mdx_apply_field_flags(self, node, flags, in_list_view):
        invisible, readonly, required, _strict = flags
        if invisible:
            node.set("invisible", "True")
            if in_list_view:
                node.set("column_invisible", "True")
        if readonly:
            node.set("readonly", "True")
        if required and not (invisible or readonly):
            node.set("required", "True")

    @api.model
    def _mdx_filter_uses(self, node, hidden_fields):
        """Whether a search filter groups by, or filters on, a hidden field."""
        text = "%s %s %s" % (node.get("context") or "", node.get("domain") or "",
                             node.get("date") or "")
        if node.get("date") in hidden_fields:
            return True
        return any(
            re.search(r"['\"]%s(?::[a-z]+)?['\"]" % re.escape(name), text)
            for name in hidden_fields
        )

    # ------------------------------------------------------------------
    # field metadata: what filters, grouping, sorting and export offer
    # ------------------------------------------------------------------
    @api.model
    def fields_get(self, allfields=None, attributes=None):
        result = super().fields_get(allfields, attributes)
        if self.env.su:
            return result
        Profile = self.env["mdx.access.profile"]
        if self._name not in Profile._rules_snapshot()["ui_models"]:
            return result
        rules = Profile._field_rules(self.env.user, self._name)
        for name, (invisible, readonly, required, _strict) in rules.items():
            description = result.get(name)
            if description is None:
                continue
            if invisible:
                for key in ("searchable", "sortable", "groupable", "aggregator"):
                    if key in description:
                        description[key] = False
                if "exportable" in description:
                    description["exportable"] = False
            if readonly and "readonly" in description:
                description["readonly"] = True
            if required and not (invisible or readonly) and "required" in description:
                description["required"] = True
        return result

    # ------------------------------------------------------------------
    # optional: refuse writes on the server
    # ------------------------------------------------------------------
    @api.model
    def _has_field_access(self, field, operation):
        allowed = super()._has_field_access(field, operation)
        if not allowed or operation != "write" or self.env.su:
            return allowed
        Profile = self.env["mdx.access.profile"]
        if self._name not in Profile._rules_snapshot()["strict_models"]:
            return allowed
        flags = Profile._field_rules(self.env.user, self._name).get(field.name)
        return not (flags and flags[3])


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    @api.model
    def _visible_menu_ids(self, debug=False):
        # Core caches its answer per set of groups. A profile can single out
        # one user, so the subtraction has to happen outside that cache.
        visible = super()._visible_menu_ids(debug)
        if self.env.su:
            return visible
        hidden = self.env["mdx.access.profile"]._hidden_menu_ids(self.env.user)
        return visible - hidden if hidden else visible


class IrActionsActions(models.Model):
    _inherit = "ir.actions.actions"

    @api.model
    def get_bindings(self, model_name):
        result = super().get_bindings(model_name)
        if self.env.su:
            return result
        Profile = self.env["mdx.access.profile"]
        if model_name not in Profile._rules_snapshot()["binding_models"]:
            return result
        rules = Profile._element_rules(self.env.user, model_name)
        hidden = {
            "report": {int(n) for n in rules.get("report", ()) if str(n).isdigit()},
            "action": {int(n) for n in rules.get("action", ()) if str(n).isdigit()},
        }
        if not (hidden["report"] or hidden["action"]):
            return result
        # core hands back a cached structure: copy before filtering
        filtered = {}
        for binding_type, actions in result.items():
            blocked = hidden.get(binding_type, ())
            filtered[binding_type] = [a for a in actions if a.get("id") not in blocked]
        return filtered
