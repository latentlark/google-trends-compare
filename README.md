# Google Trends Compare: Unlimited Keywords on One Scale

**For SEO and content teams, e-commerce and product researchers, brand and market analysts, investors and data scientists.** Google Trends shows at most **5 keywords** at once, and every chart is rescaled to its own 0-100. Values from two different charts **cannot be compared**. This Actor fixes that.

**Outcome:** you get one clean table where **every keyword, of any count, sits on the same 0-100 scale** in each country. The history can be **daily for any range back to 2004**. You don't need a login, a Google account or an API key.

- ♾️ **Unlimited keywords, one scale.** It ranks your keywords first, then groups them into size-based tiers of up to 5 that share link terms, so small terms stay precise next to giants. In tests the stitched scale was within **0.05-0.25 points** (mean absolute error) of Google's own 5-keyword chart.
- 📅 **Daily history for any range.** Google only returns daily points for ranges under ~8 months. `daily-stitched` chains overlapping 240-day windows into one continuous daily series for 1, 5 or 20 years. In tests the window overlaps correlated at **0.9996+**, and the daily series matched Google's own weekly series at **0.90-0.9995** correlation.
- 🌍 **Any country or worldwide**, plus Google Trends categories and search properties (web, image, news, Shopping, YouTube).
- 🛡️ **Built not to fail.** Every request is spaced by at least 1.5 s, with backoff retries and a fresh IP after the 2nd failure. Datacenter proxy is the default, with an **automatic residential fallback**. A country that still fails is **not charged**.
- 🔍 **Transparent QA record.** For every run you get the tiers, link factors, window factors, correlation vs Google's coarse series, an optional verify-sample error, and network stats.
- 💸 **Pay per keyword series:** $0.004 per keyword × country on the Free plan (cheaper on paid plans).

## Quick start (first run in about a minute)
1. Click **Try for free** (or **Start** in Console). The form is prefilled with **7 drinks keywords** (`coffee`, `tea`, `beer`, `kombucha`, `matcha`, `energy drink`, `cold brew`), country **US** and the **last 12 months**.
2. Click **Start**. In about a minute you get **~370 rows**: 7 keywords × ~53 weekly points on one shared 0-100 scale. That is something Google's UI cannot show in one chart. It costs about **$0.03** ($0.005 start + 7 × $0.004).
3. Paste your own keywords (10, 50, 200…), add countries (`GB`, `DE`, `BR`, `IN`, `WORLDWIDE`), and switch **Resolution** to **Daily for any range** for long daily history.

## What you can do with it
- **Market and brand share:** rank 30 competitors, products or features by search interest in each country on one scale, and track the share over time.
- **SEO and content planning:** compare dozens of topic or keyword ideas at once, so you are no longer stuck with 5-term charts and copied numbers.
- **Seasonality and demand forecasting:** daily series over 2-5+ years for model features, such as retail, travel or seasonal products.
- **Trend and investment research:** crypto, stocks, consumer brands or AI tools, with long daily history and multiple countries.
- **Country comparison:** run the same keyword set across 10 countries with each country on its own consistent scale.
- **Dashboards:** schedule weekly runs and send the dataset to Google Sheets with [Google Sheets Export](https://apify.com/latentlark/sheets-export).

## Input
| Field | What it does |
|---|---|
| `keywords` | Any number of search terms (required). All of them end up on one scale per country |
| `geos` | ISO country codes (`US`, `GB`, `DE`, `BR`, `IN`, `JP`…) or `WORLDWIDE`. Default `US` |
| `timeRange` | `today 1-m`, `today 3-m`, `today 12-m` (default), `today 5-y`, `all`, `today N-y/N-m/N-d`, or `YYYY-MM-DD YYYY-MM-DD` |
| `resolution` | `auto` (Google's daily/weekly/monthly, cheapest) or `daily-stitched` (daily for any range) |
| `scaleMode` | `accurate` (default: pre-rank + size-based tiers) or `fast` (single anchor). Same price |
| `outputFormat` | `long` (row per keyword × date, default) or `wide` (row per date with `values{keyword: value}`) |
| `category`, `property` | Google Trends category id (0 = all) and search property (web, `images`, `news`, `froogle` = Shopping, `youtube`) |
| `verifySample` | One extra real 5-keyword Google request; reports the scale error in the QA record (free) |
| Advanced | `proxyMode` (`auto`/`datacenter`/`residential`/`none`), `pacingSecs` (≥1.5), `maxAttempts`, `fallbackAfterFailures`, `tierMaxRatio`, `anchorKeyword` |

**Example: 12 brands, 3 countries, 5 years:**
```json
{
  "keywords": ["nike", "adidas", "puma", "new balance", "asics", "under armour", "reebok", "hoka", "on running", "salomon", "vans", "converse"],
  "geos": ["US", "GB", "DE"],
  "timeRange": "today 5-y"
}
```
That is 36 keyword series, about **$0.15**.

**Example: 2 years of daily history, wide output:**
```json
{
  "keywords": ["bitcoin", "ethereum", "solana", "dogecoin", "cardano", "chainlink", "polkadot"],
  "geos": ["WORLDWIDE"],
  "timeRange": "today 2-y",
  "resolution": "daily-stitched",
  "outputFormat": "wide"
}
```
That is 731 daily rows. The cost is 7 series + 14 daily-history-years, about **$0.075**.

## Output
`long` format: one dataset row per keyword × country × date.
```json
{
  "keyword": "coffee",
  "geo": "GB",
  "date": "2024-10-01",
  "value": 14.438,
  "resolution": "daily",
  "isPartial": false,
  "timeRange": "2024-10-01 2026-10-25",
  "scaleId": "GB|2024-10-01 2026-10-25|daily-stitched",
  "tier": 1
}
```
`wide` format: one row per country × date.
```json
{
  "geo": "JP",
  "date": "2025-10-26",
  "resolution": "weekly",
  "isPartial": false,
  "timeRange": "today 12-m",
  "scaleId": "JP|today 12-m|auto",
  "values": { "sushi": 78, "ramen": 76, "curry": 33, "udon": 18, "yakitori": 9, "tempura": 9.507 }
}
```
- `value` is on **one shared 0-100 scale per `scaleId`** (country × range × resolution): 100 is the single highest point of any keyword in that country. Small terms keep decimals instead of rounding to 0 or 1.
- `isPartial: true` marks Google's still-incomplete latest period.
- `tier` is the size tier the keyword was measured in (1 = biggest).
- **Storage → Key-value store → `QA`** holds a per-keyword summary (mean, max, share of total), the tiers and link factors, the stitch windows and their correlation, the verify-sample error, the billing counts and network stats.

## Pricing
Pay per event, with volume discounts on higher Apify plans:

| Event | What it is | Free plan | Bronze | Silver | Gold+ |
|---|---|---|---|---|---|
| `apify-actor-start` | Once per run (per GB of memory; default 256 MB = one start) | $0.005 | $0.005 | $0.005 | $0.005 |
| `keyword-series` | One keyword × one country delivered on the shared scale (any range, Google's resolution) | $0.004 | $0.0036 | $0.0032 | $0.0028 |
| `daily-history-year` | Only with `daily-stitched` and ranges over 240 days: per keyword × country × year of stitched daily data | $0.003 | $0.0027 | $0.0024 | $0.0021 |

- **Free:** countries that fail after all retries, verify-sample requests, the QA record, and `wide` vs `long` (same price).
- Examples (Free plan):
  - 7 keywords × US × 12 months ≈ **$0.033**
  - 50 keywords × 1 country × 5 years weekly ≈ **$0.21**
  - 50 keywords × 5 years **daily** ≈ **$0.96**
  - 200 keywords × 10 countries × 12 months ≈ **$8.01**
- Set **Max cost per run** in Console to cap spend. Before it starts, the Actor trims countries (then keywords) to fit your limit, so it never overspends.

## How it works and limitations
- **Scale.** Google normalises every request to its own 0-100. The Actor first ranks all keywords against one anchor. It then measures them in size-ordered tiers of up to 5, where neighbouring tiers share a link keyword, and only admits terms within `tierMaxRatio` (10×) of the tier's top term. Tiers are chained by the link term's ratio, and the result is rescaled so the global maximum is 100.
- **Daily stitching.** The Actor requests 240-day daily windows that overlap by 60 days, from newest to oldest. Each window is scaled to the next one using the overlap across all keywords. When the overlap has no signal, it falls back to Google's weekly/monthly series.
- **Google's data is sampled.** Google Trends values come from a sample of searches. Repeated requests can differ by a point or two, and very small terms (mean below ~1 on the shared scale) are noisy. Use `category` to disambiguate words like "apple".
- **Very long keyword chains** (50+ keywords spanning several orders of magnitude) add small errors at each link. `verifySample` measures it, and the QA record flags weak links.
- **Speed.** Each Google request is spaced by ≥1.5 s to stay unblocked. A ranking pass plus tier requests: 50 keywords ≈ 25-30 requests ≈ 1-2 min per country and range. Daily stitching multiplies that by the number of windows (5 years ≈ 10 windows).
- **Public data only.** No login and no Google account. The Actor does not provide related queries or regional breakdowns (interest over time only).

## FAQ
**I got 0 results / the run failed.** Check the country code (ISO 2-letter, or `WORLDWIDE`) and the time range format (`today 12-m`, or `2024-01-01 2026-01-01`). Keywords with almost no searches return all-zero series, not missing rows. The run's status message and the `QA` record (`geos.<country>.error`) say what happened. Countries that fail are not charged.

**I see 429 / "rate limited" in the log.** That is Google throttling a request. The Actor waits (2/4/8/16 s backoff), retries on a new IP, and switches to residential proxy automatically if the datacenter keeps failing. In tests every 429 recovered. If you set `proxyMode: datacenter` and it still fails, try `auto` or `residential`, or raise `pacingSecs`.

**Why do my numbers differ from the Google Trends website?** On the website each chart has its own 0-100. Here all keywords share one scale, so a keyword at 100 on its own Google chart can be 3.2 here next to a bigger term. Ratios between keywords match Google's to within a fraction of a point. Turn on `verifySample` to see the measured error vs a real Google chart.

**Can I compare countries with each other?** Each country gets its own scale (Google normalises by country). Compare keyword shares within a country, and trends across countries.

**How do I track this weekly?** Create a schedule with the same input and send the dataset to Google Sheets with [Google Sheets Export](https://apify.com/latentlark/sheets-export).

## More from LatentLark
- [Google Sheets Export & Import (No OAuth)](https://apify.com/latentlark/sheets-export): send any dataset to Google Sheets.
- [Google Maps Rank Tracker](https://apify.com/latentlark/google-maps-rank-tracker): geo-grid local SEO rankings.
- [Google Ads Transparency & LinkedIn Ad Library Scraper](https://apify.com/latentlark/competitor-ads-monitor): your competitors' ads on Google and LinkedIn.
- [TikTok Ads Library Scraper](https://apify.com/latentlark/tiktok-ads-library): TikTok EU Ad Library and Top Ads.
- [TikTok Transcript Scraper](https://apify.com/latentlark/tiktok-transcript): transcripts of TikTok videos.
