# Performance & Health Auditor (`mdx_perf_auditor`)

Read-only auditor for self-hosted Odoo 19. Finds the database, ORM, scheduled-job
and configuration problems that make an instance slow, ranks them, scores overall
health, and offers one-click safe fixes for the few changes that are safe to
automate.

## What it does

| Group | Detectors | Source |
|---|---|---|
| **A — Database** | A1 unindexed FKs · A2 unindexed sort columns · A3 dead-tuple bloat · A4 stale statistics · A5 unused indexes · A6 seq-scan-heavy tables · A7 very large tables · A8 low cache-hit ratio · A9 unbounded tables | PostgreSQL catalog / `pg_stat_*` |
| **B — ORM & model design** | B1 sort on computed field · B2 compute without `@api.depends` · B4 non-stored related field in list views · B5 `create()` not batch-safe · B6 deep view-inheritance chains · B7 `_log_access` on huge tables | Loaded registry |
| **C — Runtime** | C0 no profiling data · C1 slow requests · C3 N+1 query patterns · C6 slow SQL | `ir.profile` / `pg_stat_statements` |
| **D — Background jobs** | D2 cron overruns its interval · D3 failing / auto-disabled crons · D5 mail queue backlog · D6 job-queue backlog | `ir.cron`, `perf.cron.run`, `mail.mail`, `queue.job` |
| **E — Assets** | E1 large compiled bundles · E2 bundle fragmentation | `ir.attachment`, `ir.asset` |
| **F — Configuration** | F1 PG memory settings · F2 planner settings · F3 `pg_stat_statements` missing · F4 dev flags in prod · F5 profiling left on · F6 worker configuration | `pg_settings`, server config, `/proc/meminfo` |

## Deep analysis

The quick scan (~0.1 s) tells you *where* to look. **Deep analysis** answers *why*,
by working from real captured traffic and real query plans.

1. **Start a capture** from the dashboard (1–120 minutes). Odoo's own profiler is
   switched on for the window and records every statement with its real
   parameters into `ir.profile`. Outside a window the hook costs nothing.
2. **Use the parts of Odoo that feel slow.**
3. **Run Deep analysis.** It runs everything the quick scan does, plus:

| Group | Detectors | Needs |
|---|---|---|
| **G — Query plans** | G0 no captured traffic · G1 sequential scan in a slow query (names the exact index to create) · G2 planner row estimate wrong · G3 sort spilled to disk · G4 nested loop over an unindexed relation | a capture |
| **C — Runtime** | C1 slow requests · C3 N+1 patterns · C6 slow statements — all become live once traffic is captured | a capture |
| **H — Growth** | H0 not enough history · H1 fastest-growing tables with a 90-day projection · H2 autovacuum falling behind | 2+ snapshots |
| **J — Measured bloat** | J1 real table bloat · J2 index leaf density | `pgstattuple` |
| **Planner validation** | every index this module recommends is offered to the planner as a hypothetical index; recommendations it would ignore are demoted to advisory instead of shown as work | `hypopg` |

Slow statements are replayed under `EXPLAIN (ANALYZE, BUFFERS)` inside a savepoint
with a 5 s timeout, and only when they are pure reads — the auditor's own catalog
queries are excluded so it never profiles itself.

**Model Deep Dive** (`Performance ▸ Model Deep Dive`) reports on one model at a
time: storage and scan counts, the field inventory, every index with its size and
usage, unindexed foreign keys, which writes trigger which recomputes, and the
slowest captured statements touching its table.

## Safety

* Every detector is read-only. No business data is touched.
* The only statements "Apply safe fix" will run are `ANALYZE`, `VACUUM`, and
  `CREATE INDEX CONCURRENTLY IF NOT EXISTS`, matched against an allow-list, run
  on a separate autocommit connection, logged to the finding's chatter, and
  gated to Settings administrators.
* `DROP INDEX` and configuration changes are shown as text for you to run
  yourself.

## Usage

1. Install; open **Performance ▸ Run Audit Now** (or the Dashboard).
2. Review **Findings**, grouped by category. Acknowledge, snooze, or ignore.
3. **Print Health Report** from an audit run for a PDF to keep or send on.
4. A weekly cron re-runs the audit; an hourly cron snapshots PostgreSQL stats
   for the trend chart. Both can be disabled in **Settings ▸ Technical ▸
   Scheduled Actions**.

## Configuring it

**Settings ▸ Performance** covers what actually gets tuned: alert recipients and
severity, snapshot and cron-timing retention, default capture length, and the four
sensitivity knobs that decide how noisy the findings list is.

**Performance ▸ Detectors** lists all 41 checks with their category, depth, default
severity, how many findings each is currently raising, and a toggle to switch one
off. Each row names the `ir.config_parameter` keys that tune it, so the remaining
thresholds are discoverable rather than buried in the source — set them as
`mdx_perf_auditor.threshold.<key>`.

## Alerting

The weekly scheduled audit emails the users named in Settings, but only when a
finding appears that was not open before. A quiet week sends nothing.

## Tests

```
odoo-bin -d <db> -u mdx_perf_auditor --test-enable --test-tags /mdx_perf_auditor
```
25 tests: the run lifecycle and scoring, a contract test that every detector runs
and returns well-formed rows, the maintenance-SQL allow-list (including injection
attempts), and one regression test per bug that ever shipped.

## Configuration

Thresholds are `ir.config_parameter` keys `mdx_perf_auditor.threshold.<key>`
(see each detector for names and defaults). Disable individual detectors with
`mdx_perf_auditor.disabled_detectors` = comma-separated codes, e.g. `A7,E2`.

## Requirements

* Odoo 19.0, Community or Enterprise
* Self-hosted / on-premise. On managed hosting where the database user is not a
  superuser, `pg_settings` and `pg_stat_statements` checks degrade to advisory.
