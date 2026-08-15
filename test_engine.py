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
    # "irrelevant" is now the reason that means the TERM is at fault. "plated" moved to
    # the filter-defect group, which flags the seller but deliberately leaves the search
    # term alone — the term found solid-gold-looking listings, our filter missed the
    # plating, and retiring the term for that would hide our bug behind a number.
    {"id":"a","query":"q_bad","seller":"shady1","verdict":"bad","category":"irrelevant","ts":now_ms},
    {"id":"b","query":"q_bad","seller":"shady1","verdict":"bad","category":"irrelevant","ts":now_ms},
    {"id":"g","query":"q_defect","seller":"shady1","verdict":"bad","category":"plated","ts":now_ms},
    {"id":"h","query":"q_defect","seller":"shady1","verdict":"bad","category":"plated","ts":now_ms},
    {"id":"c","query":"q_good","seller":"nice1","verdict":"good","ts":now_ms},
    {"id":"d","query":"q_good","seller":"nice1","verdict":"good","ts":now_ms},
    {"id":"e","query":"q_style","seller":"nice2","verdict":"bad","category":"style","ts":now_ms},
    {"id":"f","query":"q_old","seller":"old1","verdict":"bad","category":"plated","ts":now_ms - 400*86400*1000},
]
cfg["feedback_file"] = "_test_fb.json"
json.dump(fb, open("_test_fb.json","w"))
qm, sm, block, n = gs.load_feedback(cfg)
check("fb: event count", n == 8)
check("fb: bad query weighted down", qm.get("q_bad", 1) < 1.0, str(qm.get("q_bad")))
check("fb: good query weighted up", qm.get("q_good", 1) > 1.0)
check("fb: 'style' taps never punish", "q_style" not in qm or qm["q_style"] == 1.0, str(qm.get("q_style")))
check("fb: a filter-defect tap leaves the search term alone",
      "q_defect" not in qm or qm["q_defect"] == 1.0, str(qm.get("q_defect")))
check("fb: a filter-defect tap still flags the seller", sm.get("shady1", 1) < 1.0)
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
stats["queries"]["core1"] = {"runs":25,"deals":0,"traps":0,"last_rc":10,"origin":"explore"}   # retire-able
cfg2["retire_min_live"] = 1      # see note above: guardrail scaled to the fixture
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
_cfgLo = copy.deepcopy(cfg2); _cfgLo["explore_frac"] = 0.05
_cfgHi = copy.deepcopy(cfg2); _cfgHi["explore_frac"] = 0.50
_lo = len(gs.select_queries(_cfgLo, {"meta":{"run_counter":0},"queries":{}}, {})[1])
_hi = len(gs.select_queries(_cfgHi, {"meta":{"run_counter":0},"queries":{}}, {})[1])
check("select: explore volume scales with explore_frac", _hi > _lo, f"{_lo} -> {_hi}")
check("select: explore never exceeds the candidate pool",
      len(explore) <= len(gs.explore_candidates(cfg2, stats)) + len(explore))

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
check("breakdown: strong AG deal bucketed separately", _b.get("strong_ag") == 1)
check("breakdown: weak non-AG deal counted", _b.get("weak") == 1)
check("breakdown: traps counted separately", _b.get("traps") == 1)
check("breakdown: all four deal buckets sum to deals",
      sum(_b.get(k,0) for k in ("strong","strong_ag","weak","weak_ag")) == _b.get("deals"))
check("breakdown: ag total counted", _b.get("ag") == 1)
check("breakdown: mixed counted", _b.get("mixed") == 1)

# ---------- nothing hardcoded: every knob reachable from settings.json ----------
import shutil as _sh
_sh.copy("settings.json", "_test_settings_backup.json")
try:
    _s = json.load(open("settings.json"))
    _s.update({"query_weights": {"trap": -99.0}, "reserve_runs": 5, "strong_score": 88,
               "max_revives": 7, "query_prior_runs": 11})
    json.dump(_s, open("settings.json", "w"))
    _c = copy.deepcopy(gs.CONFIG); gs.load_settings(_c)
    check("config: a single weight can be overridden", _c["query_weights"]["trap"] == -99.0)
    check("config: unlisted weights keep their defaults", _c["query_weights"]["strong_ag"] == 3.0)
    check("config: reserve_runs is editable", _c["reserve_runs"] == 5)
    check("config: strong_score is editable", _c["strong_score"] == 88)
    check("config: max_revives is editable", _c["max_revives"] == 7)
    check("config: query_prior_runs is editable", _c["query_prior_runs"] == 11)
finally:
    _sh.move("_test_settings_backup.json", "settings.json")

# manual-run headroom: scheduled sweeps must not spend the whole daily budget
_ch = copy.deepcopy(gs.CONFIG); _ch["runs_per_day"] = 4
_core, _ex, _sorts = gs.select_queries(_ch, {"meta": {"run_counter": 0}, "queries": {}}, {})
_per_sweep = (len(_core) + len(_ex)) * len(_sorts) + _ch["max_detail_calls"]
_spent = _per_sweep * 4
check("budget: scheduled sweeps leave headroom for manual runs",
      _spent < _ch["daily_call_budget"], f"{_spent}/{_ch['daily_call_budget']}")
check("budget: headroom covers at least reserve_runs manual sweeps",
      (_ch["daily_call_budget"] - _spent) >= _per_sweep * _ch["reserve_runs"],
      f"headroom {_ch['daily_call_budget']-_spent}, need {_per_sweep*_ch['reserve_runs']}")

# ---------- weighted query scoring ----------
cfgW = copy.deepcopy(gs.CONFIG)
_v = lambda **k: gs.query_value(dict({"runs": 10}, **k), cfgW, k.pop("_fb", None))
check("score: traps push a search negative", _v(traps=12) < 0)
check("score: low-score deals are neutral", _v(weak=10) == 0)
check("score: strong deals are positive", _v(strong=10) > 0)
check("score: AG outweighs equivalent non-AG", _v(strong_ag=10) > _v(strong=10))
check("score: weak AG beats weak non-AG", _v(weak_ag=10) > _v(weak=10))
check("score: thumbs up outweighs a strong deal",
      gs.query_value({"runs":10}, cfgW, {"up":1}) > _v(strong=1))
check("score: thumbs down outweighs a trap",
      abs(gs.query_value({"runs":10}, cfgW, {"down":1})) > abs(_v(traps=1)))
check("score: smoothing damps a single lucky run",
      gs.query_value({"runs":1,"strong_ag":1}, cfgW) < gs.query_value({"runs":50,"strong_ag":50}, cfgW))

# --- weight parsing: size next to weight ------------------------------------
# REGRESSION: one space-tolerant rule handled both "12. 23g" (spaced decimal point)
# and "13,25g" (European decimal comma). That made "Size 6, 4 grams" read as 6.4g,
# inventing 2.4g of gold and turning a fairly priced ring into a fake deal.
_wt = [
    ("EUC 14K Dome/Shrimp/ Ring, Brushed & Polished Gold, Marked 14k, Size 6, 4 grams", 4.0),
    ("14k gold ring size 7, 3.2 grams", 3.2),
    ("10k gold ring sz 9, 5 grams", 5.0),
    ("14k gold band Size 6.5, 4.1 grams", 4.1),
    ("14k gold ring size 6 1/2, 3 grams", 3.0),
    ("14k gold ring size 10 5.5g", 5.5),
    ("14k gold chain 13,25g", 13.25),          # real European decimal, no space
    ("14k gold chain 12. 23 grams", 12.23),    # spaced decimal point
    ("14k gold chain 8 . 5g", 8.5),
    ("14k gold scrap lot 1,500 grams", 1500.0),
    ("1/2 gram 14k gold", 0.5),
]
for _t, _exp in _wt:
    check(f"weight: {_t[:44]!r} -> {_exp}g", gs.extract_grams(_t) == _exp,
          f"got {gs.extract_grams(_t)}")
check("weight: comma+space is a separator, never a decimal",
      gs.extract_grams("14k gold ring, 4 grams") == 4.0)
check("weight: gold-specific parser uses the same size rule",
      gs.extract_gold_grams("14k ring size 6, gold weight: 4.2g") == 4.2,
      str(gs.extract_gold_grams("14k ring size 6, gold weight: 4.2g")))

# --- mixed materials on the fast path ---------------------------------------
# REGRESSION: MIXED_METAL was only ever tested against item specifics on deep-scanned
# listings, so the fast path — most rows — never checked for a second metal at all.
for _t in ["Mixed Metal Lot 14k Gold 30 grams", "14k Gold and Sterling Silver Lot 25 grams",
           "Stainless Steel and 14k Gold Bracelet 20g", "base metal 14k gold ring 5g"]:
    check(f"mixed: blocked {_t[:38]!r}", not gs.is_solid_no_stones(_t))
# Tone words are no longer a hard block: "two tone" usually means two colours of gold.
# They're kept and marked instead — see the graded-verdict tests below.
for _t in ["Two Tone 14k Gold Ring 5g", "14k Gold Tri-Tone Chain 8 grams"]:
    check(f"mixed: tone kept as suspect {_t[:32]!r}",
          gs.material_verdict(_t)["state"] == "suspect")
for _t in ["14k Solid Gold Rope Chain 12 grams", "14k White Gold Ring 4 grams",
           "10k Gold Cuban Link 15 grams"]:
    check(f"mixed: allowed {_t[:38]!r}", gs.is_solid_no_stones(_t))
check("mixed: 'silver tone' is a colour, not a second metal",
      not gs.has_mixed_metal("14k gold chain silver tone clasp"))

# a clean title whose BODY reveals a second metal must be caught once we've paid
# for the detail call anyway
_it = {"title": "14k Solid Gold Ring 5 grams"}
check("mixed: description re-check catches a clean title",
      gs.deep_disqualifies(_it, {"description": "14k gold with sterling silver band"})
      is not None)
check("mixed: description re-check catches stones in the body",
      gs.deep_disqualifies(_it, {"description": "set with three small diamonds"})
      is not None)
check("mixed: clean listing survives the re-check",
      gs.deep_disqualifies(_it, {"description": "solid 14k gold, no stones, 5 grams"})
      is None)

# --- graded material verdict: the false-negative fix -------------------------
# A binary filter has to choose which way to be wrong, and choosing "reject" makes the
# error invisible — a bad listing on the board can be flagged, a good one that never
# arrived cannot. These are the cases the blunt filter was silently throwing away.
_mat = [
    # kept outright: two/tri-tone usually means two COLOURS OF GOLD
    ("Two Tone 14k Gold Ring white and yellow gold 5g", "clear"),
    ("14k Yellow Gold Rope Chain with white gold accents 12 grams", "clear"),
    ("14k Solid Gold Rope Chain 12 grams", "clear"),
    # kept but marked: another metal, but only on a small part
    ("14K Gold Chain 20 grams sterling silver clasp", "suspect"),
    ("14k Solid Gold Bracelet 18g steel spring clasp", "suspect"),
    ("14k gold bracelet with sterling silver lobster clasp 18g", "suspect"),
    ("Two Tone 14k Gold Ring 6 grams", "suspect"),
    ("14k Gold Chain with diamond accent 15 grams", "suspect"),
    # still blocked: the second material IS the item
    ("Sterling Silver and 14k Gold Ring 6 grams", "blocked"),
    ("14k gold and silver bracelet 20g", "blocked"),
    ("14k yellow gold with stainless steel band", "blocked"),
    ("Two Tone 14k Gold and Steel Bracelet 20g", "blocked"),
    ("14k Gold Ring set with diamonds 5g", "blocked"),
    ("14K Gold Plated Chain 25 grams", "blocked"),
]
for _t, _exp in _mat:
    _v = gs.material_verdict(_t)
    check(f"material: {_exp:8} {_t[:40]!r}", _v["state"] == _exp,
          f"got {_v['state']} {_v['tags']}")

check("material: a suspect still passes the yes/no gate",
      gs.is_solid_no_stones("14K Gold Chain 20 grams sterling silver clasp"))
check("material: blocked still fails the yes/no gate",
      not gs.is_solid_no_stones("Sterling Silver and 14k Gold Ring 6 grams"))
check("material: every blocked verdict explains itself",
      all(gs.material_verdict(t)["reason"] for t, e in _mat if e == "blocked"))

# a suspect is demoted, not discarded — and ranks below an identical clear listing
_spotM = 3400/31.1035
def _mkm(title, grams):
    return {"itemId": "v1|1|0", "title": title,
            "price": {"value": str(round(gs.PURITY[14]*_spotM*grams*0.7, 2))},
            "shippingOptions": [{"shippingCost": {"value": "0"}}],
            "seller": {"username": "a", "feedbackScore": 900, "feedbackPercentage": "99.5"},
            "additionalImages": [1, 2, 3]}
_cfgM = copy.deepcopy(gs.CONFIG)
_clear = gs.evaluate(_mkm("14k Solid Gold Rope Chain 20 grams", 20), _spotM, _cfgM)
_susp = gs.evaluate(_mkm("14k Gold Chain 20 grams sterling silver clasp", 20), _spotM, _cfgM)
check("material: a suspect survives evaluation", _susp is not None)
check("material: suspect ranks below an identical clear listing",
      _susp["score"] < _clear["score"], f"{_susp['score']} vs {_clear['score']}")
check("material: the row says why it's uncertain",
      _susp["material"] == "suspect" and bool(_susp["material_why"]))

# the deep re-check must use the SAME grading, or it undoes the whole thing
check("material: deep re-check keeps a component-metal suspect",
      gs.deep_disqualifies({"title": "14k Gold Chain 20 grams"},
                           {"description": "solid 14k gold, sterling silver lobster clasp"})
      is None)
check("material: deep re-check still blocks a primary second metal",
      gs.deep_disqualifies({"title": "14k Solid Gold Ring 5 grams"},
                           {"description": "14k gold with a stainless steel band"})
      is not None)

# --- feedback: a filter defect must not retire a good search term ------------
# The fb_effects table was honoured for alerting and silently ignored for rotation,
# so a 👎 meaning "our parser missed this" hit the term at full weight.
_cfgFB = copy.deepcopy(gs.CONFIG); _cfgFB["feedback_file"] = "_test_fb_cat.json"
json.dump({"events": [
    {"query": "gold ring grams", "verdict": "bad", "category": "mixed"},
    {"query": "gold ring grams", "verdict": "bad", "category": "weight_karat"},
    {"query": "gold ring grams", "verdict": "bad", "category": "stones"},
    {"query": "gold ring grams", "verdict": "bad", "category": "plated"},
    {"query": "junk term",       "verdict": "bad", "category": "irrelevant"},
    {"query": "plain term",      "verdict": "bad"},
]}, open("_test_fb_cat.json", "w"))
_fbc = gs.feedback_counts(_cfgFB)
check("feedback: filter-defect 👎 carries no rotation weight",
      _fbc["gold ring grams"]["down"] == 0, str(_fbc["gold ring grams"]))
check("feedback: 'not what I searched for' still penalises the term",
      _fbc["junk term"]["down"] == 1.0)
check("feedback: an uncategorised 👎 keeps full weight",
      _fbc["plain term"]["down"] == 1.0)

# and the consequence that actually matters: it must not get retired for our bug
_statsFB = {"meta": {"run_counter": 0}, "queries": {
    "gold ring grams": {"runs": 30, "deals": 6, "strong": 6, "origin": "core"}}}
_cfgFB["retire_min_live"] = 1
_, _retFB = gs.update_query_stats(_cfgFB, _statsFB, [], [])
check("feedback: a term isn't retired for our own filter defects",
      "gold ring grams" not in _retFB)

# defects are logged as evidence, and never auto-edit anything
_cfgFB["defects_file"] = "_test_defects.json"
_bl = gs.build_defect_backlog(_cfgFB, [{"id": "1", "reason": "second metal named in the listing body"}])
check("defects: backlog counts every defect category",
      sum(_bl["by_category"].values()) == 4, str(_bl["by_category"]))
check("defects: engine drops recorded alongside your taps", _bl["total"] == 5)
for _f in ("_test_fb_cat.json", "_test_defects.json"):
    if os.path.exists(_f): os.remove(_f)

# REGRESSION: the AG penalty must not gate the detail call that removes it. A 30%
# under-melt listing scores 60 on melt but ~35 after the unconfirmed-AG docking; with
# a confirmation floor of 45 it could never be confirmed, so the docking was permanent.
_cfgML = copy.deepcopy(gs.CONFIG)
_spotML = 3400/31.1035
_itML = {"itemId": "v1|9|0", "title": "14K Solid Gold Ring 5 grams",
         "price": {"value": str(round(gs.PURITY[14]*_spotML*5*0.7, 2))},
         "shippingOptions": [{"shippingCost": {"value": "0"}}],
         "seller": {"username": "a", "feedbackScore": 900, "feedbackPercentage": "99.5"},
         "additionalImages": [1, 2, 3]}
_rML = gs.evaluate(_itML, _spotML, _cfgML)
check("ag: melt_score is recorded before the AG penalty",
      _rML["melt_score"] > _rML["score"], f"{_rML['melt_score']} vs {_rML['score']}")
check("ag: an unconfirmed listing still clears the confirmation floor",
      _rML["melt_score"] >= _cfgML["ag_confirm_min_score"],
      f"{_rML['melt_score']} vs floor {_cfgML['ag_confirm_min_score']}")

# --- guardrails on the relative retirement rule -----------------------------
# The floor exists so a bad sweep can't gut coverage. Same fixture, production floor.
_cfgFloor = copy.deepcopy(gs.CONFIG); _cfgFloor["query_stats_file"] = "_test_qs4.json"
_statsFloor = {"meta": {"run_counter": 0}, "queries": {
    "dead1": {"runs": 30, "deals": 0, "traps": 20, "origin": "explore"},
    "dead2": {"runs": 30, "deals": 0, "traps": 18, "origin": "explore"},
    "ok":    {"runs": 30, "deals": 8, "strong_ag": 8, "origin": "explore"}}}
_, _retF = gs.update_query_stats(_cfgFloor, _statsFloor, [], [])
check("retire: live-term floor blocks culling a small pool", _retF == [])

# batch cap: many dead terms, big pool, only retire_batch_cap go per sweep
_cfgCap = copy.deepcopy(gs.CONFIG); _cfgCap["query_stats_file"] = "_test_qs5.json"
_cfgCap["retire_batch_cap"] = 2; _cfgCap["retire_min_live"] = 1
_statsCap = {"meta": {"run_counter": 0}, "queries": {
    f"dead{i}": {"runs": 30, "deals": 0, "traps": 10, "origin": "explore"} for i in range(9)}}
_statsCap["queries"]["good"] = {"runs": 30, "deals": 9, "strong_ag": 9, "origin": "explore"}
_, _retC = gs.update_query_stats(_cfgCap, _statsCap, [], [])
check("retire: batch cap limits retirements per sweep", len(_retC) == 2, str(len(_retC)))

# a term you've thumbed up is never auto-retired, however bad its numbers
_cfgLiked = copy.deepcopy(gs.CONFIG); _cfgLiked["query_stats_file"] = "_test_qs6.json"
_cfgLiked["retire_min_live"] = 1; _cfgLiked["feedback_file"] = "_test_fb_liked.json"
json.dump({"events": [{"query": "liked", "verdict": "good"},
                      {"query": "liked", "verdict": "good"}]},
          open("_test_fb_liked.json", "w"))
_statsLiked = {"meta": {"run_counter": 0}, "queries": {
    "liked": {"runs": 30, "deals": 0, "traps": 15, "origin": "core"},
    "other": {"runs": 30, "deals": 5, "strong": 5, "origin": "core"}}}
_, _retL = gs.update_query_stats(_cfgLiked, _statsLiked, [], [])
check("retire: net-liked term protected from auto-retirement", "liked" not in _retL)
os.remove("_test_fb_liked.json")

# --- Authenticity Guarantee -------------------------------------------------
_cfgAG = copy.deepcopy(gs.CONFIG)
_spot = 3400/31.1035
def _agitem(price, grams=8.0, qp=None, karat=14):
    it = {"itemId": "v1|1|0", "title": f"{karat}K Solid Gold Rope Chain {grams} grams",
          "price": {"value": str(price)},
          "shippingOptions": [{"shippingCost": {"value": "0.00"}}],
          "seller": {"username": "s", "feedbackScore": 900, "feedbackPercentage": "99.5"},
          "additionalImages": [1, 2, 3]}
    if qp: it["qualifiedPrograms"] = qp
    return it
_AGOPT = {"addonServices": [{"serviceType": "AUTHENTICITY_GUARANTEE",
                             "selection": "OPTIONAL", "serviceFee": {"value": "40.00"}}]}
_AGREQ = {"addonServices": [{"serviceType": "AUTHENTICITY_GUARANTEE",
                             "selection": "REQUIRED", "serviceFee": {"value": "0.00"}}]}

check("ag: search-hit badge reads as included",
      gs.ag_status(_agitem(700, qp=["AUTHENTICITY_GUARANTEE"]), _cfgAG)[0] == "included")
check("ag: detail OPTIONAL reads as optional with its fee",
      gs.ag_status(_agitem(300), _cfgAG, detail=_AGOPT)[:2] == ("optional", 40.0))
check("ag: detail REQUIRED reads as included",
      gs.ag_status(_agitem(700), _cfgAG, detail=_AGREQ)[0] == "included")
check("ag: a detail call with no AG is a CONFIRMED none",
      gs.ag_status(_agitem(300), _cfgAG, detail={"description": "x"}) == ("none", 0.0, True))
check("ag: below the band, no authentication at any price",
      gs.ag_status(_agitem(120), _cfgAG)[0] == "none")
check("ag: in-band search hit is an UNCONFIRMED guess, never a guarantee",
      gs.ag_status(_agitem(300), _cfgAG)[0] == "unknown"
      and gs.ag_status(_agitem(300), _cfgAG)[2] is False)

# the fee must actually be charged into profit, not just displayed
_p14 = gs.PURITY[14]*_spot
_rowOpt = gs.evaluate(_agitem(round(_p14*8*0.70), 8.0), _spot, _cfgAG, detail=_AGOPT)
check("ag: paying the fee is reflected in profit",
      abs((_rowOpt["raw_profit"] - _rowOpt["profit"]) - 40.0) < 0.01,
      f"{_rowOpt['raw_profit']} vs {_rowOpt['profit']}")

# score must never fall as the discount deepens (the fee cliff regression)
_prev, _mono = -1, True
for _pct in (0.05, 0.10, 0.12, 0.15, 0.20, 0.30, 0.40):
    _r = gs.evaluate(_agitem(round(_p14*5*(1-_pct)), 5.0), _spot, _cfgAG)
    if not _r: continue
    if _r["score"] < _prev: _mono = False
    _prev = _r["score"]
check("ag: score is monotonic across the fee threshold", _mono)

# an unprotected listing must rank below an identical protected one
_pr = gs.evaluate(_agitem(round(_p14*8*0.75), 8.0, qp=["AUTHENTICITY_GUARANTEE"]), _spot, _cfgAG)
_un = gs.evaluate(_agitem(round(_p14*8*0.75), 8.0), _spot, _cfgAG,
                  detail={"description": "x"})
check("ag: protected outranks identical unprotected", _pr["score"] > _un["score"],
      f"{_pr['score']} vs {_un['score']}")

# the fee must never soften a trap
_trap = gs.evaluate(_agitem(round(_p14*8*0.20), 8.0), _spot, _cfgAG, detail=_AGOPT)
check("ag: fee never masks a trap", _trap["trap"] is True)

# require mode drops what can't be authenticated
_cfgReq = copy.deepcopy(_cfgAG); _cfgReq["ag_mode"] = "require"
check("ag: require mode drops unauthenticatable listings",
      gs.evaluate(_agitem(120, 1.5), _spot, _cfgReq) is None)

# REGRESSION: the old rule retired only on `deals==0 AND traps==0`, so a search
# returning nothing but traps was immortal. That's why rotation stalled.
cfgT = copy.deepcopy(gs.CONFIG); cfgT["query_stats_file"] = "_test_qs3.json"
# The live-term floor and batch cap are production guardrails sized for a ~48-term
# pool. This fixture has three, so scale them down; otherwise the floor (correctly)
# refuses to retire anything and the test is measuring the guardrail, not the rule.
cfgT["retire_min_live"] = 1
cfgT["retire_batch_cap"] = 2
cfgT["promote_min_deals"] = 999  # isolate retirement from promotion for this fixture —
                                  # otherwise neutral/earner promote mid-pass, leave the
                                  # live pool, and trip the min-live floor on their own
statsT = {"meta": {"run_counter": 0}, "queries": {
    "trapfactory": {"runs": 30, "deals": 0, "traps": 20, "origin": "explore"},
    "neutral":     {"runs": 30, "deals": 10, "weak": 10, "traps": 0, "origin": "explore"},
    "earner":      {"runs": 30, "deals": 8, "strong_ag": 8, "traps": 3, "origin": "explore"}}}
_, retiredT = gs.update_query_stats(cfgT, statsT, [], [])
check("retire: trap-only search finally retires", "trapfactory" in retiredT)
check("retire: neutral low-score search survives", "neutral" not in retiredT)
check("retire: AG earner survives despite some traps", "earner" not in retiredT)
check("retire: records the score it died at",
      statsT["queries"]["trapfactory"].get("retired_score") is not None)

# NEW BEHAVIOR: core-origin and already-promoted queries are permanent — the same
# zero-deal, trap-heavy numbers that retire an explore query must NOT retire a core
# one or a promoted one, since the whole point is that established terms don't get
# culled just because the relative bar moved against them on a slow sweep.
cfgPerm = copy.deepcopy(gs.CONFIG); cfgPerm["query_stats_file"] = "_test_qs_perm.json"
cfgPerm["retire_min_live"] = 0
cfgPerm["retire_batch_cap"] = 5
statsPerm = {"meta": {"run_counter": 0}, "queries": {
    "dead_core":      {"runs": 30, "deals": 0, "traps": 20, "origin": "core"},
    "dead_promoted":  {"runs": 30, "deals": 0, "traps": 20, "origin": "explore", "status": "promoted"},
    "dead_explore":   {"runs": 30, "deals": 0, "traps": 20, "origin": "explore"},
    "earner":         {"runs": 30, "deals": 8, "strong_ag": 8, "origin": "core"}}}
_, retiredPerm = gs.update_query_stats(cfgPerm, statsPerm, [], [])
check("retire: core-origin query survives identical bad numbers",
      "dead_core" not in retiredPerm)
check("retire: promoted query survives identical bad numbers",
      "dead_promoted" not in retiredPerm)
check("retire: explore-origin, not-yet-promoted query still retires",
      "dead_explore" in retiredPerm)

# ---------- revive: bounded fresh trials ----------
cfgV = copy.deepcopy(cfgT); cfgV["revived_queries"] = ["trapfactory"]; cfgV["max_revives"] = 2
gs.update_query_stats(cfgV, statsT, [], [])
_tf = statsT["queries"]["trapfactory"]
check("revive: status cleared", _tf.get("status") != "retired")
check("revive: counters reset for a fair trial", _tf.get("runs") == 0 and _tf.get("traps") == 0)
check("revive: revive count tracked", _tf.get("revives") == 1)
_tf.update({"runs": 30, "deals": 0, "traps": 20, "status": "retired"})
gs.update_query_stats(cfgV, statsT, [], [])
gs.update_query_stats(cfgV, statsT, [], [])
_tf2 = statsT["queries"]["trapfactory"]
check("revive: capped at max_revives", _tf2.get("revives") <= cfgV["max_revives"])

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

# Leave no trace: the working copy of this repo gets uploaded by hand, so the suite
# must not litter it. Globbed rather than listed so a new fixture can't be forgotten.
import glob
for f in glob.glob("_test_*.json"):
    try: os.remove(f)
    except OSError: pass

print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL))+' FAILURES: '+', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
