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

# ---------- split-decimal weight parsing ----------
# Sellers break the decimal constantly: European commas and stray spaces after the
# point. Left unjoined, only the fractional part matches and the weight inflates --
# a real listing titled "12. 23 grams" priced ABOVE melt was scored as a $550 profit.
_G = [("14k gold ring 13,25g",                     13.25,  "european comma"),
      ("10k chain 5,5 dwt",                         8.55,  "comma + dwt"),
      ("14k gold ring 13.25g",                     13.25,  "plain decimal unaffected"),
      ("clip earrings 12. 23 grams",               12.23,  "space AFTER the point"),
      ("14k chain 8 .5g",                            8.5,  "space BEFORE the point"),
      ("14k chain 8 . 5 grams",                      8.5,  "spaces both sides"),
      ("14k 6,75 g pendant",                        6.75,  "comma + spaced unit"),
      ("14k gold 12.235 grams",                   12.235,  "3-decimal untouched"),
      ("bulk 1,500 grams scrap",                  1500.0,  "thousands separator kept whole"),
      ("scrap lot 1,234.56 grams",               1234.56,  "thousands + decimal"),
      # guards: things that must NOT be glued together
      ("lot of 12 rings 23 grams",                  23.0,  "bare space is not a decimal"),
      ("Price $1,225. 23 grams total",              23.0,  "price digits never glue to weight"),
      ("sold 3 items, 45 g total",                  45.0,  "comma after a word is not a decimal")]
for _t, _exp, _why in _G:
    _got = gs.extract_grams(_t)
    check(f"grams: {_why}", _got is not None and abs(_got - _exp) < 0.011,
          f"got {_got} want {_exp} from {_t!r}")
check("gold-wt: comma decimal in gold-specific weight", gs.extract_gold_grams("8,5g of gold content") == 8.5)
check("gold-wt: spaced decimal in gold-specific weight",
      gs.extract_gold_grams("total 40g, gold weight: 12. 23 grams") == 12.23)

# ---------- watch exclusion ----------
check("watch: 'mens watch' excluded even if titled solid gold",
      not gs.is_solid_no_stones("14k solid gold mens watch 45g"))
check("watch: 'chronograph' excluded", not gs.is_solid_no_stones("18k gold chronograph 60g"))
check("watch: 'movement' excluded", not gs.is_solid_no_stones("vintage gold watch movement parts"))
check("watch: plain chain still passes", gs.is_solid_no_stones("14k solid gold rope chain 10g"))
check("watch: bracelet still passes (not a watch band)", gs.is_solid_no_stones("14k solid gold cuban bracelet 20g"))

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

# ---------- manual query overrides: pin / disable ----------
cfgP = copy.deepcopy(cfg2)
cfgP["pinned_queries"]  = ["core1"]          # core1 was auto-retired above
cfgP["disabled_queries"] = ["core0"]         # core0 is the star performer
statsP = copy.deepcopy(stats)
coreP, exploreP, _ = gs.select_queries(cfgP, statsP, {})
check("pin: retired query resurrected by pinning", "core1" in coreP)
check("disable: top performer dropped despite best value", "core0" not in coreP)
gs.update_query_stats(cfgP, statsP, coreP + exploreP, [])
check("pin: status cleared of 'retired'", statsP["queries"]["core1"].get("status") != "retired")
check("pin: flagged in stats", statsP["queries"]["core1"].get("pinned") is True)
check("disable: marked disabled in stats", statsP["queries"]["core0"].get("status") == "disabled")

# a pinned query must survive the retirement sweep no matter how badly it does
statsP["queries"]["core1"].update({"runs": 999, "deals": 0, "traps": 0})
_, retiredP = gs.update_query_stats(cfgP, statsP, ["core1"], [])
check("pin: immune to auto-retirement", "core1" not in retiredP
      and statsP["queries"]["core1"].get("status") != "retired")

# disabled queries must never be re-offered for exploration either
cfgX = copy.deepcopy(cfg2)
cfgX["disabled_queries"] = [gs.explore_candidates(cfgX, stats)[0]]
check("disable: excluded from explore pool",
      cfgX["disabled_queries"][0] not in gs.explore_candidates(cfgX, stats))

# re-enabling clears the disabled status rather than stranding it
cfgR = copy.deepcopy(cfgP); cfgR["disabled_queries"] = []
gs.update_query_stats(cfgR, statsP, ["core0"], [])
check("disable: re-enabling clears status", statsP["queries"]["core0"].get("status") != "disabled")

# pinned queries are seated first, so pinning more than the slot count can't starve the run
cfgF = copy.deepcopy(cfg2); cfgF["pinned_queries"] = cfgF["queries"][:]
coreF, exploreF, sortsF = gs.select_queries(cfgF, copy.deepcopy(stats), {})
estF = (len(coreF) + len(exploreF)) * len(sortsF) + cfgF["max_detail_calls"]
check("pin: over-pinning still respects the call budget", estF <= 4500/48 + 1, f"{estF}")

# sort alternation flips with counter
s_even = gs.sorts_for_run(cfg2, 0); s_odd = gs.sorts_for_run(cfg2, 1)
check("alternate: sorts flip between runs", s_even != s_odd)

# ---------- authenticity guarantee: a tag, never a filter ----------
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

cfgAG = copy.deepcopy(gs.CONFIG)
r_ag, r_plain = gs.evaluate(ag_item, 132.0, cfgAG), gs.evaluate(plain_item, 132.0, cfgAG)
check("ag: guaranteed item tagged true", r_ag is not None and r_ag["auth_guaranteed"] is True)
check("ag: non-guaranteed item still surfaces", r_plain is not None)
check("ag: non-guaranteed item tagged false", r_plain is not None and r_plain["auth_guaranteed"] is False)

# REGRESSION: the eBay query must stay unrestricted. Adding qualifiedPrograms without
# deliveryCountry + deliveryPostalCode returns HTTP 200 with zero items — a silent
# outage that once took the scanner down for ~19h. Filtering happens in the UI now.
import unittest.mock as mock
resp = mock.Mock(status_code=200); resp.json.return_value = {"itemSummaries": []}
with mock.patch.object(gs.requests, "get", return_value=resp) as mget:
    gs.search("tok", "14k gold ring grams", 50, cfg=cfgAG)
    filt = mget.call_args.kwargs["params"]["filter"]
check("search: no qualifiedPrograms in the eBay query", "qualifiedPrograms" not in filt)
check("search: no deliveryPostalCode dependency", "deliveryPostalCode" not in filt)

# ---------- mixed lots: a tag, never a filter ----------
mixed_item = {"price":{"value":"500"},"seller":{"feedbackPercentage":"99.5","feedbackScore":"800"},
              "title":"14k and 10k gold lot 20 grams","itemId":"mx1","itemWebUrl":"u","buyingOptions":[]}
r_mixed = gs.evaluate(mixed_item, 132.0, copy.deepcopy(gs.CONFIG))
check("mixed: lot still surfaces", r_mixed is not None)
check("mixed: tagged as mixed", r_mixed is not None and r_mixed["mixed_lot"] is True)
check("mixed: priced at the lowest karat floor", r_mixed is not None and r_mixed["karat"] == "10K")
check("mixed: carries an explanatory note", r_mixed is not None and "floor" in (r_mixed["mixed_note"] or ""))

# ---------- per-search hit breakdown feeds the bar chart ----------
cfgB = copy.deepcopy(gs.CONFIG); cfgB["query_stats_file"] = "_test_qs2.json"
statsB = {"meta": {"run_counter": 0}, "queries": {}}
gs.update_query_stats(cfgB, statsB, ["qb"], [
    {"query":"qb","trap":False,"score":85,"auth_guaranteed":True,"mixed_lot":False},
    {"query":"qb","trap":False,"score":40,"auth_guaranteed":False,"mixed_lot":True},
    {"query":"qb","trap":True, "score":10,"auth_guaranteed":False,"mixed_lot":False}])
_b = statsB["queries"]["qb"]
check("breakdown: strong deals counted", _b.get("strong") == 1)
check("breakdown: weak deals counted", _b.get("weak") == 1)
check("breakdown: traps counted separately", _b.get("traps") == 1)
check("breakdown: strong+weak equals deals", _b.get("strong",0)+_b.get("weak",0) == _b.get("deals"))
check("breakdown: ag counted", _b.get("ag") == 1)
check("breakdown: mixed counted", _b.get("mixed") == 1)

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
