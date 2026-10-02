# Store listing assets

All captured from the module running on a real Odoo 19 database. Nothing here is
a mockup — if a number appears in a caption, the screenshot shows it.

| File | View | What it shows |
|---|---|---|
| `banner.png` | — | 1200×600, ModuleDex plum palette |
| `icon.png` | — | 140×140 shield-and-keyhole mark |
| `screenshot_matrix.png` | Effective Rights | 366 models for Administrator, 16 assigned groups → 42 effective |
| `screenshot_provenance.png` | Effective Rights ▸ Why? | 13 provenance rows on `res.partner`, with implication chains and the global rule |
| `screenshot_change.png` | Staged Changes | Previewed change, 12 permissions gained, Draft→Previewed→Applied bar |
| `screenshot_compare.png` | Compare | New starter vs established colleague |
| `screenshot_findings.png` | Review ▸ Findings | Real scan of a standard database |

Captured at 1600×1000 with `device_scale_factor=2`, so the files are 3200×2000 and
stay sharp on a retina display.

## Recapturing

The capture scripts are disposable but the recipe is worth keeping:

1. Seed demo records (a matrix, a comparison, a previewed change set, a findings scan).
   The findings screenshot is more persuasive with a genuine critical finding, which
   needs a group that reaches `base.group_system` **indirectly** — a direct implication
   is rated warning, since the invisible path is the dangerous one.
2. Drive Chromium with Playwright. Do **not** wait on `networkidle`: Odoo holds a
   websocket open, so it never fires. Wait for `.o_action_manager` and then sleep.
3. Park the mouse (`page.mouse.move(20, 980)`) before capturing, or a row tooltip
   ends up in the shot.
4. Delete the demo records afterwards.
