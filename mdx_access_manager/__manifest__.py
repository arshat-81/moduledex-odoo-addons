{
    # Title measured against the store, not guessed: proven sellers (500+ sold,
    # or 10+/month, or 5,000+ downloads) have a median title of 22-23 chars and
    # 0% of them carry two or more pipes, while 17% of apps with 1-19 sales do.
    # "Odoo" appears in ~1 in 3 selling titles. "Access Rights" is the phrase
    # buyers search; "Manager" says it writes, where the free companion module
    # only reads.
    "name": "Odoo Access Rights Manager",
    "version": "19.0.1.0.0",
    # The summary is the card subtitle AND feeds store search, so it has to do
    # both jobs: lead with the outcome, but carry the nouns people type. "group"
    # and "audit" were missing from title+summary and are now covered; "security"
    # and "role" stay in the description rather than being forced into a sentence
    # that still has to read like English on a card.
    "summary": "Resolve any user's real access rights, see which group granted each one, "
               "audit every change, and preview a permission change before you apply it",
    "description": """
Odoo Access Rights Manager
==========================

Odoo tells you which groups a user is in. It does not tell you what that
actually means, where a permission came from, or what will break if you change
it. This module answers those three questions.

Built for the way Odoo 19 organises security: groups are grouped under
privileges, and the roles you assign rarely match the access a user ends up
with. Every report here names the privilege alongside the group, because a bare
group name is ambiguous in exactly the places it matters.

Effective rights matrix
    Every model a user can reach, with read / write / create / delete resolved
    the way the server resolves it — through the full transitive closure of
    implied groups, not just the groups on the user form.

Provenance
    For any allowed operation, the chain that granted it: the explicitly
    assigned group, the implication path to the group that actually carries the
    access control, and the ACL or record rule itself.

Compare
    Two users side by side, or one user against a proposed set of groups, as a
    diff of gained and lost access.

Staged changes with a dry run
    Stage group additions and removals, see exactly which permissions the change
    would gain or lose, then apply or discard. The preview resolves a
    hypothetical group set in memory — nothing is written to compute it.

Audit trail
    Every change to groups, access controls and record rules, with who changed
    it, when, and the before/after values.

Findings
    Detectors for the configuration that causes access bugs: redundant
    implications, access controls that can never grant anything because a
    broader one already does, global record rules that silently nullify
    group rules, and unexpected paths to administrator.

Read-only by default. The only writes are the group changes you explicitly
apply from a staged change set, and every one of them is logged.
    """,
    "category": "Technical",
    "author": "ModuleDex",
    "maintainer": "ModuleDex",
    # Paid apps on the Odoo Apps Store must ship under the Odoo Proprietary
    # License; LGPL-3 cannot be sold there.
    "license": "OPL-1",
    "price": 179.00,
    "currency": "USD",
    "website": "https://apps.odoo.com/apps/modules/browse?author=ModuleDex",
    "support": "moduledex@gmail.com",
    "images": ["static/description/banner.png"],
    "depends": ["base"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/access_matrix_views.xml",
        "views/access_compare_views.xml",
        "views/access_change_set_views.xml",
        "views/access_change_log_views.xml",
        "views/access_finding_views.xml",
        "views/access_menus.xml",
    ],
    "application": True,
    "installable": True,
}
