# ModuleDex Odoo Addons

Odoo 19 addons published by **ModuleDex** on the
[Odoo Apps Store](https://apps.odoo.com/apps/modules/browse?author=ModuleDex).

This repository holds both free (LGPL-3) and commercial (OPL-1) modules —
see [Licensing](#licensing) below.

| Module | Description | License |
| --- | --- | --- |
| [BigCommerce Connector](#bigcommerce-connector) | Two-way BigCommerce ↔ Odoo sync | OPL-1 — $249 |
| [Odoo Module Upgrade AI](#odoo-module-upgrade-ai) | AI-assisted addon migration, Odoo 11–20 | OPL-1 — $149 |
| [Odoo Performance Auditor](#odoo-performance-auditor) | Read-only audit of database, ORM, cron and config | OPL-1 — $99 |
| [SQL Query & Report Builder](#sql-query--report-builder) | Safe ad-hoc SQL runner with reporting | OPL-1 — $59 |
| [External ID Finder](#external-id-finder) | Find, inspect and create XML IDs | LGPL-3 — Free |
| [Security Simulator](#security-simulator) | Simulate a user's access before granting it | LGPL-3 — Free |

## Commercial addons

### BigCommerce Connector

Two-way synchronisation between BigCommerce and Odoo, built around the parts of
the BigCommerce API most connectors leave out.

- Multi-Storefront/Channel-aware catalog and pricing sync.
- BigCommerce Price Lists synced to Odoo pricelists, tied to Customer Groups.
- Customers and addresses synced into `res.partner`.
- Publish Odoo products to BigCommerce as new catalog products.
- Targeted import of specific records by id, id range, or all.
- Refunds pulled in, optionally raising an Odoo credit note automatically.
- Abandoned cart tracking with one-click conversion into a CRM lead.
- Webhook-driven real-time sync, backed by a cron fallback.
- Rate-limit-aware API client honouring BigCommerce's backoff headers.
- Per-record audit log with before/after values, plus sales and catalog dashboards.

### Odoo Module Upgrade AI

Scan an Odoo addon from a server path or uploaded zip, compare its dependencies
against local Community and Enterprise addon roots, and generate an AI-assisted
migration report for any Odoo 11–20 source/target version pair.

- Upgrade *or* downgrade between any Odoo 11–20 version pair.
- Long-running AI work runs in the background through the module's own queue,
  driven by a standard Odoo scheduled action — no external queue module and no
  `odoo.conf` changes required.
- Stored API keys are encrypted at rest.
- Pluggable providers: OpenAI, Anthropic, Google Gemini, Groq, DeepSeek,
  Ollama, and any OpenAI-compatible endpoint.

> **On-premise / self-hosted only** (not compatible with Odoo Online). Source
> code is sent to whichever third-party AI provider you configure; Settings
> requires an explicit opt-in before any such call is made.
>
> Because AI calls can run for minutes, raise the cron watchdog in `odoo.conf`
> or the server will restart mid-migration:
>
> ```
> limit_time_real_cron = 3600
> ```

### Odoo Performance Auditor

Read-only auditor for self-hosted Odoo. Runs 41 checks against the PostgreSQL
catalog, the ORM registry, the scheduled-action and mail queues and the server
configuration, then ranks what it finds against tunable thresholds, scores the
instance 0-100 and produces a client-ready PDF Health Report.

Nothing it reads is written. The only write actions are three explicitly safe
maintenance commands — `ANALYZE`, `VACUUM` and `CREATE INDEX CONCURRENTLY` —
each behind a one-click confirmation.

> `pg_stat_statements`, `pgstattuple` and `hypopg` unlock extra depth where they
> are installed; without them those individual checks report as unavailable
> rather than failing.

### SQL Query & Report Builder

Safe SQL query runner with report previews, CSV export, execution history, and
admin full-access control.

## Free addons

### External ID Finder

Technical helper for Odoo 19 administrators and developers, under
`Settings > Technical > Sequences & Identifiers > External ID Finder`.

- Find records by XML ID.
- Find XML IDs from a model and database ID.
- Search external identifiers by text.
- Create a missing XML ID for an existing record.
- Open the linked Odoo record or native external identifier record.

### Security Simulator

Technical security helper for administrators, developers and implementers,
under `Settings > Technical > Security > Security Simulator`.

- Simulate model access for a selected user.
- Review matching access rights and record rules.
- Compare read, write, create, and delete outcomes.
- Inspect user groups, restricted fields, menus, and actions.
- Open the native Odoo security records from the simulation results.

## Compatibility

- Odoo 19 Community
- Odoo 19 Enterprise

Branches `17.0` and `18.0` hold earlier ports of `mdx_external_id_finder`,
`mdx_security_simulator` and `data_insight_workbench`. The same per-module
licensing applies there.

## Licensing

**This repository has no single license.** Each module is licensed
independently; the authoritative license is the `license` key in that module's
`__manifest__.py`, with the full text in its own `LICENSE` file.

- `mdx_external_id_finder`, `mdx_security_simulator` — **LGPL-3**, free software.
- `data_insight_workbench`, `ai_module_migrator`, `mdx_bigcommerce_connector` —
  **OPL-1** (Odoo Proprietary License v1.0). These may only be used with a valid
  purchased license, normally obtained through the Odoo Apps Store.

The presence of source code in this repository does not grant any right to use,
redistribute or resell the OPL-1 modules. See [`LICENSE`](LICENSE) and each
module's `LICENSE` file for exact terms.

## Support

moduledex@gmail.com
