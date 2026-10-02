# ModuleDex banner collection

All module banners use a 1600 × 800 editorial layout: warm paper, charcoal workflow panels, Montserrat typography, a product accent, three verified feature statements, and the branch-specific Odoo version. Each product has its own workflow illustration. These are explanatory illustrations, not screenshots or benchmark results. Existing screenshots and listing feature descriptions are retained.

## Research and design decisions

Reviewed local Dashboard Ninja (Nuventura), Hide Menu User (bestomart), and the existing ModuleDex banners. Dashboard Ninja uses a prominent benefit statement and animated feature demonstrations; Hide Menu User makes the single use case immediately visible. The old ModuleDex collection mixes several styles and includes stale Odoo 19 labels in the 20.0 branch. Access Control previously reused the Access Rights Manager artwork and had no banner in its description page.

Compared the public listings for [Dashboard Ninja](https://apps.odoo.com/apps/modules/19.0/ks_dashboard_ninja), [Simplify Access Management](https://apps.odoo.com/apps/modules/19.0/simplify_access_management), and [Performance Profiler](https://apps.odoo.com/apps/modules/19.0/perf_console). Search results supplied feature positioning; fetching the full live pages was unavailable. Visual analysis is based on the locally available competitor assets, not unobserved live artwork.

The design adopts readable product names, a concise benefit, and a feature workflow. All artwork is original vector geometry; competitor artwork is not incorporated. Accent colours and workflow diagrams distinguish products while the layout ties the collection together. Version badges identify the actual addon branch, independently of target migration versions.

## Assets and regeneration

- `static/description/banner.svg`: editable vector source.
- `static/description/banner.png`: primary Odoo Apps image, 1600 × 800.
- Existing GIF banners: four frames with a restrained moving accent; first frame is complete and readable.
- Existing legacy banner frames and SVG sources are refreshed to match.
- Description pages keep their existing banner filenames. Obsolete cache query strings are removed. Access Control receives its missing hero image.

Run from this checkout:

```sh
python3 -m pip install cairosvg Pillow
python3 tools/banner_design.py
```

Rendering requires Montserrat and DejaVu Sans Mono fonts. The current artwork was generated with the system fonts in `/usr/share/fonts/truetype/`. SVGs use a sans-serif fallback. No module runtime files or manifest feature declarations are changed.
