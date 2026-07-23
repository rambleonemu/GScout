#!/usr/bin/env python3
"""Offline tests for GScout's regex fixes + learning/selection engines. No network."""
import json, os, sys, copy, time
import gold_scout as gs

FAIL = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond: FAIL.append(name)

# ---------- regex accuracy ----------
check("stones: plural 'diamonds' caught", not gs.is_solid_no_stones("14k gold ring with diamonds 5g"))
check("stones: plural 'sapphires' caught", not gs.is_solid_no_stones("14k gold earrings sapphires 3g"))
check("stones: 'rubies' irregular plural caught", not gs.is_solid_no_stones("18k ring rubies 4g"))
check("stones: clean solid passes", gs.is_solid_no_stones("14k solid gold rope chain 10g"))
check("stones: 'no stones' negation still passes", gs.is_solid_no_stones("14k gold ring no stones 5g"))
check("stones: diamond-cut (style, not stone) passes", gs.is_solid_no_stones("10k diamond cut rope chain 8g"))

check("karat: '$10k obo' not misread as 10k", gs.karat_from_text("gold chain $10k obo") is None)
check("karat: '14k gold' reads 14", gs.karat_from_text("14k gold chain") == 14)
check("karat: '14 karat' reads 14", gs.karat_from_text("14 karat gold ring") == 14)
check("fineness: price '585.00' not misread", gs.karat_from_text("gold chain 585.00 shipped") is None)
check("fineness: '585 gold' reads 14k", gs.karat_from_text("585 gold bracelet") == 14)
check("mixed: single karat not mixed", gs.karats_in_text("14k gold ring size 10") == {14})

check("grams: '1/2 gram' = 0.5", gs.extract_grams("1/2 gram gold") == 0.5)
check("grams: sane clamp rejects 5000g", gs.extract_grams("necklace 5000 grams") is None)
check("grams: dwt converts", abs(gs.extract_grams("10 dwt scrap") - 15.55) < 0.01)
check("gold-wt: '8.2g of gold' specific", gs.extract_gold_grams("lot with 8.2g of gold") == 8.2)

# ---------- feedback engine ----------
cfg = copy.deepcopy(gs.CONFIG)
now_ms = time.time() * 1000
fb = [
    {"id":"a","query":"q_bad","seller":"shady1","verdict":"bad","category":"plated","ts":now_ms},
    {"id":"b","query":"q_bad","seller":"shady1","verdict":"bad","category":"plated","ts":now_ms},
    {"id":"c","query":"q_good","seller":"nice1","verdict":"good","ts":now_ms},
    {"id":"d","query":"q_good","seller":"nice1","verdict":"good","ts":now_ms},
    {"id":"e","query":"q_style","seller":"nice2","verdict":"bad","category":"style","ts":now_ms},
    {"id":"f","query":"q_old","seller":"old1","verdict":"bad","category":"plated","ts":now_ms - 400*86400*1000},
]
cfg["feedback_file"] = "_test_fb.json"
json.dump(fb, open("_test_fb.json","w"))
qm, sm, block, n = gs.load_feedback(cfg)
check("fb: event count", n == 6)
check("fb: bad query weighted down", qm.get("q_bad", 1) < 1.0, str(qm.get("q_bad")))
check("fb: good query weighted up", qm.get("q_good", 1) > 1.0)
check("fb: 'style' taps never punish", "q_style" not in qm or qm["q_style"] == 1.0, str(qm.get("q_style")))
check("fb: repeat-bad seller blocked", "shady1" in block)
check("fb: good seller not blocked", "nice1" not in block)
check("fb: 400-day-old tap ~fully decayed (no block)", "old1" not in block)
check("fb: multiplier bounded by span", all(1-cfg["fb_weight_span"] <= v <= 1+cfg["fb_weight_span"] for v in list(qm.values())+list(sm.values())))
os.remove("_test_fb.json")

# ---------- trust application ----------
row = {"score": 60, "query": "q_bad", "seller_user": "x", "trap": False}
cfg["_query_mult"], cfg["_seller_mult"] = qm, sm
gs._apply_trust(row, cfg)
check("trust: nudges down, bounded", 51 <= row["score"] < 60, str(row["score"]))
check("trust: base_score preserved", row.get("base_score") == 60)

# ---------- selection engine ----------
cfg2 = copy.deepcopy(gs.CONFIG)
cfg2["queries"] = [f"core{i}" for i in range(60)]
cfg2["daily_call_budget"], cfg2["runs_per_day"] = 4500, 48
cfg2["max_detail_calls"], cfg2["results_per_query"] = 30, 50
cfg2["query_stats_file"], cfg2["history_file"] = "_test_qs.json", "_no_hist.json"
stats = {"meta":{"run_counter":10},"queries":{}}
for i, q in enumerate(cfg2["queries"]):   # steady state: everything has history
    stats["queries"][q] = {"runs":10,"deals":0,"traps":0,"last_rc":10,"origin":"core"}
stats["queries"]["core0"] = {"runs":20,"deals":15,"traps":0,"last_rc":10,"origin":"core"}  # star
stats["queries"]["core1"] = {"runs":25,"deals":0,"traps":0,"last_rc":10,"origin":"core"}   # retire-able
stats["queries"]["core2"] = {"runs":10,"deals":0,"traps":0,"last_rc":2,"origin":"core"}    # most starved
stats["queries"]["14k gold wedding band grams"] = {"runs":4,"deals":3,"traps":0,"origin":"explore"}  # promotable
core, explore, sorts = gs.select_queries(cfg2, stats, {})
budget_per_run = 4500/48
est = (len(core)+len(explore))*len(sorts) + 30
check("budget: est calls fit per-run budget", est <= budget_per_run, f"{est} vs {budget_per_run:.0f}")
check("budget: alternate mode = 1 sort", len(sorts) == 1)
check("select: star query included", "core0" in core)
check("select: starving query jumps queue", "core2" in core)
check("select: explore slots reserved", len(explore) >= 1, str(len(explore)))
check("select: explore ≈ explore_frac of slots", len(explore) <= round((len(core)+len(explore))*0.2)+1)

promoted, retired = gs.update_query_stats(cfg2, stats, core+explore, [])
check("promote: 3-deal explorer promoted", "14k gold wedding band grams" in promoted)
check("retire: 25-run zero-hit query retired", "core1" in retired)

core2b, explore2b, _ = gs.select_queries(cfg2, stats, {})
check("promoted competes as core next run", "14k gold wedding band grams" in core2b)
check("retired excluded next run", "core1" not in core2b)
pool = gs.explore_candidates(cfg2, stats)
check("promoted leaves explore pool", "14k gold wedding band grams" not in pool)
check("explore pool generates from term lists", len(pool) > 10)

# sort alternation flips with counter
s_even = gs.sorts_for_run(cfg2, 0); s_odd = gs.sorts_for_run(cfg2, 1)
check("alternate: sorts flip between runs", s_even != s_odd)

# mixed exclusion toggle
cfg3 = copy.deepcopy(gs.CONFIG); cfg3["exclude_mixed"] = True; cfg3["auth_guarantee_only"] = False
item = {"price":{"value":"500"},"seller":{"feedbackPercentage":"99.5","feedbackScore":"800"},
        "title":"14k and 10k gold lot 20 grams","itemId":"z1","itemWebUrl":"u","buyingOptions":[]}
check("mixed: excluded when toggle on", gs.evaluate(item, 132.0, cfg3) is None)
cfg3["exclude_mixed"] = False
r = gs.evaluate(item, 132.0, cfg3)
check("mixed: allowed + floored when toggle off", r is not None and r["karat"] == "10K")

# ---------- authenticity guarantee filter ----------
ag_item = {"price":{"value":"500"},"seller":{"feedbackPercentage":"99.5","feedbackScore":"800"},
           "title":"14k solid gold rope chain 10 grams","itemId":"ag1","itemWebUrl":"u",
           "buyingOptions":[], "qualifiedPrograms":["AUTHENTICITY_GUARANTEE"]}
plain_item = {"price":{"value":"500"},"seller":{"feedbackPercentage":"99.5","feedbackScore":"800"},
              "title":"14k solid gold rope chain 10 grams","itemId":"pl1","itemWebUrl":"u",
              "buyingOptions":[]}
detail_item = {"price":{"value":"500"},"seller":{"feedbackPercentage":"99.5","feedbackScore":"800"},
               "title":"14k solid gold rope chain 10 grams","itemId":"dt1","itemWebUrl":"u",
               "buyingOptions":[], "authenticityGuarantee":{"program":"ELIGIBLE"}}
check("ag: qualifiedPrograms array detected", gs.auth_guaranteed(ag_item))
check("ag: authenticityGuarantee container detected", gs.auth_guaranteed(detail_item))
check("ag: plain item not flagged", not gs.auth_guaranteed(plain_item))

cfgAG = copy.deepcopy(gs.CONFIG); cfgAG["auth_guarantee_only"] = True
r_ag = gs.evaluate(ag_item, 132.0, cfgAG)
check("ag-only on: AG item still evaluated", r_ag is not None and r_ag["auth_guaranteed"] is True)
check("ag-only on: non-AG item dropped", gs.evaluate(plain_item, 132.0, cfgAG) is None)
cfgAG["auth_guarantee_only"] = False
r_plain = gs.evaluate(plain_item, 132.0, cfgAG)
check("ag-only off: non-AG item now allowed", r_plain is not None and r_plain["auth_guaranteed"] is False)

import unittest.mock as mock
resp = mock.Mock(status_code=200); resp.json.return_value = {"itemSummaries": []}
with mock.patch.object(gs.requests, "get", return_value=resp) as mget:
    gs.search("tok", "14k gold ring grams", 50, cfg={"auth_guarantee_only": True})
    filt_on = mget.call_args.kwargs["params"]["filter"]
    gs.search("tok", "14k gold ring grams", 50, cfg={"auth_guarantee_only": False})
    filt_off = mget.call_args.kwargs["params"]["filter"]
check("ag-only on: eBay filter includes qualifiedPrograms", "qualifiedPrograms:{AUTHENTICITY_GUARANTEE}" in filt_on)
check("ag-only off: eBay filter omits qualifiedPrograms", "qualifiedPrograms" not in filt_off)

# ---------- history metrics + trim behavior ----------
cfgH = copy.deepcopy(gs.CONFIG)
cfgH["history_file"] = "_test_hist.json"
cfgH["history_max"] = 2000
seed = [{"t":f"2026-01-01T00:{i%60:02d}:00+00:00","spot_oz":4000,"deals":1,"traps":0,
         "avg_under":10,"best":50,"profit":5,"by_karat":{},"by_query":{"x":{"deals":1,"traps":0}}}
        for i in range(1600)]
json.dump(seed, open("_test_hist.json","w"))
deals_fake = [{"query":"q1","trap":False,"under_pct":20,"score":80,"profit":100,"karat":"14K"},
              {"query":"q1","trap":False,"under_pct":10,"score":60,"profit":50,"karat":"10K"}]
hist = gs.append_history(cfgH, 4100.0, {"14K":77.0}, deals_fake, [], price_src="test", new_deals=2)
rec = hist[-1]
check("hist: avg_score recorded", rec["avg_score"] == 70.0, str(rec.get("avg_score")))
check("hist: avg_profit recorded", rec["avg_profit"] == 75.0, str(rec.get("avg_profit")))
check("hist: new_deals recorded", rec["new_deals"] == 2)
check("hist: by_query stripped from old records", "by_query" not in hist[0] and "by_query" in hist[-1])
os.remove("_test_hist.json")

# ---------- crash-proofing ----------
cfgX = copy.deepcopy(gs.CONFIG); cfgX["feedback_file"] = "_test_fbx.json"
json.dump([
    {"id":"ok","query":"q1","seller":"s1","verdict":"good","ts":time.time()*1000},
    {"id":"bad_ts","query":"q2","seller":"s2","verdict":"bad","category":"plated","ts":"garbage"},
    "not even a dict", {"verdict":"bad"}, None,
], open("_test_fbx.json","w"))
try:
    qmx, smx, bx, nx = gs.load_feedback(cfgX)
    check("crashproof: malformed feedback events skipped, run survives", "q1" in qmx and "q2" not in qmx)
except Exception as e:
    check("crashproof: malformed feedback events skipped, run survives", False, str(e))
os.remove("_test_fbx.json")

# stale-spot fallback: fresh history record accepted, old one refused
from datetime import datetime, timezone, timedelta
gs.CONFIG["history_file"] = "_test_spot.json"
fresh_t = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
json.dump([{"t": fresh_t, "spot_oz": 4100.0, "price_src": "gold-api.com"}], open("_test_spot.json","w"))
import unittest.mock as mock
with mock.patch.object(gs.requests, "get", side_effect=Exception("net down")):
    p, src = gs.live_spot_per_oz()
    check("failsafe: stale spot used when all APIs down", p == 4100.0 and "stale" in src, f"{p} {src}")
old_t = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
json.dump([{"t": old_t, "spot_oz": 4100.0, "price_src": "gold-api.com"}], open("_test_spot.json","w"))
with mock.patch.object(gs.requests, "get", side_effect=Exception("net down")):
    try:
        gs.live_spot_per_oz(); check("failsafe: too-old spot refused (crash correctly)", False)
    except RuntimeError:
        check("failsafe: too-old spot refused (crash correctly)", True)
os.remove("_test_spot.json")

for f in ("_test_qs.json",):
    if os.path.exists(f): os.remove(f)

print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL))+' FAILURES: '+', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
