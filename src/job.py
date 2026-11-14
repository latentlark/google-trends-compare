"""Job orchestration (no Apify SDK) -> dataset rows + QA. Testable on the box with proxyMode=none."""
from __future__ import annotations
import datetime as dt, statistics as st, time
from .gtcore import (Client, GTError, Scaler, daily_windows, google_time, parse_range, normalise,
                     stitch_daily, coarse_qa, day)

SMALL_JOB_SERIES = 10          # keyword x country series; larger paid jobs have no soft deadline
SMALL_JOB_SOFT_DEADLINE = 200  # s from job start; one in-flight request (<= 35 s) may finish after it


def _clean_keywords(kws):
    out = []
    for k in kws or []:
        k = " ".join(str(k).split())
        if k and k.lower() not in {x.lower() for x in out}:
            out.append(k)
    return out

def _res_name(times):
    if len(times) < 2:
        return "unknown"
    d = (times[1] - times[0]) / 86400
    return "hourly" if d < 1 else "daily" if d < 2 else "weekly" if d < 8 else "monthly"

def _rows(values_by_kw_time, geo, time_range, resolution, partial_dates, scale_id, tiers=None, wide=False):
    if wide:   # one row per date, all keywords as columns -> N x fewer dataset writes
        by_date = {}
        for k, series in values_by_kw_time.items():
            for d, v in series:
                by_date.setdefault(d, {})[k] = v
        return [{"geo": geo or "WORLDWIDE", "date": d.isoformat(), "resolution": resolution,
                 "isPartial": d in partial_dates, "timeRange": time_range, "scaleId": scale_id, "values": vals}
                for d, vals in sorted(by_date.items())]
    rows = []
    tier_of = {}
    for i, t in enumerate(tiers or []):
        for k in t:
            tier_of.setdefault(k, i + 1)
    for k, series in values_by_kw_time.items():
        for d, v in series:
            rows.append({"keyword": k, "geo": geo or "WORLDWIDE", "date": d.isoformat(), "value": v,
                         "resolution": resolution, "isPartial": d in partial_dates, "timeRange": time_range,
                         "scaleId": scale_id, "tier": tier_of.get(k)})
    return rows

def _summary(values_by_kw, latest_n=4):
    out = {}
    tot = sum(sum(v) for v in values_by_kw.values()) or 1
    for k, v in values_by_kw.items():
        out[k] = {"mean": round(st.mean(v), 3), "max": round(max(v), 3),
                  f"meanLast{latest_n}": round(st.mean(v[-latest_n:]), 3), "shareOfTotal": round(sum(v) / tot, 4)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["mean"]))

def _verify(scaler, kws, tm, norm, times, tiers):
    """Extra real 5-term Google request across tiers -> MAE vs stitched (renormalised on those 5)."""
    flat = [k for t in tiers for k in t]
    order = list(dict.fromkeys(flat))
    if len(order) <= 5:
        return None
    idx = sorted({round(i * (len(order) - 1) / 4) for i in range(5)})
    pick = [order[i] for i in idx]
    rows, _ = scaler.fetch(pick, tm)
    t = [r["t"] for r in rows]
    if t != times:
        return {"terms": pick, "error": "time axis mismatch"}
    truth = {k: [r["v"][i] for r in rows] for i, k in enumerate(pick)}
    sub = normalise({k: norm[k] for k in pick}, 6)
    e = [abs(sub[k][i] - truth[k][i]) for k in pick for i in range(len(t))]
    rel = {k: (round(st.mean(sub[k]) / st.mean(truth[k]) - 1, 4) if st.mean(truth[k]) >= 1 else None) for k in pick}
    return {"terms": pick, "MAE_pts": round(st.mean(e), 3), "maxAbsErr_pts": round(max(e), 2),
            "meanRelErrByTerm": rel}

def run_job(inp: dict, log=print):
    kws = _clean_keywords(inp.get("keywords"))
    if not kws:
        raise GTError("Provide at least one keyword")
    geos = inp.get("geos") or ([inp["geo"]] if inp.get("geo") is not None else ["US"])
    geos = [("" if str(g).upper() in ("", "WORLDWIDE", "GLOBAL") else str(g).upper()) for g in geos]
    time_range = inp.get("timeRange") or "today 12-m"
    resolution = inp.get("resolution") or "auto"
    scale_mode = inp.get("scaleMode") or "accurate"
    cat = int(inp.get("category") or 0)
    prop = inp.get("property") or ""
    anchor = inp.get("anchorKeyword")
    ratio = float(inp.get("tierMaxRatio") or 10)
    wide = (inp.get("outputFormat") or "long") == "wide"
    client = Client(mode=inp.get("proxyMode") or "auto", dc_groups=inp.get("datacenterGroups"),
                    country=inp.get("residentialCountry"), pace=inp.get("pacingSecs") or 1.5,
                    max_attempts=inp.get("maxAttempts") or 5, fallback_after_429=inp.get("fallbackAfterFailures") or 6,
                    log=log, debug_fail_dc=bool(inp.get("_debugFailDatacenter")))
    t0 = time.time()
    if len(kws) * len(geos) <= SMALL_JOB_SERIES and resolution != "daily-stitched":
        client.deadline = t0 + SMALL_JOB_SOFT_DEADLINE   # small jobs (incl. the Store prefill) end well inside 5 min
    all_rows, qa = [], {"keywords": kws, "geos": {}, "input": {k: v for k, v in inp.items() if k != "keywords"}}
    for geo in geos:
        scaler = Scaler(client, geo, cat, prop, log)
        g = geo or "WORLDWIDE"
        gq = {}
        try:
            s, e = parse_range(time_range)
            wins = daily_windows(s, e) if resolution == "daily-stitched" else None
            scale_id = f"{g}|{time_range}|{resolution}"
            if not wins or len(wins) == 1:
                tm = google_time(time_range, s, e) if not wins else f"{s} {e}"
                res = scaler.compare(kws, tm, anchor, scale_mode, max_ratio=ratio)
                norm = normalise(res["values"])
                dates = [day(t) for t in res["times"]]
                rname = _res_name(res["times"])
                pdates = {day(t) for t in res["partial"]}
                all_rows += _rows({k: list(zip(dates, v)) for k, v in norm.items()}, geo, time_range, rname,
                                  pdates, scale_id, res["tiers"], wide)
                gq.update({"method": res["qa"]["method"], "resolution": rname, "points": len(dates),
                           "googleTime": tm, "scaleQa": res["qa"], "summary": _summary(norm)})
                if inp.get("verifySample") and res["tiers"] and len(kws) > 5:
                    gq["verifySample"] = _verify(scaler, kws, tm, norm, res["times"], res["tiers"])
            else:
                full_tm = google_time(time_range, s, e)
                log(f"[{g}] daily-stitched: {len(wins)} windows x {len(kws)} keywords")
                coarse = scaler.compare(kws, full_tm, anchor, "fast" if len(kws) > 5 else "accurate")
                if len(kws) > 5:
                    sums = {k: sum(coarse["values"][k]) for k in kws}
                    ranked = sorted(kws, key=lambda k: -sums[k])
                    tiers = Scaler.build_tiers(ranked, sums, ratio)
                else:
                    tiers = [kws]
                wdata = []
                for (ws, we) in wins:
                    r = scaler.compare(kws, f"{ws} {we}", tiers=tiers) if len(kws) > 5 else scaler.compare(kws, f"{ws} {we}")
                    if _res_name(r["times"]) != "daily":
                        raise GTError(f"window {ws}..{we} did not return daily data")
                    wdata.append({"start": ws, "end": we, "times": r["times"], "values": r["values"],
                                  "partial": r["partial"], "qa": r["qa"]})
                    log(f"[{g}] window {ws}..{we} ok")
                merged, partial, sqa = stitch_daily(wdata, kws, coarse)
                flat = {k: [merged[k][d] for d in sorted(merged[k])] for k in kws}
                norm = normalise(flat)
                dates = sorted(merged[kws[0]])
                dates = [d for d in dates if s <= d <= e]
                start_i = sorted(merged[kws[0]]).index(dates[0])
                ser = {k: list(zip(dates, norm[k][start_i:start_i + len(dates)])) for k in kws}
                all_rows += _rows(ser, geo, time_range, "daily", partial, scale_id, tiers, wide)
                normd = {k: dict(v) for k, v in ser.items()}
                gq.update({"method": "daily_stitched", "resolution": "daily", "points": len(dates),
                           "span": [str(dates[0]), str(dates[-1])], "windows": sqa, "tiers": tiers,
                           "coarseResolution": _res_name(coarse["times"]),
                           "dailyVsGoogleCoarseCorr": coarse_qa(normd, coarse, kws),
                           "windowScaleQa": [w["qa"] for w in wdata],
                           "summary": _summary({k: [v for _, v in ser[k]] for k in kws})})
            gq["seriesRequests"] = scaler.series_calls
            gq["ok"] = True
        except GTError as ex:
            gq.update({"ok": False, "error": str(ex), "seriesRequests": scaler.series_calls})
            log(f"[{g}] FAILED: {ex}")
        qa["geos"][g] = gq
    codes = {}
    for r in client.reqlog:
        key = f"{r['proxy']}:{r['k']}:{r['code']}"
        codes[key] = codes.get(key, 0) + 1
    qa["network"] = {"httpRequests": len(client.reqlog), "codes": codes, "approxBytes": client.bytes,
                     "proxyModeRequested": client.mode, "proxyFinal": client.cur,
                     "fallback": client.switched_at, "secs": round(time.time() - t0, 1),
                     "captchas": sum(1 for r in client.reqlog if r["code"] == "CAPTCHA"),
                     "boots": sum(1 for r in client.reqlog if r["k"] == "boot"),
                     "residentialBoots": sum(1 for r in client.reqlog if r["k"] == "boot" and r["proxy"] == "residential"),
                     "bootMethod": "HEAD (cookie only, 0-byte body; HTML boot page never fetched)"}
    return all_rows, qa
