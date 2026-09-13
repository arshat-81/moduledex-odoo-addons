# ModuleDex Odoo Addons

Odoo 18 addons published by ModuleDex on the Odoo Apps Store:
https://apps.odoo.com/apps/modules/browse?author=ModuleDex

This repository holds both free (LGPL-3) and commercial (OPL-1) modules —
see Licensing below.

## Available addons

### External ID Finder

Technical helper for Odoo 18 administrators and developers.

It adds a focused screen under:

`Settings > Technical > Sequences & Identifiers > External ID Finder`

Use it to:

- Find records by XML ID.
- Find XML IDs from a model and database ID.
- Search external identifiers by text.
- Create a missing XML ID for an existing record.
- Open the linked Odoo record or native external identifier record.

### Security Simulator

Technical security helper for Odoo 18 administrators, developers, and implementers.

It adds a focused screen under:

`Settings > Technical > Security > Security Simulator`

Use it to:

- Simulate model access for a selected user.
- Review matching access rights and record rules.
- Compare read, write, create, and delete outcomes.
- Inspect user groups, restricted fields, menus, and actions.
- Open the native Odoo security records from the simulation results.

### Odoo Access Rights Manager

Odoo tells you which groups a user is in. It does not tell you what that means,
where a permission came from, or what will break if you change it.

- **Effective rights matrix** — every model a user can reach, and which of their
  groups were inherited rather than chosen. Odoo 18 writes implied groups onto
  the user record, so the user form cannot tell you this.
- **Provenance** — for any granted operation, the group, the implication path and
  the access control or record rule that produced it.
- **Compare** — two users side by side, or one user against a proposed group set.
- **Staged changes with a dry run** — preview exactly which permissions a change
  would gain and lose, then apply or discard. The preview writes nothing.
- **Audit trail** — every change to groups, access controls and record rules,
  with who made it and the before/after values. Module installs are excluded.
- **Findings** — redundant implications, access controls that can never grant,
  global record rules that silently narrow group rules, groups that hand out
  Settings access, and write access without read.

Read-only by default. The only write is a group change you explicitly apply, it
is made with the acting user's own rights rather than through `sudo`, and it is
logged.

### Odoo Performance Auditor

Read-only performance and health auditor for self-hosted Odoo 18.

It runs 41 checks against the PostgreSQL catalog, the ORM registry, the
scheduled-action and mail queues, and the server configuration, then:

- Ranks every finding against a tunable threshold, with the measured value and a
  recommendation.
- Scores the instance 0-100 and tracks the score across runs.
- Snapshots table and index growth weekly, so slowdowns can be shown over time.
- Produces a client-ready PDF Health Report.

Nothing it reads is written. The only write actions are three explicitly safe
maintenance commands — `ANALYZE`, `VACUUM` and `CREATE INDEX CONCURRENTLY` —
each behind a one-click confirmation.

`pg_stat_statements`, `pgstattuple` and `hypopg` unlock extra depth where they
are installed; without them those individual checks report as unavailable rather
than failing.

## Compatibility

- Odoo 18 Community
- Odoo 18 Enterprise

## Licensing

**This repository has no single license.** Each module is licensed
independently; the authoritative license is the `license` key in that module's
`__manifest__.py`, with the full text in its own `LICENSE` file.

- `mdx_external_id_finder`, `mdx_security_simulator` — **LGPL-3**, free software.
- `data_insight_workbench`, `mdx_perf_auditor`, `mdx_access_manager` — **OPL-1** (Odoo Proprietary
  License v1.0). May only be used with a valid purchased license, normally
  obtained through the Odoo Apps Store.

The presence of source code in this repository does not grant any right to use,
redistribute or resell the OPL-1 modules. See [`LICENSE`](LICENSE) and each
module's `LICENSE` file for exact terms.
