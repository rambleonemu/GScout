#!/usr/bin/env python3
"""
gold_scout.py  --  the engine

Fetches the live gold price, pulls US-only eBay gold listings, keeps only solid
gold (no plating, no gems, no silver/other metals) priced under today's price,
reads descriptions to recover missing weights, filters weak sellers and fake
bars, scores each deal, logs run history for the charts, and writes results.json
+ history.json for the dashboard. Pushes phone alerts on strong new listings.

Two modes (set by the SCOUT_MODE env var):
  full  (default) - full sweep, deep description scan, page + history + alerts
  fast            - quick pass over priority categories, alerts only

No extra eBay keys needed beyond your App ID + Cert ID.
"""

import os, re, csv, time, json, base64, smtplib, requests
import html as _html
from datetime import datetime, timezone
from email.message import EmailMessage

CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "PASTE_APP_ID")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "PASTE_CERT_ID")
SCOUT_MODE    = os.environ.get("SCOUT_MODE", "full")   # "full" or "fast"

CONFIG = {
    "payout_pct":     1.00,   # your buyer pays the full listed gold price (100% of melt)
    "trap_under_pct": 0.50,   # more than this far under price = likely fake/misweighed -> hidden
    "tax_pct":        0.0,    # sales tax you pay buying on eBay
    "max_price":      2000,
    "min_feedback_pct":   96.0,  # skip sellers below this positive-feedback %
    "min_feedback_score": 10,    # skip brand-new sellers below this many ratings
    "queries": [
        # every kind of gold item, per karat. "grams" biases toward listings
        # that state a weight, which we need to price them.
        "10k gold scrap grams", "10k gold chain grams", "10k gold ring grams",
        "10k gold pendant grams", "10k gold bracelet grams", "10k gold necklace grams",
        "10k gold earrings grams",
        "14k gold scrap grams", "14k gold chain grams", "14k gold ring grams",
        "14k gold pendant grams", "14k gold bracelet grams", "14k gold necklace grams",
        "14k gold earrings grams",
        "18k gold scrap grams", "18k gold chain grams", "18k gold ring grams",
        "18k gold pendant grams", "18k gold bracelet grams", "18k gold necklace grams",
        "18k gold earrings grams",
        "22k gold scrap grams", "22k gold chain grams", "22k gold bracelet grams",
        "24k gold scrap grams", "solid gold scrap lot grams",
        # European-marked pieces (585=14k, 750=18k, 417=10k, 916=22k)
        "585 gold grams", "750 gold grams", "417 gold grams",
    ],
    # priority categories scanned in fast mode, newest-first
    "fast_queries": [
        "14k gold scrap grams", "10k gold scrap grams",
        "18k gold scrap grams", "14k gold chain grams",
    ],
    "results_per_query": 50,
    "deep_scan":        True,
    "max_detail_calls": 35,   # hard cap per run, protects your daily eBay quota
    "json_out":   "results.json",
    "deals_csv":  "gold_candidates.csv",
    "traps_csv":  "gold_traps.csv",
    "history_file": "history.json",
    "history_max":  2000,     # keep roughly the last few weeks of runs

    # ---- quota budgeting (all overridable from settings.json / dashboard) ----
    "daily_call_budget": 4500,  # stay under eBay's 5,000/day with headroom for retries
    "runs_per_day": 0,          # 0 = auto-estimate from history timestamps (fallback 48)
    "sort_mode": "alternate",   # "alternate" (halves calls) | "both" | "price" | "newlyListed"

    # ---- content filters ----
    "exclude_mixed": True,      # drop multi-karat items entirely (lots, pendant-on-chain combos)

    # ---- feedback learning (👍/👎 taps from the dashboard -> feedback.json) ----
    "feedback_file": "feedback.json",
    "fb_half_life_days": 45,    # how fast old taps fade
    "fb_weight_span": 0.15,     # max score nudge: ±15%. 0 disables learning entirely
    "fb_seller_block_bad": 2,   # decayed bad-seller signals with zero goods -> seller skipped
    # which categories move which trust weights (query / seller), overridable
    "fb_effects": {
        "plated":       {"query": 1.0, "seller": 1.0},
        "weight_karat": {"query": 1.0, "seller": 0.0},
        "stones":       {"query": 0.6, "seller": 0.0},
        "seller":       {"query": 0.0, "seller": 1.0},
        "lot":          {"query": 0.5, "seller": 0.0},
        "overpriced":   {"query": 0.0, "seller": 0.0},   # pricing math, not their fault
        "style":        {"query": 0.0, "seller": 0.0},   # taste — logged, never punished
        "other":        {"query": 0.0, "seller": 0.0},
    },

    # ---- dynamic query engine ----
    "query_stats_file": "query_stats.json",
    "explore_enabled": True,
    "explore_frac": 0.12,       # share of each run's query slots spent trying new searches
    "explore_pool": [],         # your own candidate searches to try first (from dashboard)
    "promote_min_deals": 3,     # explore query with this many total deals -> promoted to core
    "retire_min_runs": 25,      # a query this many runs old with 0 deals AND 0 traps -> retired
    "starvation_cap": 6,        # rotation cadence hint (informational)
    "exploit_share": 0.7,       # share of core slots locked to proven top performers
    # term lists the auto-explorer combines when your own pool runs dry
    "explore_karats": ["10k", "14k", "18k"],
    "explore_items": ["wedding band", "hoop earrings", "figaro chain", "box chain",
                      "curb chain", "medal", "religious medal", "id bracelet",
                      "watch case", "pinky ring", "nugget ring", "grillz"],
}

EMAIL = {
    "to": os.environ.get("EMAIL_TO", ""), "from": os.environ.get("EMAIL_FROM", ""),
    "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("SMTP_PORT", "465")),
    "smtp_user": os.environ.get("SMTP_USER", ""),
    "smtp_pass": os.environ.get("SMTP_PASS", ""),
    "min_score": 60,
}

ALERT = {
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
    "min_score":  int(os.environ.get("ALERT_MIN_SCORE", "70")),
    "seen_file":  "seen_ids.json",
}

PURITY = {10: 10/24, 14: 14/24, 18: 18/24, 22: 22/24, 24: 0.999}
TROY = 31.1035
EXTRA_EXCLUDE_RE = None   # set from settings.json (your own words to exclude)

NOT_SOLID = re.compile(
    r"\b(gold[\s-]?filled|gold[\s-]?plat(e|ed|ing)|g\.?f\.?\b|g\.?p\.?\b|"
    r"gold[\s-]?tone|gold[\s-]?color|plated|rolled\s?gold|vermeil|overlay|"
    r"gep|rgp|hgp| kgp|\dkgp|hge|electroplate|electro[\s-]?plat|bonded|clad|"
    r"over\s?sterling|over\s?silver|costume|fashion)\b", re.I)
# plating shorthand, used only to word the trap reason precisely
PLATED_RE = re.compile(
    r"\b(gold[\s-]?filled|gold[\s-]?plat|plated|electroplate|electro[\s-]?plat|"
    r"vermeil|g\.?e\.?p|gep|rgp|hge|\dkgp|overlay|bonded|clad)\b", re.I)
# NOTE: every stone noun carries s? (or its irregular plural) — the old version
# matched "diamond" but NOT "diamonds", which let plural-worded listings through.
HAS_STONE = re.compile(
    r"\b(diamonds?|gemstones?|gems?|stones?|cz|cubic\s?zirconias?|sapphires?|rub(?:y|ies)|"
    r"emeralds?|pearls?|opals?|topaz(?:es)?|amethysts?|garnets?|turquoise|jades?|onyx|moissanites?|"
    r"rhinestones?|crystals?|birthstones?|set\s?with|quartz|glass|cameos?|"
    r"shells?|corals?|amber|agates?|lapis|jaspers?|citrines?|peridots?|aquamarines?|tourmalines?|"
    r"zircons?|spinels?|malachites?|moonstones?|marcasites?|abalone|carnelians?|chalcedony|"
    r"hematites?|obsidian|mother\s?of\s?pearl|tiger'?s?\s?eyes?|resin|ceramic|enamel(?:ed)?)\b", re.I)
NON_GOLD = re.compile(
    r"\b(silver|sterling|925|platinum|palladium|titanium|stainless|steel|"
    r"brass|copper|pewter|tungsten|bronze|nickel)\b", re.I)
BAR_RE = re.compile(r"\b(bar|bullion|ingot|shot|pellet|grain)\b", re.I)
# positive-signal language for the "why this scores well" line
HALLMARK_RE = re.compile(r"\b(stamp(ed)?|hallmark(ed)?|marked|signed|tested|acid[\s-]?test"
                         r"|electronic(ally)?[\s-]?tested|xrf)\b", re.I)
SOLID_RE = re.compile(r"\bsolid\b", re.I)
# (?<![\$\d.]) stops "$10k obo" reading as 10 karat and "4.14k" style decimals
KARAT_RE = re.compile(r"(?<![\$\d.])\b(10|14|18|22|24)\s?k(?:t|arat)?\b", re.I)
FINENESS = {"417": 10, "585": 14, "750": 18, "916": 22, "990": 24, "999": 24}
# (?!\.?\d) stops the 585 in a price like "585.00" from reading as 14k fineness
FINENESS_RE = re.compile(r"(?<![\d$.])(417|585|750|916|990|999)(?!\.?\d)")
GRAM_RE  = re.compile(r"(\d*\.?\d+)\s?(?:g\b|gr\b|gram|grams)", re.I)
DWT_RE   = re.compile(r"(\d*\.?\d+)\s?(?:dwt|pennyweight|penny\s?weight)\b", re.I)
FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s?(?:g\b|gr\b|gram|grams)", re.I)
DWT_TO_G = 1.55517
# Weight explicitly attributed to gold (so we can price off gold, not total weight)
GOLD_WT_RES = [
    re.compile(r"(\d*\.?\d+)\s?(?:g\b|grams?)\s+of\s+(?:fine\s|pure\s|solid\s)?gold\b", re.I),
    re.compile(r"\bgold\s*(?:weight|content|wt)\b\s*[:\-]?\s*(\d*\.?\d+)\s?(?:g\b|grams?|dwt)?", re.I),
]
# An item specific that names a second, non-gold metal alongside the gold
MIXED_METAL = re.compile(
    r"(two[\s-]?tone|2[\s-]?tone|mixed\s?metal|base\s?metal|with\s?(silver|steel|"
    r"platinum)|gold\s?(and|&)\s?(silver|steel|platinum))", re.I)


def karat_from_text(text):
    """Read karat from a 10k/14k stamp, or fall back to a European fineness number."""
    m = KARAT_RE.search(text or "")
    if m:
        return int(m.group(1))
    m = FINENESS_RE.search(text or "")
    if m:
        return FINENESS[m.group(1)]
    return None


def karats_in_text(text):
    """Every distinct karat mentioned (stamps + fineness marks). Used to catch
    mixed-grade lots, e.g. '14k and 10k gold lot' -> {10, 14}."""
    if not text:
        return set()
    found = {int(k) for k in KARAT_RE.findall(text)}
    found |= {FINENESS[f] for f in FINENESS_RE.findall(text)}
    return found


def extract_grams(text):
    """Weight in grams from text. Handles fractions (1/2), leading decimals (.5),
    plain grams, and pennyweight (dwt). Fractions are checked first so '1/2 gram'
    isn't misread as 2 grams."""
    if not text:
        return None
    m = FRACTION_RE.search(text)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        if den:
            return _sane_g(round(num / den, 3))
    m = GRAM_RE.search(text)
    if m:
        return _sane_g(float(m.group(1)))
    m = DWT_RE.search(text)
    if m:
        return _sane_g(round(float(m.group(1)) * DWT_TO_G, 2))
    return None


def _sane_g(v):
    """Weights outside (0, 2000]g on sub-$2000 listings are parse errors
    (mm measurements, model numbers), not gold. Reject rather than misprice."""
    return v if v and 0 < v <= 2000 else None


def strip_html(s):
    return _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))


def extract_gold_grams(text):
    """Weight the seller attributes specifically to gold (e.g. '8.2g of gold',
    'gold weight: 8.2g'). Lets us price off the gold, not the total. Returns None
    if no gold-specific weight is stated."""
    if not text:
        return None
    for rx in GOLD_WT_RES:
        m = rx.search(text)
        if m:
            try:
                v = float(m.group(1))
                return _sane_g(round(v * DWT_TO_G, 2) if "dwt" in m.group(0).lower() else v)
            except (TypeError, ValueError):
                pass
    return None


def is_solid_no_stones(text):
    if NOT_SOLID.search(text):
        return False
    if NON_GOLD.search(text):
        return False
    if EXTRA_EXCLUDE_RE and EXTRA_EXCLUDE_RE.search(text):   # your own words from settings
        return False
    cleaned = re.sub(r"diamond[\s-]?cut", "", text, flags=re.I)
    cleaned = re.sub(r"\b(no|without|free\s?of|minus)\s+(stone|stones|gem|gems|"
                     r"gemstone|gemstones|diamond|diamonds)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(stone|gem|diamond)[\s-]?free\b", "", cleaned, flags=re.I)
    if HAS_STONE.search(cleaned):
        return False
    return True


def seller_ok(item, cfg):
    s = item.get("seller") or {}
    if s.get("username") and s["username"] in cfg.get("_seller_block", ()):
        return False   # you flagged this seller repeatedly with zero goods
    try:
        pct = s.get("feedbackPercentage")
        if pct is not None and float(pct) < cfg["min_feedback_pct"]:
            return False
        score = s.get("feedbackScore")
        if score is not None and int(score) < cfg["min_feedback_score"]:
            return False
    except (TypeError, ValueError):
        pass
    return True


def deal_score(under_by, payout_pct, trap_under):
    be = 1 - payout_pct
    if trap_under <= be:
        return 0
    s = (under_by - be) / (trap_under - be) * 100
    return max(0, min(100, round(s)))


def load_settings(cfg):
    """Read settings.json (written by the website's settings panel) and override
    the engine controls. Missing file or fields just fall back to the defaults."""
    global EXTRA_EXCLUDE_RE
    try:
        with open("settings.json") as f:
            s = json.load(f)
    except Exception:
        return
    for k in ("payout_pct", "trap_under_pct", "max_price", "max_detail_calls",
              "min_feedback_pct", "min_feedback_score", "results_per_query",
              "daily_call_budget", "runs_per_day", "fb_half_life_days",
              "fb_weight_span", "fb_seller_block_bad", "explore_frac",
              "promote_min_deals", "retire_min_runs", "starvation_cap", "exploit_share",
              "history_max"):
        if isinstance(s.get(k), (int, float)):
            cfg[k] = s[k]
    for k in ("exclude_mixed", "explore_enabled"):
        if isinstance(s.get(k), bool):
            cfg[k] = s[k]
    if s.get("sort_mode") in ("alternate", "both", "price", "newlyListed"):
        cfg["sort_mode"] = s["sort_mode"]
    if isinstance(s.get("explore_pool"), list):
        cfg["explore_pool"] = [q.strip() for q in s["explore_pool"]
                               if isinstance(q, str) and q.strip()]
    for k in ("explore_karats", "explore_items"):
        if isinstance(s.get(k), list) and s[k]:
            cfg[k] = [str(x).strip() for x in s[k] if str(x).strip()]
    if isinstance(s.get("fb_effects"), dict):
        for cat, eff in s["fb_effects"].items():
            if isinstance(eff, dict):
                cfg["fb_effects"][cat] = {"query": float(eff.get("query", 0)),
                                          "seller": float(eff.get("seller", 0))}
    if isinstance(s.get("queries"), list):
        qs = [q.strip() for q in s["queries"] if isinstance(q, str) and q.strip()]
        if qs:
            cfg["queries"] = qs
    if isinstance(s.get("fast_queries"), list):
        fq = [q.strip() for q in s["fast_queries"] if isinstance(q, str) and q.strip()]
        if fq:
            cfg["fast_queries"] = fq
    if "alert_min_score" in s:
        try:
            ALERT["min_score"] = int(s["alert_min_score"])
        except (TypeError, ValueError):
            pass
    words = [re.escape(w.strip()) for w in (s.get("extra_exclude") or [])
             if isinstance(w, str) and w.strip()]
    if words:
        EXTRA_EXCLUDE_RE = re.compile(r"\b(" + "|".join(words) + r")\b", re.I)
    print(f"settings.json: {len(cfg['queries'])} queries · payout {cfg['payout_pct']} · "
          f"trap {cfg['trap_under_pct']} · {len(words)} extra excludes")


# ======================== feedback learning engine =========================

def load_feedback(cfg):
    """Read feedback.json (👍/👎 taps committed by the dashboard) and turn it into
    per-query and per-seller trust multipliers plus a seller blocklist.

    Design: exponential time decay (half-life fb_half_life_days) so old taps fade;
    Laplace-smoothed trust = (good+1)/(good+bad+2) so one tap can't swing anything;
    trust maps to a bounded multiplier 1 ± fb_weight_span. A nudge, never a veto.
    Returns (query_mult, seller_mult, seller_block, n_events)."""
    try:
        with open(cfg["feedback_file"]) as f:
            events = json.load(f)
        assert isinstance(events, list)
    except Exception:
        return {}, {}, set(), 0

    now = datetime.now(timezone.utc).timestamp()
    half = max(1.0, float(cfg["fb_half_life_days"])) * 86400
    qg, qb, sg, sb = {}, {}, {}, {}
    for e in events:
        if not isinstance(e, dict) or e.get("verdict") not in ("good", "bad"):
            continue
        ts = float(e.get("ts", now)) / (1000 if float(e.get("ts", now)) > 1e11 else 1)
        w = 0.5 ** (max(0.0, now - ts) / half)
        q, s = (e.get("query") or "").strip(), (e.get("seller") or "").strip()
        if e["verdict"] == "good":
            if q: qg[q] = qg.get(q, 0) + w
            if s: sg[s] = sg.get(s, 0) + w
        else:
            eff = cfg["fb_effects"].get(e.get("category") or "other",
                                        {"query": 0.0, "seller": 0.0})
            if q and eff.get("query"):  qb[q] = qb.get(q, 0) + w * eff["query"]
            if s and eff.get("seller"): sb[s] = sb.get(s, 0) + w * eff["seller"]

    span = max(0.0, min(0.5, float(cfg["fb_weight_span"])))
    def mults(good, bad):
        out = {}
        for k in set(good) | set(bad):
            trust = (good.get(k, 0) + 1) / (good.get(k, 0) + bad.get(k, 0) + 2)
            out[k] = round(1 + span * 2 * (trust - 0.5), 4)
        return out

    block = {s for s, b in sb.items()
             if b >= cfg["fb_seller_block_bad"] - 0.05 and sg.get(s, 0) == 0}
    return mults(qg, qb), mults(sg, sb), block, len(events)


# ====================== dynamic query selection engine ======================

def load_query_stats(cfg):
    try:
        with open(cfg["query_stats_file"]) as f:
            st = json.load(f)
        st.setdefault("meta", {}).setdefault("run_counter", 0)
        st.setdefault("queries", {})
        return st
    except Exception:
        return {"meta": {"run_counter": 0}, "queries": {}}


def save_query_stats(cfg, stats):
    try:
        with open(cfg["query_stats_file"], "w") as f:
            json.dump(stats, f, indent=1)
    except Exception as e:
        print(f"  ! couldn't save query stats: {e}")


def estimate_runs_per_day(cfg):
    """Use the actual gaps between recent runs in history.json — the ground truth of
    your real cadence — falling back to 48 (30-min) if history is thin."""
    if cfg["runs_per_day"]:
        return max(1, int(cfg["runs_per_day"]))
    try:
        with open(cfg["history_file"]) as f:
            hist = json.load(f)
        # median of the last 24 gaps: adapts to a cron cadence change within ~half a
        # day, and the median shrugs off outlier gaps from manual "run now" clicks
        ts = [datetime.fromisoformat(h["t"]).timestamp() for h in hist[-25:]]
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]) if 60 < b - a < 6 * 3600)
        if len(gaps) >= 5:
            return max(1, min(200, round(86400 / gaps[len(gaps) // 2])))
    except Exception:
        pass
    return 48


def sorts_for_run(cfg, run_counter):
    mode = cfg["sort_mode"]
    if mode == "both":
        return ["price", "newlyListed"]
    if mode in ("price", "newlyListed"):
        return [mode]
    return ["price"] if run_counter % 2 == 0 else ["newlyListed"]  # alternate


def explore_candidates(cfg, stats):
    """Candidate searches not already active/known: your own explore_pool first,
    then auto-combined karat × item terms. Retired queries are never re-tried."""
    active = set(cfg["queries"])
    known = stats["queries"]
    pool = [q for q in cfg["explore_pool"] if q not in active]
    for k in cfg["explore_karats"]:
        for item in cfg["explore_items"]:
            q = f"{k} gold {item} grams"
            if q not in active and q not in pool:
                pool.append(q)
    return [q for q in pool
            if known.get(q, {}).get("status") not in ("retired", "promoted")]


def select_queries(cfg, stats, query_mult):
    """Pick which searches this run gets, inside the real quota budget.

    Slots = (daily budget / runs per day − detail-call reserve − margin) / sorts.
    Core queries are ranked by smoothed deals-per-run × your feedback trust, with a
    starvation guarantee: anything unrun for starvation_cap runs jumps the queue,
    so low-data queries keep getting fair trials instead of dying unranked.
    A configurable slice of slots goes to exploration when the pool has candidates.
    """
    rc = stats["meta"]["run_counter"]
    sorts = sorts_for_run(cfg, rc)
    rpd = estimate_runs_per_day(cfg)
    per_run = cfg["daily_call_budget"] / rpd
    pages = max(1, -(-cfg["results_per_query"] // 50))          # ceil, calls per query per sort
    slots = int((per_run - cfg["max_detail_calls"] - 3) / (len(sorts) * pages))
    slots = max(4, slots)

    qs = stats["queries"]
    # core = your settings list + explorers that earned promotion, minus retirees
    promoted = [q for q, st in qs.items()
                if st.get("status") == "promoted" and q not in cfg["queries"]]
    active = [q for q in list(cfg["queries"]) + promoted
              if qs.get(q, {}).get("status") != "retired"]

    def value(q):
        st = qs.get(q, {})
        return ((st.get("deals", 0) + 1) / (st.get("runs", 0) + 2)) * query_mult.get(q, 1.0)

    explore = []
    if cfg["explore_enabled"]:
        pool = explore_candidates(cfg, stats)
        n_ex = min(len(pool), max(0, round(slots * cfg["explore_frac"])))
        if n_ex:
            pool.sort(key=lambda q: (qs.get(q, {}).get("runs", 0),
                                     qs.get(q, {}).get("last_rc", 0)))
            explore = pool[:n_ex]

    # two-tier core fill: proven earners are never evicted by rotation —
    # exploit_share of slots goes to top value, the rest to least-recently-run
    core_slots = max(0, slots - len(explore))
    ranked = sorted(active, key=lambda q: -value(q))
    n_top = min(core_slots, max(1, int(core_slots * cfg.get("exploit_share", 0.7))))
    core = ranked[:n_top]
    rest = sorted((q for q in active if q not in set(core)),
                  key=lambda q: qs.get(q, {}).get("last_rc", -10**9))
    core += rest[:core_slots - n_top]

    est = (len(core) + len(explore)) * len(sorts) * pages + cfg["max_detail_calls"]
    print(f"[budget] ~{per_run:.0f} calls/run available ({cfg['daily_call_budget']}/day "
          f"÷ ~{rpd} runs) -> {slots} query slots · sorts: {'+'.join(sorts)} · "
          f"{len(core)} core + {len(explore)} explore · est {est} calls this run")
    return core, explore, sorts


def update_query_stats(cfg, stats, ran, rows):
    """Record this run's outcomes; promote explorers that earn it, retire dead weight."""
    stats["meta"]["run_counter"] += 1
    rc = stats["meta"]["run_counter"]
    qs = stats["queries"]
    active = set(cfg["queries"])
    for q in ran:
        st = qs.setdefault(q, {"runs": 0, "deals": 0, "traps": 0,
                               "origin": "core" if q in active else "explore"})
        st["runs"] += 1
        st["last_rc"] = rc
        st["last_t"] = datetime.now(timezone.utc).isoformat()
    for r in rows:
        st = qs.get(r.get("query") or "")
        if st:
            st["traps" if r["trap"] else "deals"] += 1

    promoted, retired = [], []
    for q, st in qs.items():
        if (st.get("origin") == "explore" and st.get("status") != "promoted"
                and st.get("deals", 0) >= cfg["promote_min_deals"]):
            st["status"] = "promoted"
            promoted.append(q)
        if (st.get("status") not in ("retired",) and st.get("runs", 0) >= cfg["retire_min_runs"]
                and st.get("deals", 0) == 0 and st.get("traps", 0) == 0):
            st["status"] = "retired"
            retired.append(q)
    for q in promoted:
        print(f"[explore] PROMOTED to core: {q!r} — add it to your query list to lock it in")
    for q in retired:
        print(f"[stats] retired (0 hits in {qs[q]['runs']} runs): {q!r}")
    return promoted, retired


def get_token():
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {creds}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


API_ERRORS = {"count": 0, "quota": 0}   # non-200s this run; quota = HTTP 429s specifically


def search(token, query, limit, sort="price"):
    out, offset = [], 0
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    while offset < limit:
        page = min(50, limit - offset)
        r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params={"q": query,
                    "filter": "buyingOptions:{FIXED_PRICE},conditions:{USED|UNSPECIFIED},"
                              "itemLocationCountry:US",
                    "sort": sort, "limit": page, "offset": offset}, timeout=30)
        if r.status_code != 200:
            API_ERRORS["count"] += 1
            if r.status_code == 429:
                API_ERRORS["quota"] += 1
            print(f"  ! {query!r} -> HTTP {r.status_code}: {r.text[:120]}"); break
        items = r.json().get("itemSummaries", []) or []
        out.extend(items)
        if len(items) < page:
            break
        offset += page; time.sleep(0.2)
    return out


def get_item_detail(token, item_id):
    try:
        r = requests.get(f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
            headers={"Authorization": f"Bearer {token}",
                     "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}, timeout=20)
        if r.status_code == 429:
            return "RATELIMIT"
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _ship_cost(item):
    for opt in item.get("shippingOptions", []) or []:
        c = (opt.get("shippingCost") or {}).get("value")
        if c is not None:
            return float(c)
    return 0.0


def evaluate_core(item, karat, grams, spot24, cfg, title_text=None, photos=None,
                  gold_specific=False, mixed_karats=None):
    if not seller_ok(item, cfg):
        return None
    price = float((item.get("price") or {}).get("value", 0) or 0)
    if price <= 0 or grams <= 0 or price > cfg["max_price"]:
        return None
    ship = _ship_cost(item)
    page_per_g = PURITY[karat] * spot24
    cost = (price + ship) * (1 + cfg["tax_pct"])
    all_in_g = cost / grams
    if all_in_g >= page_per_g:
        return None
    under_by = (page_per_g - all_in_g) / page_per_g

    title = title_text or item.get("title", "")
    is_trap = under_by >= cfg["trap_under_pct"]
    reason = ""
    if karat == 24 and BAR_RE.search(title):    # 24k "bullion" under melt is almost always fake
        is_trap = True
        reason = "24k bar/bullion priced under melt — almost always counterfeit or plated"
    elif is_trap:
        # discount this deep past melt isn't a real deal; explain the most likely cause
        if PLATED_RE.search(title):
            reason = (f"{round(under_by*100)}% under melt and the title shows plating shorthand "
                      f"— the weight is base metal with a thin gold layer, not solid gold")
        elif under_by >= 0.85:
            reason = (f"{round(under_by*100)}% under melt — that's not a discount, the weight "
                      f"is real but the metal almost certainly isn't solid gold (plated/filled)")
        else:
            reason = (f"{round(under_by*100)}% under melt — too far below spot to be a genuine "
                      f"deal; likely wrong karat, inflated weight, or a non-gold core")
    # soft flag: a steep-but-not-trap discount is the kind most often caused by a
    # wrong/total weight, so prompt a closer look without hiding it
    mixed = bool(mixed_karats and len(mixed_karats) > 1)
    verify = (not is_trap) and (under_by >= 0.40 or mixed)

    if photos is None:
        # additionalImages is the real gallery count; thumbnailImages is ~always a
        # single thumb, which made every fast-path listing look like "2 photos"
        photos = 1 + len(item.get("additionalImages") or item.get("thumbnailImages") or [])
    payout = page_per_g * grams * cfg["payout_pct"]
    seller = item.get("seller") or {}
    s_score = int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None else None
    s_pct = float(seller["feedbackPercentage"]) if seller.get("feedbackPercentage") else None

    # mixed-grade items: excluded by default (currently steering clear of lots /
    # multi-karat pieces) — flip exclude_mixed in settings.json / the dashboard to
    # revert to the old behavior of pricing at the lowest-karat floor with a flag.
    if mixed and cfg.get("exclude_mixed", True):
        return None
    mixed_note = ""
    if mixed:
        ks = "+".join(f"{k}k" for k in sorted(mixed_karats))
        mixed_note = (f"mixed lot ({ks}) — priced at {karat}k floor since the listing "
                      f"doesn't break down weight by grade")

    # positive signals: what makes a genuine deal look trustworthy (learning aid, not proof)
    why = []
    if not is_trap:
        if gold_specific:
            why.append("seller stated gold weight separately")
        if HALLMARK_RE.search(title):
            why.append("hallmark/tested language in title")
        elif SOLID_RE.search(title):
            why.append('titled "solid"')
        cat = " ".join(c.get("categoryName", "") for c in (item.get("categories") or []))
        if "fine" in cat.lower():
            why.append("listed under fine jewelry")
        if s_score is not None and s_score >= 500:
            why.append(f"established seller ({s_score//1000}k sales)" if s_score >= 1000
                       else f"established seller ({s_score} sales)")
        if s_pct is not None and s_pct >= 99:
            why.append("99%+ feedback")
        if photos >= 5:
            why.append(f"well documented ({photos} photos)")
        if 0.05 <= under_by <= 0.35:
            why.append("believable margin, not too-good")
    deal_why = ", ".join(why[:3])

    return {
        "score": deal_score(under_by, cfg["payout_pct"], cfg["trap_under_pct"]),
        "under_pct": round(under_by * 100, 1),
        "trap": is_trap, "trap_reason": reason, "deal_why": deal_why,
        "verify": verify, "gold_wt": gold_specific,
        "mixed_lot": mixed, "mixed_note": mixed_note,
        "offer": "BEST_OFFER" in (item.get("buyingOptions") or []),
        "seller_pct": s_pct, "seller_score": s_score, "seller_user": seller.get("username", ""), "photos": photos,
        "id": item.get("itemId", ""),
        "karat": f"{karat}K", "grams": grams, "price": round(price, 2), "ship": round(ship, 2),
        "all_in_per_g": round(all_in_g, 2), "page_per_g": round(page_per_g, 2),
        "profit": round(payout - cost, 2),
        "title": title[:110], "url": item.get("itemWebUrl", ""),
        "listed": item.get("itemCreationDate", ""),
    }


def evaluate(item, spot24, cfg):
    title = item.get("title", "")
    if not is_solid_no_stones(title):
        return None
    ks = karats_in_text(title)
    karat = min(ks) if ks else karat_from_text(title)   # mixed lot -> lowest karat floor
    gold_g = extract_gold_grams(title)
    grams = gold_g or extract_grams(title)
    if not karat or not grams:
        return None
    return evaluate_core(item, karat, grams, spot24, cfg,
                         gold_specific=bool(gold_g), mixed_karats=ks)


def needs_description(item, cfg):
    title = item.get("title", "")
    if not is_solid_no_stones(title):
        return False
    if not karat_from_text(title):
        return False
    if extract_grams(title):
        return False
    price = float((item.get("price") or {}).get("value", 0) or 0)
    return 0 < price <= cfg["max_price"]


def evaluate_deep(item, detail, spot24, cfg):
    title = item.get("title", "")
    desc = strip_html(detail.get("description", ""))
    aspects = " ".join(
        f"{a.get('name','')}: {' '.join(a.get('values', []))}"
        for a in (detail.get("localizedAspects") or [])
    )
    blob = f"{title} {aspects} {desc}"
    if not is_solid_no_stones(blob):
        return None
    # reject pieces whose item specifics name a second non-gold metal (two-tone
    # with silver/steel, base metal, etc.) — these inflate the weight with non-gold
    if MIXED_METAL.search(aspects):
        return None
    ks = karats_in_text(title) or karats_in_text(aspects)
    karat = min(ks) if ks else (karat_from_text(title) or karat_from_text(aspects))
    if not karat:
        return None
    gold_g = extract_gold_grams(blob)
    grams = gold_g or extract_grams(aspects) or extract_grams(desc)
    if not grams:
        return None
    photos = 1 + len(detail.get("additionalImages") or [])
    return evaluate_core(item, karat, grams, spot24, cfg, title_text=title,
                         photos=photos, gold_specific=bool(gold_g), mixed_karats=ks)


def load_seen(path):
    try:
        with open(path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(path, seen):
    try:
        with open(path, "w") as f:
            json.dump(sorted(seen)[-5000:], f)   # cap: alerted-id memory never bloats the cache
    except Exception:
        pass


def notify(title, body, priority="default", tags="warning"):
    """Plain ntfy push for health/failure alerts (separate from deal alerts)."""
    topic = ALERT["ntfy_topic"]
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=15)
    except Exception as e:
        print(f"  ! notify failed: {e}")


def send_alerts(deals):
    topic = ALERT["ntfy_topic"]
    if not topic:
        return
    seen = load_seen(ALERT["seen_file"])
    fresh = [d for d in deals
             if d["score"] >= ALERT["min_score"] and (d["id"] or d["url"]) not in seen]
    for d in fresh:
        try:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=(f"{d['karat']} {d['grams']}g  ${d['price']:.0f}  "
                      f"{d['under_pct']}% under  est ${d['profit']:.0f}\n{d['title']}").encode("utf-8"),
                headers={"Title": f"Gold deal · score {d['score']}",
                         "Priority": "high", "Tags": "moneybag", "Click": d["url"]},
                timeout=15)
            seen.add(d["id"] or d["url"])
        except Exception as e:
            print(f"  ! alert failed: {e}")
    if fresh:
        print(f"alerted {len(fresh)} new deal(s) via ntfy")
    save_seen(ALERT["seen_file"], seen)


def send_email(deals):
    if not EMAIL["to"] or not EMAIL["smtp_user"]:
        return
    keep = [d for d in deals if d["score"] >= EMAIL["min_score"]]
    if not keep:
        return
    lines = [f"{len(keep)} solid-gold deals scoring {EMAIL['min_score']}+:", ""]
    for d in keep[:40]:
        lines.append(f"[{d['score']:3}] {d['under_pct']}% under  {d['karat']} {d['grams']}g  "
                     f"${d['price']:.0f}  profit ${d['profit']:.0f}\n  {d['title']}\n  {d['url']}")
    msg = EmailMessage()
    msg["Subject"] = f"Gold Scout: {len(keep)} deals"
    msg["From"], msg["To"] = EMAIL["from"] or EMAIL["smtp_user"], EMAIL["to"]
    msg.set_content("\n".join(lines))
    with smtplib.SMTP_SSL(EMAIL["smtp_host"], EMAIL["smtp_port"]) as s:
        s.login(EMAIL["smtp_user"], EMAIL["smtp_pass"]); s.send_message(msg)
    print(f"emailed {len(keep)} deals to {EMAIL['to']}")


def append_history(cfg, spot_oz, prices, deals, traps, price_src="", new_deals=0, api_errors=0):
    # per-query hit tracking: only queries with at least one deal or trap this run
    # are logged, so this stays compact — sum across history to see which queries
    # actually earn their call budget vs. which are dead weight.
    by_query = {}
    for d in deals:
        by_query.setdefault(d.get("query") or "?", {"deals": 0, "traps": 0})["deals"] += 1
    for t in traps:
        by_query.setdefault(t.get("query") or "?", {"deals": 0, "traps": 0})["traps"] += 1

    rec = {
        "t": datetime.now(timezone.utc).isoformat(),
        "spot_oz": round(spot_oz, 2),
        "price_src": price_src,
        "p14": prices.get("14K"),
        "deals": len(deals),
        "new_deals": new_deals,
        "api_errors": api_errors,
        "traps": len(traps),
        "avg_under": round(sum(d["under_pct"] for d in deals) / len(deals), 1) if deals else 0,
        "avg_score": round(sum(d["score"] for d in deals) / len(deals), 1) if deals else 0,
        "avg_profit": round(sum(d["profit"] for d in deals) / len(deals), 2) if deals else 0,
        "best": max((d["score"] for d in deals), default=0),
        "profit": round(sum(d["profit"] for d in deals if d["profit"] > 0), 2),
        "by_karat": {k: sum(1 for d in deals if d["karat"] == k)
                     for k in ["10K", "14K", "18K", "22K", "24K"]},
        "by_query": by_query,
    }
    hist = []
    try:
        with open(cfg["history_file"]) as f:
            hist = json.load(f)
    except Exception:
        hist = []
    hist.append(rec)
    hist = hist[-cfg["history_max"]:]
    # keep long-range history light: per-query detail only matters for recent
    # curation — cumulative totals live in query_stats.json — so strip it from
    # everything but the newest 1500 records
    for old in hist[:-1500]:
        old.pop("by_query", None)
    with open(cfg["history_file"], "w") as f:
        json.dump(hist, f)
    return hist


def _apply_trust(row, cfg):
    """Nudge the score by your accumulated 👍/👎 trust in this query and seller.
    Bounded by fb_weight_span (±15% default), never flips a trap, and the raw
    melt score is preserved as base_score so the UI can show both honestly."""
    qm = cfg.get("_query_mult", {}).get(row.get("query", ""), 1.0)
    sm = cfg.get("_seller_mult", {}).get(row.get("seller_user", ""), 1.0)
    if qm == 1.0 and sm == 1.0:
        return
    row["base_score"] = row["score"]
    row["score"] = max(0, min(100, round(row["score"] * qm * sm)))


def collect(token, queries, spot24, cfg, sort, deep):
    # sort can be one sort string or a list of them. Sweeping both "price"
    # (cheapest/underpriced) and "newlyListed" (fresh) catches deals that a
    # single sort would miss — the cheapest list never shows a brand-new
    # mid-priced bargain, and the newest list never shows an old underpriced one.
    sorts = sort if isinstance(sort, (list, tuple)) else [sort]
    rows, seen, candidates = [], set(), []
    for q in queries:
        for srt in sorts:
            print(f"Searching ({srt}): {q}")
            for item in search(token, q, cfg["results_per_query"], sort=srt):
                iid = item.get("itemId")
                if iid in seen:
                    continue
                seen.add(iid)
                row = evaluate(item, spot24, cfg)
                if row:
                    row["query"] = q
                    _apply_trust(row, cfg)
                    rows.append(row)
                elif deep and cfg["deep_scan"] and needs_description(item, cfg):
                    item["_src_query"] = q
                    candidates.append(item)

    if deep and cfg["deep_scan"] and candidates:
        candidates.sort(key=lambda it: float((it.get("price") or {}).get("value", 1e9) or 1e9))
        cap = cfg["max_detail_calls"]
        print(f"Deep-scanning {min(cap, len(candidates))} of {len(candidates)} weightless listings")
        recovered = 0
        for item in candidates[:cap]:
            detail = get_item_detail(token, item.get("itemId"))
            if detail == "RATELIMIT":
                print("  ! eBay rate limit hit, stopping deep scan"); break
            if not detail:
                continue
            row = evaluate_deep(item, detail, spot24, cfg)
            if row:
                row["query"] = item.get("_src_query", "")
                _apply_trust(row, cfg)
                rows.append(row); recovered += 1
            time.sleep(0.1)
        print(f"  recovered {recovered} extra deal(s) from descriptions")
    return rows


def main():
    if "PASTE_" in CLIENT_ID or "PASTE_" in CLIENT_SECRET:
        print("Add your eBay CLIENT_ID and CLIENT_SECRET first (see header)."); return

    load_settings(CONFIG)
    try:
        spot_oz, price_src = live_spot_per_oz()
    except Exception as e:
        notify("Gold Scout · price feed down",
               f"Couldn't reach the gold price API ({e}). Skipping this run; will retry next schedule.",
               priority="high", tags="rotating_light")
        print(f"price feed unreachable: {e}")
        return
    spot24 = spot_oz / TROY
    prices = {f"{k}K": round(PURITY[k] * spot24, 2) for k in PURITY}
    print(f"[{SCOUT_MODE}] Live gold: ${spot_oz:.2f}/oz  ->  24K ${spot24:.2f}/g  (via {price_src})")

    token = get_token()

    # feedback learning: your 👍/👎 taps -> trust weights + seller blocklist
    qmult, smult, sblock, n_fb = load_feedback(CONFIG)
    CONFIG["_query_mult"], CONFIG["_seller_mult"], CONFIG["_seller_block"] = qmult, smult, sblock
    if n_fb:
        print(f"[learning] {n_fb} feedback taps -> {len(qmult)} query / {len(smult)} seller "
              f"weights · {len(sblock)} seller(s) blocked")

    if SCOUT_MODE == "fast":
        # quick pass over priority categories, newest first, alerts only
        rows = collect(token, CONFIG["fast_queries"], spot24, CONFIG, sort="newlyListed", deep=False)
        deals = sorted([r for r in rows if not r["trap"]], key=lambda r: r["score"], reverse=True)
        print(f"fast: {len(deals)} deal(s) found, sending alerts only")
        send_alerts(deals)
        return

    # budget-aware selection: rank core queries, reserve exploration slots
    qstats = load_query_stats(CONFIG)
    core_q, explore_q, sorts = select_queries(CONFIG, qstats, qmult)
    ran = core_q + explore_q
    rows = collect(token, ran, spot24, CONFIG, sort=sorts, deep=True)
    promoted, retired = update_query_stats(CONFIG, qstats, ran, rows)
    save_query_stats(CONFIG, qstats)
    deals = sorted([r for r in rows if not r["trap"]], key=lambda r: r["score"], reverse=True)
    traps = sorted([r for r in rows if r["trap"]], key=lambda r: r["under_pct"], reverse=True)

    # load previous results to track price changes across runs (results.json is
    # cached run-to-run, so this is genuinely the last run — not a stale repo copy)
    prev, prev_prices = {}, {}
    try:
        with open(CONFIG["json_out"]) as f:
            prev = json.load(f)
            for d in (prev.get("deals") or []) + (prev.get("traps") or []):
                if d.get("id"):
                    prev_prices[d["id"]] = d.get("price")
    except Exception:
        pass

    # ---- fail-safe: a 0-deal 0-trap sweep is almost always quota/API failure,
    # not a genuinely empty market. Carry the last good listings forward (flagged
    # as carried) so the dashboard stays usable instead of wiping to zero.
    carried_from = None
    if not deals and not traps and (prev.get("deals") or prev.get("traps")):
        deals = prev.get("deals") or []
        traps = prev.get("traps") or []
        carried_from = prev.get("carried_from") or prev.get("updated")
        why = (f"{API_ERRORS['quota']} quota (429) errors" if API_ERRORS["quota"]
               else f"{API_ERRORS['count']} API errors" if API_ERRORS["count"]
               else "no API errors — possibly genuinely empty")
        print(f"[failsafe] sweep returned 0/0 ({why}) — carrying {len(deals)} deal(s) / "
              f"{len(traps)} trap(s) forward from {carried_from}")
        if not prev.get("carried_from"):     # first failed run of this outage only
            notify("GScout sweep failed", f"0 results ({why}). Showing last good data "
                   f"from {carried_from}. Retrying on schedule.", priority="high")

    # attach prev_price so the dashboard can show "was $X → now $Y"
    if not carried_from:
        for row in deals + traps:
            pid = prev_prices.get(row["id"])
            row["prev_price"] = round(pid, 2) if pid is not None and pid != row["price"] else None

    # new-deals-per-run: ids that weren't in the last *real* (non-carried) sweep
    prev_ids = set(qstats["meta"].get("last_deal_ids") or [])
    if carried_from:
        new_count = 0                      # nothing new was actually found
    else:
        cur_ids = {d["id"] for d in deals if d.get("id")}
        new_count = len(cur_ids - prev_ids) if prev_ids else len(cur_ids)
        qstats["meta"]["last_deal_ids"] = sorted(cur_ids)[:3000]
        save_query_stats(CONFIG, qstats)

    hist = append_history(CONFIG, spot_oz, prices, deals if not carried_from else [],
                          traps if not carried_from else [], price_src=price_src,
                          new_deals=new_count, api_errors=API_ERRORS["count"])

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "spot_per_oz": round(spot_oz, 2),
        "prices_per_gram": prices,
        "payout_pct": CONFIG["payout_pct"],
        "deals": deals,
        "traps": traps[:80],
        "traps_count": len(traps),
        "total_profit": round(sum(d["profit"] for d in deals if d["profit"] > 0), 2),
        "settings_used": {
            "payout_pct": CONFIG["payout_pct"], "trap_under_pct": CONFIG["trap_under_pct"],
            "max_price": CONFIG["max_price"], "min_feedback_pct": CONFIG["min_feedback_pct"],
            "alert_min_score": ALERT["min_score"], "queries": CONFIG["queries"],
        },
        "learning": {
            "feedback_events": n_fb,
            "sellers_blocked": sorted(sblock),
            "queries_ran": len(ran), "explored": explore_q,
            "promoted": promoted, "retired": retired,
            "sorts": sorts,
        },
        "carried_from": carried_from,
        "api_errors": dict(API_ERRORS),
        "quota": {
            "est_calls_run": len(ran) * len(sorts) * max(1, -(-CONFIG["results_per_query"] // 50))
                             + CONFIG["max_detail_calls"],
            "runs_per_day_est": estimate_runs_per_day(CONFIG),
            "daily_budget": CONFIG["daily_call_budget"],
        },
    }
    with open(CONFIG["json_out"], "w") as f:
        json.dump(payload, f, indent=2)

    cols = ["score","under_pct","karat","grams","price","ship","all_in_per_g",
            "page_per_g","profit","seller_pct","offer","query","title","url"]
    for path, data in [(CONFIG["deals_csv"], deals), (CONFIG["traps_csv"], traps)]:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader(); w.writerows(data)

    print(f"\n{len(deals)} deals under price ({len(traps)} traps hidden) · "
          f"{len(hist)} runs logged")
    for d in deals[:10]:
        tag = " [offer]" if d["offer"] else ""
        print(f"  [{d['score']:3}] {d['under_pct']:4}% under  {d['karat']} {d['grams']}g  "
              f"${d['price']:.0f}  profit ${d['profit']:.0f}{tag}  {d['title'][:50]}")

    send_alerts(deals)
    send_email(deals)


def live_spot_per_oz():
    """Fetch live XAU/USD spot price with a 3-source fallback chain.
    Returns (price_float, source_label_str)."""

    # --- Source 1: gold-api.com (original, true spot, no key needed) ---
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=10)
        r.raise_for_status()
        price = float(r.json()["price"])
        if price > 500:
            print(f"[price] gold-api.com -> ${price:.2f}/oz")
            return price, "gold-api.com"
    except Exception as e:
        print(f"[price] gold-api.com failed: {e}")

    # --- Source 2: metals.dev (true spot, no key needed) ---
    try:
        r = requests.get(
            "https://api.metals.dev/v1/latest?api_key=demo&currency=USD&unit=toz",
            timeout=10,
        )
        r.raise_for_status()
        price = float(r.json()["metals"]["gold"])
        if price > 500:
            print(f"[price] metals.dev -> ${price:.2f}/oz")
            return price, "metals.dev"
    except Exception as e:
        print(f"[price] metals.dev failed: {e}")

    # --- Source 3: Yahoo Finance XAUUSD=X (forex spot rate, not futures) ---
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X"
            "?interval=1d&range=1d",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        meta = r.json()["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or meta["previousClose"])
        if price > 500:
            print(f"[price] Yahoo Finance XAUUSD=X -> ${price:.2f}/oz (spot)")
            return price, "yahoo XAUUSD=X"
    except Exception as e:
        print(f"[price] Yahoo Finance failed: {e}")

    raise RuntimeError("All 3 gold price sources failed — check network and APIs")


if __name__ == "__main__":
    main()
