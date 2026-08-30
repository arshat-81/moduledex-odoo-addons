# ModuleDex Odoo Addons

Odoo 17 addons published by ModuleDex on the Odoo Apps Store:
https://apps.odoo.com/apps/modules/browse?author=ModuleDex

This repository holds both free (LGPL-3) and commercial (OPL-1) modules —
see Licensing below.

## Available addons

### External ID Finder

Technical helper for Odoo 17 administrators and developers.

It adds a focused screen under:

`Settings > Technical > Sequences & Identifiers > External ID Finder`

Use it to:

- Find records by XML ID.
- Find XML IDs from a model and database ID.
- Search external identifiers by text.
- Create a missing XML ID for an existing record.
- Open the linked Odoo record or native external identifier record.

### Security Simulator

Technical helper for Odoo 17 administrators and implementers.

It adds a saved simulation screen under:

`Settings > Technical > Security > Security Simulator`

Use it to:

- Simulate access for a selected user, company scope, model, optional record, and domain.
- Review read, write, create, and delete results.
- Inspect matching access rights and record rules.
- Check user groups, restricted fields, menus, and actions.
- Reopen saved simulations for later comparison or documentation.

## Compatibility

- Odoo 17 Community
- Odoo 17 Enterprise

## Licensing

**This repository has no single license.** Each module is licensed
independently; the authoritative license is the `license` key in that module's
`__manifest__.py`, with the full text in its own `LICENSE` file.

- `mdx_external_id_finder`, `mdx_security_simulator` — **LGPL-3**, free software.
- `data_insight_workbench` — **OPL-1** (Odoo Proprietary License v1.0). May only
  be used with a valid purchased license, normally obtained through the Odoo
  Apps Store.

The presence of source code in this repository does not grant any right to use,
redistribute or resell the OPL-1 modules. See [`LICENSE`](LICENSE) and each
module's `LICENSE` file for exact terms.
