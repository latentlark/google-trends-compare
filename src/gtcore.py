"""Google Trends consistent-scale engine (PRIVATE LatentLark Day-2 scaffold).

- Unlimited keywords on ONE 0-100 scale: pre-rank pass (single anchor) -> size-based tiers of 5
  chained through one shared link term (sum-ratio) -> global renormalise (decimals kept).
- Long daily history: <=240-day daily windows with 60-day overlap, stitched newest->oldest by
  overlap sum-ratio across all terms; coarse (weekly/monthly) fallback + QA correlation.
- Proxy: datacenter default (fresh session per IP, boot cookie page once), residential fallback
  (no 900 KB boot page). Port/host/password come from env; proxy URLs are never logged.
- Pacing >= 1.5 s between Google requests, 429 backoff 2/4/8/16 s + jitter, new IP after 2nd fail.
Pure requests; no Apify SDK here so it can be tested on the box with proxy mode "none".
"""
from __future__ import annotations
import datetime as dt, json, math, os, random, re, statistics as st, time
import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
MIN_PACE = 1.5
DAILY_MAX_DAYS = 240     # Google gives daily up to ~269 d; keep margin
OVERLAP_DAYS = 60        # >= 30 required by Day-0 finding
MIN_OVERLAP = 30
EARLIEST = dt.date(2004, 1, 1)

class GTError(Exception):
    pass

def _redact(s: str) -> str:
    pw = os.environ.get("APIFY_PROXY_PASSWORD")
    s = str(s)
    if pw:
        s = s.replace(pw, "***")
    return re.sub(r"(https?://[^:\s/@]+):([^@\s/]+)@", r"\1:***@", s)

# ---------------------------------------------------------------- HTTP client
class Client:
    def __init__(self, mode="auto", dc_groups=None, country=None, pace=MIN_PACE, max_attempts=5,
                 fallback_after_429=6, log=print, debug_fail_dc=False):
        self.mode = mode                       # auto | datacenter | residential | none
        self.cur = {"auto": "datacenter"}.get(mode, mode)
        self.dc_groups = [g for g in (dc_groups or []) if g]
        self.country = country
        self.pace = max(MIN_PACE, float(pace or MIN_PACE))
        self.max_attempts = max(1, int(max_attempts))
        self.fallback_after_429 = int(fallback_after_429)
        self.log = log
        self.debug_fail_dc = debug_fail_dc
        self.reqlog: list[dict] = []
        self.bytes = 0
        self.dc_fail_count = 0
        self.switched_at = None
        self._last = 0.0
        self.deadline = None                   # epoch secs; set for small jobs so Store QA finishes < 5 min
        self.new_session()

    # proxy ---------------------------------------------------------------
    def _proxy_url(self, session: str) -> str | None:
        if self.cur == "none":
            return None
        pw = os.environ.get("APIFY_PROXY_PASSWORD")
        if not pw:
            raise GTError("APIFY_PROXY_PASSWORD not set (run on Apify or use proxyMode none)")
        host = os.environ.get("APIFY_PROXY_HOSTNAME", "proxy.apify.com")
        port = os.environ.get("APIFY_PROXY_PORT", "8000")   # 8011 on platform: always read env
        parts = []
        if self.cur == "residential":
            parts.append("groups-RESIDENTIAL")
        elif self.dc_groups:
            parts.append("groups-" + "+".join(self.dc_groups))
        parts.append(f"session-{session}")
        if self.country and self.cur == "residential":
            parts.append(f"country-{self.country}")
        return f"http://{','.join(parts)}:{pw}@{host}:{port}"

    def new_session(self):
        self.S = requests.Session()
        self.S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        sid = f"gt{random.randint(1, 10**9)}"
        u = self._proxy_url(sid)
        if u:
            self.S.proxies = {"http": u, "https": u}
        # cookie boot = HEAD request (sets NID, 0-byte body). The ~900 KB HTML boot page is never fetched.
        self.booted = False

    def switch_to_residential(self, why: str):
        if self.mode != "auto" or self.cur == "residential":
            return
        self.cur = "residential"
        self.switched_at = {"after_requests": len(self.reqlog), "reason": why}
        self.log(f"proxy fallback: datacenter -> residential ({why})")
        self.new_session()

    # requests ------------------------------------------------------------
    def _wait(self):
        d = self.pace - (time.time() - self._last)
        if d > 0:
            time.sleep(d)

    def _past_deadline(self):
        return self.deadline is not None and time.time() > self.deadline

    def _get(self, kind, url, method="GET", **kw):
        if self._past_deadline():
            return "DEADLINE"
        self._wait()
        t = time.time()
        try:
            if self.debug_fail_dc and self.cur == "datacenter" and kind != "boot":
                raise requests.ConnectionError("debug: simulated datacenter failure")
            r = self.S.request(method, url, timeout=(10, 25), allow_redirects=True, **kw)
        except Exception as e:
            self._last = time.time()
            code = "EXC:" + type(e).__name__
            self.reqlog.append({"k": kind, "code": code, "proxy": self.cur, "s": round(time.time() - t, 2)})
            return code
        self._last = time.time()
        n = len(r.content) + 600 + sum(len(k) + len(v) for k, v in r.headers.items())
        self.bytes += n
        head = r.content[:5000]
        cap = ("sorry" in r.url) or (b"unusual traffic" in head) or (r.status_code != 200 and b"captcha" in head.lower())
        code = "CAPTCHA" if cap else r.status_code
        self.reqlog.append({"k": kind, "code": code, "proxy": self.cur, "b": n, "s": round(time.time() - t, 2)})
        return r if code == 200 else code

    @staticmethod
    def _json(text):
        i = text.find("{")
        if i < 0:
            raise ValueError("no json")
        return json.loads(text[i:])

    def _once(self, kws, geo, tm, cat, prop):
        if not self.booted:
            self._get("boot", f"https://trends.google.com/trends/?geo={geo or 'US'}", method="HEAD")
            self.booted = True
        req = {"comparisonItem": [{"keyword": k, "geo": geo, "time": tm} for k in kws],
               "category": cat, "property": prop}
        r = self._get("explore", "https://trends.google.com/trends/api/explore",
                      params={"hl": "en-US", "tz": "0", "req": json.dumps(req)})
        if not isinstance(r, requests.Response):
            return None, ("explore", r)
        try:
            w = [x for x in self._json(r.text)["widgets"] if x["id"] == "TIMESERIES"][0]
        except Exception:
            return None, ("explore", "PARSE")
        r = self._get("multiline", "https://trends.google.com/trends/api/widgetdata/multiline",
                      params={"hl": "en-US", "tz": "0", "req": json.dumps(w["request"]), "token": w["token"]})
        if not isinstance(r, requests.Response):
            return None, ("multiline", r)
        try:
            d = self._json(r.text)["default"]["timelineData"]
        except Exception:
            return None, ("multiline", "PARSE")
        if not d:
            return None, ("multiline", "EMPTY")
        rows = [{"t": int(x["time"]), "v": [float(v) for v in x["value"]],
                 "partial": bool(x.get("isPartial", False))} for x in d]
        return rows, 200

    def series(self, kws, geo, tm, cat=0, prop=""):
        """Fetch one <=5-term request with retries + DC->residential fallback."""
        if len(kws) > 5:
            raise GTError("max 5 terms per Google request")
        errs = []
        att, budget = 0, self.max_attempts
        while att < budget:
            rows, c = self._once(kws, geo, tm, cat, prop)
            if rows is not None:
                return rows, {"attempts": att + 1, "errs": errs, "proxy": self.cur}
            errs.append(list(c))
            if c[1] == 400:   # bad request (invalid keyword/time/geo): retrying won't help
                raise GTError(f"Google rejected request (400) for {kws} {geo} {tm}")
            if self._past_deadline():
                raise GTError(f"time limit reached after {att + 1} attempts on {kws} {geo} {tm} (Google slow or rate-limiting): {errs}")
            switched = False
            if self.cur == "datacenter":
                self.dc_fail_count += 1
                if self.dc_fail_count >= self.fallback_after_429 or att >= 2:
                    self.switch_to_residential(f"{self.dc_fail_count} DC failures")
                    switched = self.cur == "residential"
            if switched:
                budget = att + 1 + self.max_attempts   # residential gets a full retry budget
                time.sleep(1 + random.random())
            else:
                time.sleep(2 * 2 ** min(att, 3) + random.random())
                if att >= 1:
                    self.new_session()
            att += 1
        raise GTError(f"gave up after {att} attempts on {kws} {geo} {tm}: {errs}")

# ---------------------------------------------------------------- time ranges
def parse_range(time_range: str, today: dt.date | None = None):
    """Return (start, end) dates for presets or 'YYYY-MM-DD YYYY-MM-DD'."""
    today = today or dt.datetime.utcnow().date()
    tr = (time_range or "today 12-m").strip()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})", tr)
    if m:
        s, e = dt.date.fromisoformat(m[1]), dt.date.fromisoformat(m[2])
    elif tr == "all":
        s, e = EARLIEST, today
    else:
        m = re.fullmatch(r"today\s+(\d+)-([dmy])", tr)
        if not m:
            raise GTError(f"unsupported timeRange '{tr}'")
        n, u = int(m[1]), m[2]
        days = {"d": n, "m": round(n * 30.437), "y": round(n * 365.25)}[u]
        s, e = today - dt.timedelta(days=days), today
    s = max(s, EARLIEST)
    e = min(e, today)
    if s >= e:
        raise GTError("timeRange start must be before end")
    return s, e

GOOGLE_PRESETS = {"today 1-m", "today 3-m", "today 12-m", "today 5-y", "all"}

def google_time(time_range, s, e):
    tr = " ".join((time_range or "").split())
    if tr in GOOGLE_PRESETS:
        return tr
    return f"{s.isoformat()} {e.isoformat()}"

def daily_windows(s: dt.date, e: dt.date, size=DAILY_MAX_DAYS, overlap=OVERLAP_DAYS):
    """Newest first. Each window <= size days, consecutive windows overlap by `overlap` days."""
    wins = []
    end = e
    while True:
        start = end - dt.timedelta(days=size - 1)
        if start <= s:
            wins.append((s, end))
            break
        wins.append((start, end))
        end = start + dt.timedelta(days=overlap - 1)
    return wins

# ---------------------------------------------------------------- scale maths
def _sum(v):
    return float(sum(v))

def rows_to_terms(rows, kws):
    """-> (times[], {kw: values[]}, partial_times set)."""
    times = [r["t"] for r in rows]
    vals = {k: [r["v"][i] for r in rows] for i, k in enumerate(kws)}
    partial = {r["t"] for r in rows if r["partial"]}
    return times, vals, partial

def chunks(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]

class Scaler:
    """Puts any number of keywords on one scale for a single geo + Google time string."""

    def __init__(self, client: Client, geo: str, cat=0, prop="", log=print):
        self.c, self.geo, self.cat, self.prop, self.log = client, geo, cat, prop, log
        self.series_calls = 0

    def fetch(self, kws, tm):
        rows, meta = self.c.series(kws, self.geo, tm, self.cat, self.prop)
        self.series_calls += 1
        return rows, meta

    def anchor_pass(self, kws, tm, anchor):
        """Single-anchor stitch ([A,k1..k4] batches). Returns times, values, partial, qa."""
        others = [k for k in kws if k != anchor]
        batches = chunks(others, 4) or [[]]
        out, ref, times, partial, qa = {}, None, None, set(), []
        for b in batches:
            terms = [anchor] + b
            rows, meta = self.fetch(terms, tm)
            t, v, p = rows_to_terms(rows, terms)
            if times is None:
                times, partial, ref = t, p, v[anchor]
            if t != times:
                raise GTError("time axis mismatch between batches (Google changed resolution?)")
            sa = _sum(v[anchor])
            if ref is None or sa <= 0:
                # anchor dead in this batch: other terms dwarf it. Rank them above everything.
                f = 1e6 if _sum(ref) > 0 else 1.0
                flag = "anchor_zero_in_batch"
            else:
                f, flag = _sum(ref) / sa, None
            for k in b:
                out[k] = [x * f for x in v[k]]
            out.setdefault(anchor, list(ref))
            qa.append({"batch": terms, "factor": round(f, 5), "flag": flag, "attempts": meta["attempts"]})
        return times, out, partial, qa

    @staticmethod
    def build_tiers(ranked, prerank_sums, max_ratio=10.0):
        """Size-based tiers, big->small, <=5 terms per request. Consecutive tiers share exactly one link term.
        A tier only admits terms within `max_ratio` of its top term, so the link is never integer-rounded
        noise in the previous request (Day-2 fix for very skewed sets, e.g. bitcoin vs polkadot)."""
        sums = {k: float(prerank_sums.get(k, 0) or 0) for k in ranked}
        nz = [k for k in ranked if sums[k] > 0]
        z = [k for k in ranked if sums[k] <= 0]
        tiers, cur = [], []
        for k in nz:
            if not cur:
                cur = [k]
            elif len(cur) < 5 and sums[k] * max_ratio >= sums[cur[0]]:
                cur.append(k)
            else:
                tiers.append(cur)
                cur = [cur[-1], k]
        for k in z:   # unrankable (zero in pre-rank): pack with the smallest non-zero link
            if not cur:
                cur = [k]
            elif len(cur) < 5:
                cur.append(k)
            else:
                tiers.append(cur)
                link = ([x for x in cur if sums[x] > 0] or cur)[-1]
                cur = [link, k]
        if cur:
            tiers.append(cur)
        return tiers

    def tier_pass(self, tiers, tm, times_ref=None):
        out, times, partial, qa = {}, times_ref, set(), []
        prev_raw = None
        for idx, terms in enumerate(tiers):
            rows, meta = self.fetch(terms, tm)
            t, v, p = rows_to_terms(rows, terms)
            if times is None:
                times = t
            if t != times:
                raise GTError("time axis mismatch between tiers")
            partial |= p
            if idx == 0:
                for k in terms:
                    out[k] = v[k]
                qa.append({"tier": terms, "link": None, "factor": 1.0, "attempts": meta["attempts"]})
            else:
                link = terms[0]
                a, b = _sum(out[link]), _sum(v[link])
                f = a / b if b > 0 else 0.0
                link_mean_prev = st.mean(prev_raw[link])
                for k in terms[1:]:
                    out[k] = [x * f for x in v[k]]
                qa.append({"tier": terms, "link": link, "factor": round(f, 6), "attempts": meta["attempts"],
                           "link_mean_in_prev_request": round(link_mean_prev, 2),
                           "flag": ("weak_link" if link_mean_prev < 3 else None) if b > 0 else "dead_link"})
            prev_raw = v
        return times, out, partial, qa

    def compare(self, kws, tm, anchor=None, mode="accurate", tiers=None, max_ratio=10.0):
        """-> dict(times, values{kw:[...]} unnormalised, partial, qa, tiers)."""
        kws = list(dict.fromkeys(kws))
        if len(kws) <= 5:
            rows, meta = self.fetch(kws, tm)
            t, v, p = rows_to_terms(rows, kws)
            return {"times": t, "values": v, "partial": p, "tiers": [kws],
                    "qa": {"method": "single_request", "attempts": meta["attempts"]}}
        if tiers is None:
            anchor = anchor if anchor in kws else kws[0]
            t, v, p, aqa = self.anchor_pass(kws, tm, anchor)
            if mode == "fast":
                return {"times": t, "values": v, "partial": p, "tiers": None,
                        "qa": {"method": "single_anchor", "anchor": anchor, "batches": aqa}}
            sums = {k: _sum(v[k]) for k in kws}
            ranked = sorted(kws, key=lambda k: -sums[k])
            tiers = self.build_tiers(ranked, sums, max_ratio)
            pre = {"anchor": anchor, "batches": aqa, "prerank_values": v, "prerank_times": t}
        else:
            pre = None
        t, v, p, tqa = self.tier_pass(tiers, tm)
        qa = {"method": "tiered_chain", "tiers": tqa}
        if pre:
            qa["prerank"] = {"anchor": pre["anchor"], "batches": pre["batches"]}
            # consistency: tiered vs pre-rank relative means (same time axis)
            if pre["prerank_times"] == t:
                m_t = max(max(x) for x in v.values()) or 1
                m_p = max(max(x) for x in pre["prerank_values"].values()) or 1
                dev = {k: round(st.mean(v[k]) / m_t - st.mean(pre["prerank_values"][k]) / m_p, 3) for k in kws}
                qa["prerank_vs_tiered_mean_delta_pts"] = {k: round(d * 100, 2) for k, d in dev.items()}
        return {"times": t, "values": v, "partial": p, "tiers": tiers, "qa": qa,
                "prerank": pre}

def normalise(values: dict, ndigits=3):
    m = max((max(v) for v in values.values() if v), default=0) or 1.0
    return {k: [round(x * 100 / m, ndigits) for x in v] for k, v in values.items()}

def _corr(a, b):
    if len(a) < 3:
        return None
    ma, mb = st.mean(a), st.mean(b)
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)

def day(ts):
    return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date()

def stitch_daily(windows_data, kws, coarse=None):
    """windows_data newest-first: [{"start","end","times","values"}]. Returns (by_date{kw:{date:v}}, qa)."""
    merged = {k: {} for k in kws}
    partial = set()
    qa = []
    for i, w in enumerate(windows_data):
        dates = [day(t) for t in w["times"]]
        partial |= {day(t) for t in w.get("partial", ())}
        wv = {k: dict(zip(dates, w["values"][k])) for k in kws}
        if i == 0:
            for k in kws:
                merged[k].update(wv[k])
            qa.append({"window": [str(w["start"]), str(w["end"])], "days": len(dates), "factor": 1.0, "method": "reference"})
            continue
        ov = sorted(set(dates) & set(merged[kws[0]]))
        a = sum(merged[k][d] for k in kws for d in ov)
        b = sum(wv[k][d] for k in kws for d in ov)
        method, f, ovr = "overlap", None, None
        if len(ov) >= MIN_OVERLAP and a > 0 and b > 0:
            f = a / b
            ovr = _corr([merged[k][d] for k in kws for d in ov], [wv[k][d] * f for k in kws for d in ov])
        elif coarse:
            f = _coarse_factor(wv, coarse, kws)
            method = "coarse_fallback"
        if not f:
            f, method = 1.0, "unscaled(no_signal)"
        for k in kws:
            for d, v in wv[k].items():
                if d not in merged[k]:
                    merged[k][d] = v * f
        qa.append({"window": [str(w["start"]), str(w["end"])], "days": len(dates), "overlap_days": len(ov),
                   "factor": round(f, 5), "method": method,
                   "overlap_corr": None if ovr is None else round(ovr, 4)})
    return merged, partial, qa

def _bucket_means(daily: dict, coarse_dates):
    out = []
    for i, d0 in enumerate(coarse_dates):
        d1 = coarse_dates[i + 1] if i + 1 < len(coarse_dates) else d0 + (d0 - coarse_dates[i - 1] if i else dt.timedelta(days=7))
        vals = [v for d, v in daily.items() if d0 <= d < d1]
        exp = (d1 - d0).days
        out.append(st.mean(vals) if vals and len(vals) >= 0.8 * exp else None)
    return out

def _coarse_factor(wv, coarse, kws):
    cd = [day(t) for t in coarse["times"]]
    num = den = 0.0
    for k in kws:
        bm = _bucket_means(wv[k], cd)
        for x, y in zip(bm, coarse["values"][k]):
            if x is not None:
                num += x * y; den += x * x
    return num / den if den > 0 else None

def coarse_qa(merged, coarse, kws):
    """Correlation of stitched daily (bucketed) vs Google's own coarse series, per term."""
    cd = [day(t) for t in coarse["times"]]
    out = {}
    for k in kws:
        bm = _bucket_means(merged[k], cd)
        pairs = [(x, y) for x, y in zip(bm, coarse["values"][k]) if x is not None]
        c = _corr([p[0] for p in pairs], [p[1] for p in pairs]) if pairs else None
        cm = st.mean(p[1] for p in pairs) if pairs else 0
        out[k] = {"buckets": len(pairs), "corr": None if c is None else round(c, 4), "coarseMean": round(cm, 2),
                  "note": "coarse reference is integer-rounded; corr unreliable when coarseMean < 5" if cm < 5 else None}
    return out
