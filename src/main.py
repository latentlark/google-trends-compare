"""LatentLark Actor: Google Trends consistent-scale compare (unlimited keywords, stitched daily history).

Pay per event (keys must match Console pricing / pricing.json):
  apify-actor-start   - charged by the platform (per GB of memory, min 1)
  keyword-series      - one keyword x one country delivered on the shared 0-100 scale
  daily-history-year  - per keyword x country x started year (365.25 d, 1% tolerance) of STITCHED daily data
                        (only when resolution=daily-stitched needs >1 window, i.e. range > 240 days)
Failed countries are free. The job is trimmed up-front to fit the user's max-charge limit.
"""
import asyncio, json, math
from apify import Actor
from .gtcore import GTError, _redact, parse_range, daily_windows
from .job import run_job, _clean_keywords

EV_SERIES = "keyword-series"
EV_YEAR = "daily-history-year"
START_PRICE_FALLBACK = 0.005


def _years_per_series(inp) -> int:
    if (inp.get("resolution") or "auto") != "daily-stitched":
        return 0
    s, e = parse_range(inp.get("timeRange") or "today 12-m")
    if len(daily_windows(s, e)) <= 1:
        return 0
    return _years((e - s).days + 1)


def _years(days: int) -> int:
    """Started years of daily data; 1% tolerance so 'today 5-y' (1827 days) is 5, not 6."""
    return max(1, math.ceil(days / 365.25 - 0.01))


class Billing:
    def __init__(self):
        self.cm = Actor.get_charging_manager()
        self.debug = None

    def price(self, event):
        try:
            info = self.cm.get_pricing_info()
            return float((getattr(info, "per_event_prices", None) or {}).get(event) or 0)
        except Exception:
            return 0.0

    def remaining(self):
        try:
            info = self.cm.get_pricing_info()
            mx = getattr(info, "max_total_charge_usd", None)
            if mx is None or math.isinf(float(mx)):
                return None
            charged = float(self.cm.calculate_total_charged_amount())
            # The platform's synthetic apify-actor-start event is not always in the SDK's local totals/prices;
            # reserve it explicitly (one event per started GB of memory) so we never exceed the user's limit.
            start = self.price("apify-actor-start") or START_PRICE_FALLBACK
            start_total = start * max(1, math.ceil(float(Actor.configuration.memory_mbytes or 256) / 1024))
            self.debug = {"maxTotalChargeUsd": float(mx), "sdkCharged": charged, "startReserved": start_total}
            if charged + 1e-9 < start_total:
                charged = start_total
            return float(mx) - charged
        except Exception:
            return None

    async def charge(self, event, count):
        if count > 0:
            await self.cm.charge(event, count=count)


def _fit_budget(inp, bill, log):
    """Trim countries (then keywords) so the job never exceeds the user's max charge. Returns (inp, note)."""
    kws = _clean_keywords(inp.get("keywords"))
    geos = list(inp.get("geos") or ([inp["geo"]] if inp.get("geo") is not None else ["US"]))
    years = _years_per_series(inp)
    unit = bill.price(EV_SERIES) + years * bill.price(EV_YEAR)
    budget = bill.remaining()
    if budget is None or unit <= 0 or not kws:
        return inp, None
    max_series = int((budget + 1e-9) // unit)
    try:
        cap = bill.cm.calculate_max_event_charge_count_within_limit(EV_SERIES)
        if cap is not None and not math.isinf(float(cap)):
            max_series = min(max_series, int(cap))
    except Exception:
        pass
    need = len(kws) * len(geos)
    if max_series >= need:
        return inp, None
    if max_series < 1:
        raise GTError(f"Max cost per run is too low: one keyword series costs ${unit:.4f}. Raise the limit and retry.")
    if max_series >= len(kws):
        geos = geos[: max_series // len(kws)]
    else:
        geos, kws = geos[:1], kws[:max_series]
    note = (f"Trimmed to {len(kws)} keywords x {len(geos)} {'country' if len(geos) == 1 else 'countries'} "
            "to stay within your max cost per run.")
    log(note)
    return dict(inp, keywords=kws, geos=geos), note


async def main():
    async with Actor:
        inp = await Actor.get_input() or {}

        def log(m):
            Actor.log.info(_redact(m))

        bill = Billing()
        try:
            inp, note = _fit_budget(inp, bill, log)
            rows, qa = await asyncio.to_thread(run_job, inp, log)
        except Exception as e:  # noqa: BLE001 - end SUCCEEDED with one free explanatory row, never an empty FAILED run
            await _soft_fail(str(e) if isinstance(e, GTError) else f"Unexpected error: {type(e).__name__}: {e}")
            return
        if rows:
            for i in range(0, len(rows), 5000):
                await Actor.push_data(rows[i:i + 5000])
        n_kw = len(qa["keywords"])
        ok = [g for g, q in qa["geos"].items() if q.get("ok")]
        bad = [g for g, q in qa["geos"].items() if not q.get("ok")]
        series = n_kw * len(ok)
        years = sum(n_kw * _years(q["points"]) for q in qa["geos"].values()
                    if q.get("ok") and q.get("method") == "daily_stitched" and len(q.get("windows") or []) > 1)
        await bill.charge(EV_SERIES, series)
        await bill.charge(EV_YEAR, years)
        qa["billing"] = {EV_SERIES: series, EV_YEAR: years, "failedCountriesFree": bad, "budgetNote": note, "budget": bill.debug}
        await Actor.set_value("QA", json.loads(json.dumps(qa, default=str)))
        net = qa["network"]
        msg = (f"{len(rows)} rows: {n_kw} keywords on one scale x {len(ok)} {'country' if len(ok) == 1 else 'countries'}"
               + (f" ({len(bad)} failed, not charged: {', '.join(bad)})" if bad else "")
               + f"; {net['httpRequests']} Google requests, proxy={net['proxyFinal']}"
               + (f". {note}" if note else ""))
        if ok:
            msg += ". Did this help? A rating helps others find it."
        log(msg)
        await Actor.set_status_message(_redact(msg))
        if not ok:
            await _soft_fail("all countries failed: " + "; ".join(qa["geos"][g]["error"] for g in bad))


async def _soft_fail(msg):
    """Nothing usable: one free error row + terminal status, run ends SUCCEEDED (nothing charged beyond start)."""
    msg = _redact(msg)[:900]
    Actor.log.warning(msg)
    await Actor.push_data({"rowType": "error", "error": msg,
                           "hint": "Not charged. Retry later, use proxyMode auto/residential, or raise pacingSecs."})
    await Actor.set_status_message(f"Nothing returned - {msg}"[:900], is_terminal=True)
