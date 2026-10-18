# Universalna LPZ maps

Turns the Universalna health-insurance network (clinics, labs, pharmacies) into maps, split by
region (oblast). Clinic markers differ by category: **1+ / 1 / 2 / 3 / 4**, plus **0**, which on
the site means dental/optics.

Data source: <https://universalna.com/lpz_all.html>. This is the table embedded in
<https://universalna.com/health-insurance/clinics/>. The script downloads it to
`data/lpz_all.html` when you pass `--fetch`, or automatically when no local copy exists. Raw
snapshots aren't committed (see `.gitignore`). A `data.json` export from the site also works as
input.

## Two outputs

| | Google My Maps (CSV) | Own web map (`site/`) |
|---|---|---|
| Setup | Manual import, 1 map per region | Host the `site/` folder anywhere static |
| Coordinates | My Maps geocodes the addresses itself (Google quality) | Needs `--geocode` (OpenStreetMap, or Google with an API key) |
| Category styling | "Style by data column → Категорія" (2 clicks per layer) | Built in: coloured pins labelled 1+/1/2/3/4/С |
| Limits | 2,000 rows per layer, 10 layers per map | None in practice |
| Updates | Re-import by hand | `python generate.py --fetch ...` or the GitHub Action |
| Extras | Opens in the Google Maps app on phones | Search, service filter (МРТ, педіатр…), "where am I", region switcher |

## Usage

Python 3.8+, no dependencies.

```bash
python generate.py --region lviv                            # CSVs only (no coordinates)
python generate.py --fetch --region lviv                    # pull fresh data first
python generate.py --fetch --region lviv --geocode --site   # + coordinates + web map data
python generate.py --region kyiv dnipro --geocode --site    # several regions
python generate.py --region all --geocode --site            # every oblast (≈14k addresses, ~8-10 h on OSM)
python generate.py --region filled --cached --site          # rebuild regions already on the map, no lookups
```

`--region` takes slugs, `all`, or `filled` (every region that already has `site/data/<slug>.js`).
Slugs: `cherkasy chernihiv chernivtsi dnipro donetsk ivano-frankivsk kharkiv kherson
khmelnytskyi kropyvnytskyi kyiv lviv mykolaiv odesa poltava rivne sumy ternopil vinnytsia volyn
zakarpattia zaporizhzhia zhytomyr`. Kyiv city and Kyiv oblast are both in `kyiv`.

Long runs can be stopped (Ctrl+C) and restarted at any time. The cache is saved every 25
lookups, and `--max-minutes N` stops new lookups after N minutes and still writes the outputs.

Outputs:
- `out/mymaps/<region>/clinics.csv`, `labs.csv`, `pharmacies.csv`. A file with more than 2,000
  rows is split into `_1`, `_2`, and so on, one per My Maps layer.
- `site/data/<region>.js` + `site/data/regions.js`, which `site/index.html` reads.
- `data/geocache.json`. Every geocoded address is cached here, so re-runs only look up new ones.

### Geocoding

- Default: OpenStreetMap Nominatim. It's free but limited to 1 request/s, so Lviv oblast took
  about 40 minutes the first time. Lviv oblast result (1,332 locations): 1,010 matched the exact
  building (76%), 258 the street (19%), 60 only the town (5%), and 4 weren't found. Those 4 are
  listed under the wrong oblast in the source. On the web map, town-only matches show as dashed
  pins.
- All 23 oblasts are geocoded and cached in `data/geocache.json`. 13,886 of 13,968 locations
  (99.4%) are on the map: 71% matched the exact building, 18% the street, and 10% only the town.
  Every region is above 98%, except Volyn at 95%.
- Each hit must be in the right oblast and the right town. OSM has no oblast for some places
  (e.g. around Lutsk); there only the town name is checked. Points far from the rest of their town
  are re-geocoded once automatically. "Far" means more than 4× the town's median spread, at
  least 4 km, so big cities like Kyiv aren't penalised. Re-checked points are marked in the cache
  and never retried. Anything still off is printed as a `! far from other … locations` warning.
  `--retry-missing` retries addresses that weren't found before.
- Better: set `GOOGLE_MAPS_API_KEY` (Geocoding API enabled). Google's free tier covers about
  10k requests/month, enough for several oblasts. Delete the region's entries from
  `data/geocache.json`, or the whole file, to re-geocode them.

## Importing into Google My Maps (Lviv)

1. Go to <https://mymaps.google.com>, click **Create a new map**, and name it "Universalna — Львів".
2. Layer 1: **Import** `out/mymaps/lviv/clinics.csv`.
   - Columns to position placemarks: tick **Адреса для мапи**. Or, if you ran `--geocode`, tick
     **Latitude/Longitude** instead (then nothing gets "unrecognised").
   - Column to title markers: **Назва**.
3. Click the layer's paint roller (**Individual styles**). Choose **Style by data column →
   Категорія**. Set a colour/icon for each of 1+, 1, 2, 3, 4, 0. Tip: tick **Show labels**
   to see the category on the pin.
4. **Add layer**, then import `labs.csv`. **Add layer** again, then import `pharmacies.csv`.
   Uncheck the pharmacies layer when you don't need it.
5. If My Maps reports rows it couldn't place, open **Open data table** and fix those addresses
   by hand.

Each row's popup shows the services, notes, franchise and a Google Maps link.

## Hosting the web map

`site/` is fully static. You can serve it from GitHub Pages, Netlify, Cloudflare Pages, or any
web server. Locally: `python -m http.server -d site 8000` → <http://localhost:8000/#lviv>.
The region dropdown lists every region that has a `site/data/<slug>.js` file.

### GitHub Pages + automatic updates

Free GitHub Pages needs a **public** repo. The data is public on universalna.com anyway.
Netlify and Cloudflare Pages can host from a private repo instead.

1. Create an empty repo on GitHub, then push:
   ```bash
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. In the repo, open **Settings → Pages → Build and deployment → Source** and choose
   **GitHub Actions**.
3. Open **Actions → Update map → Run workflow** and keep the default `filled`. This publishes the
   regions already in the repo (Lviv). The site goes live at
   `https://<you>.github.io/<repo>/`.

[`.github/workflows/update-map.yml`](.github/workflows/update-map.yml):
- **Every Monday** it downloads fresh data and updates every region already on the map
  (`filled`). Only new or changed addresses are geocoded, so a run takes minutes. Then it
  redeploys the site.
- **Adding regions**: start it by hand with region slugs, e.g. `kyiv` or `dnipro kharkiv odesa`.
  It commits after each region. Before GitHub's 6-hour job limit it stops new lookups and commits
  what it has. If the log says "run again to continue", start it again with the same input.
  Kyiv oblast (~3,500 locations) needs about 3 hours. All of Ukraine needs about 8–10 hours,
  i.e. 2–3 runs.
- Alternative: fill regions on your PC with `python generate.py --region all --geocode --site`
  (overnight, resumable), then commit and push. The weekly job takes over from there.
- **Search engines**: `site/index.html` has a description, Open Graph tags (link previews in
  Telegram/Facebook/Viber), JSON-LD and a crawlable "Про мапу" text. The workflow writes
  `robots.txt` and `sitemap.xml` for `https://<you>.github.io/<repo>/`. With a custom domain, set
  a `SITE_URL` repository variable (Settings → Secrets and variables → Actions → Variables). To
  get indexed sooner, add the site in [Google Search Console](https://search.google.com/search-console)
  and submit `sitemap.xml`.
- Optional: add a `GOOGLE_MAPS_API_KEY` repository secret (Settings → Secrets → Actions) to
  geocode with Google. It's faster and more accurate than OpenStreetMap, which sometimes
  throttles GitHub's shared IPs.
