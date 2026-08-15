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
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

CLIENT_ID     = os.environ.get("EBAY_CLIENT_ID", "PASTE_APP_ID")
CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "PASTE_CERT_ID")
SCOUT_MODE    = os.environ.get("SCOUT_MODE", "full")   # "full" or "fast"

CONFIG = {
    "payout_pct":     1.00,   # your buyer pays the full listed gold price (100% of melt)
    "trap_under_pct": 0.50,   # more than this far under price = likely fake/misweighed -> hidden
    "offer_max_over_pct": 0.25,   # asking price up to this far OVER melt still worth flagging
                                   # if the seller takes offers — beyond this, no realistic
                                   # accepted offer bridges the gap
    "offer_target_under_pct": 0.10,  # the margin an offer should aim for: 10% under melt,
                                      # the same "believable, not too-good" band real deals land in
    "tax_pct":        0.0,    # sales tax you pay buying on eBay
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
    # Share of the detail-call budget spent CONFIRMING Authenticity Guarantee status
    # rather than recovering weights from descriptions. eBay only returns the
    # addonServices container (which is what tells us the optional AG add-on is
    # purchasable, and for how much) on getItem — the search response never has it.
    # So confirming "can I buy this with a guarantee?" costs one call per listing.
    "ag_detail_share": 0.5,

    # ---- Authenticity Guarantee (AG) ----
    # Three states a listing can be in, and what each costs you:
    #   included  - eBay authenticates it, no charge to you (jewelry >= ag_required_min)
    #   optional  - you can add authentication at checkout for ag_fee (jewelry in the
    #               ag_optional_min..ag_optional_max band). The fee is real money and is
    #               charged into the deal maths below, not just displayed.
    #   none      - no authentication available at any price; you're on your own
    # Bands and fee per eBay's published jewelry AG terms. They're settings, not
    # constants, because eBay has changed them before and will again.
    "ag_mode": "prefer",       # require | prefer | off  (see ag_allow_unguaranteed)
    "ag_fee": 40.0,            # buyer-paid optional AG fee for jewelry
    "ag_optional_min": 200.0,  # below this, AG can't be added at any price
    "ag_optional_max": 499.99, # top of the optional add-on band
    "ag_required_min": 500.0,  # at/above this AG is mandatory and eBay pays for it
    # Score points deducted from a listing with no authentication available. This is
    # how strongly "I'd rather buy protected" is expressed, and there is no single
    # correct number: the AG fee is FIXED at ~$40 while melt value scales with weight,
    # so paying it costs ~31 score points on a 4g piece but only ~10 on a 12g one.
    # At 25 the engine prefers the guaranteed buy on roughly 6g and up, and still lets
    # a small, cheap piece be taken unprotected — where $40 to insure a $200 purchase
    # genuinely may not pay for itself. Raise toward 35 to prefer protection almost
    # always; use ag_mode="require" to make it absolute.
    "ag_score_penalty": 25,
    "ag_confirm_min_score": 45,# only spend a detail call confirming AG above this score
    "json_out":   "results.json",
    "deals_csv":  "gold_candidates.csv",
    "traps_csv":  "gold_traps.csv",
    "history_file": "history.json",
    "history_max":  2000,     # keep roughly the last few weeks of runs

    # ---- quota budgeting (all overridable from settings.json / dashboard) ----
    "daily_call_budget": 4500,
    "reserve_runs": 2,          # budget as if this many extra sweeps happen each day, so
                                # hitting "run now" a couple of times can't overrun quota.
                                # Scheduled runs simply size themselves a little smaller.  # stay under eBay's 5,000/day with headroom for retries
    "runs_per_day": 0,          # 0 = auto-estimate from history timestamps (fallback 48)
    "sort_mode": "alternate",   # "alternate" (halves calls) | "both" | "price" | "newlyListed"

    # ---- content filters ----
    # NOTE: Authenticity Guarantee and mixed-karat lots are display filters, not search
    # or scoring filters — see search() and evaluate_core(). The dashboard decides what
    # to show; the engine always collects and scores both.

    # ---- feedback learning (👍/👎 taps from the dashboard -> feedback.json) ----
    "feedback_file": "feedback.json",
    "fb_half_life_days": 45,    # how fast old taps fade
    "fb_weight_span": 0.15,     # max score nudge: ±15%. 0 disables learning entirely
    "fb_seller_block_bad": 2,   # decayed bad-seller signals with zero goods -> seller skipped
    # which categories move which trust weights (query / seller), overridable
    # What a 👎 actually penalises, by reason.
    #
    # The dividing line is: could the search term have known? A term asked eBay for
    # solid gold jewellery by weight. If eBay returned exactly that and OUR filters
    # failed to spot the plating, the stones, the second metal or the misread weight,
    # then the term did its job and the defect is ours. Penalising the term for it
    # retires good searches, leaves the bug in place, and dresses the whole thing up
    # as evidence — while the numbers are really measuring our own parser.
    #
    # So the defect categories carry ZERO query weight and feed the parsing backlog
    # instead. What still penalises a term is the term genuinely fetching the wrong
    # kind of thing ("irrelevant"), which no filter could have rescued.
    "fb_effects": {
        # --- our defects: never blame the search term ---
        "plated":       {"query": 0.0, "seller": 1.0},   # NOT_SOLID should have caught it
        "weight_karat": {"query": 0.0, "seller": 0.0},   # weight parser defect
        "stones":       {"query": 0.0, "seller": 0.0},   # HAS_STONE defect
        "mixed":        {"query": 0.0, "seller": 0.0},   # MIXED_METAL defect
        # --- genuinely the term's doing ---
        "irrelevant":   {"query": 1.0, "seller": 0.0},   # fetched the wrong kind of item
        "lot":          {"query": 0.5, "seller": 0.0},
        # --- neither ---
        "seller":       {"query": 0.0, "seller": 1.0},
        "overpriced":   {"query": 0.0, "seller": 0.0},   # pricing math, not their fault
        "style":        {"query": 0.0, "seller": 0.0},   # taste — logged, never punished
        "other":        {"query": 0.0, "seller": 0.0},
    },
    # Reasons that mean "our filter let this through", routed to parsing_defects.json.
    "defect_categories": ["plated", "weight_karat", "stones", "mixed"],

    # ---- manual query overrides (set from the dashboard's deals-by-search chart) ----
    # These are your explicit calls and always beat the engine's automatic judgement:
    # pinned queries can never be auto-retired and always get a slot; disabled queries
    # are never run, never explored, and never resurrected by promotion.
    "pinned_queries": [],
    "disabled_queries": [],
    "revived_queries": [],
    "manual_queries": [],       # one-shot override from the dashboard Run drawer
    "manual_run_id": "",

    # ---- dynamic query engine ----
    "query_stats_file": "query_stats.json",
    "explore_enabled": True,
    "explore_frac": 0.35,       # share of each run's query slots spent trying new searches.
                                # High on purpose: at a few sweeps a day there are far more
                                # slots than core queries, and unspent slots are wasted quota.
                                # The point is to hunt for terms, not re-run known ones.
    "explore_pool": [],         # your own candidate searches to try first (from dashboard)
    "promote_min_deals": 3,     # explore query with this many total deals -> promoted to core
    "retire_min_runs": 20,      # runs before a search can be judged at all. Raised from 8:
                                # a term only gets a slot some sweeps, so 8 runs was often
                                # a handful of real trials — well inside the noise band for
                                # a market where good listings appear a few times a week.
                                # Terms were dying before they had a fair sample.
    # Retirement is judged RELATIVE to the rest of the pool, not against a fixed number.
    # A hard "score < 0" cutoff silently changes meaning as gold moves: when spot spikes,
    # margins compress, every term's score sags and the fixed bar starts culling terms
    # that are merely having a bad week. Retire the persistent bottom of the pack instead.
    "retire_rel_median": 0.35,  # retire below this fraction of the live-pool median score
    "retire_batch_cap": 2,      # most terms retirable in one sweep, so a bad run can't
                                # gut the pool in a single pass
    "retire_min_live": 12,      # never retire below this many live terms, whatever the scores
    "retire_protect_liked": True,   # a term with net-positive 👍 is never auto-retired
    "starvation_cap": 6,        # rotation cadence hint (informational)
    "exploit_share": 0.7,       # share of core slots locked to proven top performers

    # How a search earns its slot. Points per hit, summed then divided by runs, so a
    # search is judged on what it brings back per sweep rather than on raw volume.
    # Traps are a cost, not a success — the old rule treated any hit as proof of life,
    # which is why nothing ever rotated out. Authenticity Guarantee counts double
    # because those are the ones that actually close.
    "query_weights": {
        "thumbs_up":    3.0,    # you said this search found something good
        "strong_ag":    3.0,    # strong deal, authenticity guaranteed
        "strong":       1.5,    # strong deal
        "weak_ag":      1.5,    # low score, but authenticity guaranteed
        "weak":         0.0,    # low-score deal — neutral, neither earns nor costs
        "trap":        -1.0,    # cost: burned a call and your attention
        "thumbs_down": -3.0,    # you said this search found junk
    },
    "query_prior_runs": 3,      # smoothing: a new search isn't judged on one sweep
    "max_revives": 2,           # fresh trials a retired search can be granted from the UI
    "strong_score": 70,         # at or above this, a deal counts as "strong" in the
                                # per-search breakdown (matches the default alert floor)
    "defects_file": "parsing_defects.json",
    "suspect_score_penalty": 8,   # demotion applied to a "suspect" material grade
    "reject_sample_max": 120,     # rejected listings kept for review each sweep
    "ended_keep_days": 14,      # how long a vanished listing stays in Dead Listings
    "spot_stale_max_hours": 24, # how old a cached spot price may be during API outages
    # term lists the auto-explorer combines when your own pool runs dry
    "explore_karats": ["10k", "14k", "18k", "22k", "24k", "gold"],
    # Deliberately long and deliberately misspelled. Misspellings are the edge: fewer
    # bidders find them, so they close under melt. Every entry here is a candidate the
    # explorer can trial, score, and either promote or retire on its own.
    "explore_items": [
        # chains
        "rope chain", "figaro chain", "box chain", "curb chain", "cuban chain",
        "franco chain", "herringbone chain", "mariner chain", "wheat chain",
        "singapore chain", "byzantine chain", "snake chain", "bead chain",
        "valentino chain", "omega chain", "marine chain", "brilliantina chain",
        "perfectina chain", "criss cross chain", "anchor chain",
        "pocket watch chain", "albert chain", "fob chain",
        # bracelets
        "id bracelet", "tennis bracelet", "charm bracelet", "bangle bracelet",
        "cuban bracelet", "rope bracelet", "link bracelet", "presidential bracelet",
        "nugget bracelet", "figaro bracelet", "byzantine bracelet",
        "herringbone bracelet", "franco bracelet", "curb bracelet",
        "snake bracelet", "box bracelet", "mariner bracelet", "wheat bracelet",
        "omega bracelet",
        # rings
        "wedding band", "pinky ring", "nugget ring", "signet ring", "class ring",
        "band ring", "dome ring", "cigar band",
        # earrings
        "hoop earrings", "huggie earrings", "stud earrings", "drop earrings",
        # pendants / misc
        "medal", "religious medal", "crucifix pendant", "cross pendant",
        "charm", "locket", "cuff links", "money clip", "grillz", "chai pendant",
        "horseshoe pendant", "nugget pendant", "nameplate necklace",
        "nameplate pendant", "initial necklace",
        # scrap
        "dental gold",
        # common misspellings — the actual edge
        "braclet", "bracelett", "neckalce", "necklance", "earings", "earrigs",
        "chian", "chainn", "jewlery", "jewelery", "pendent", "pendnat",
        "solid glod", "yello gold", "karat gold", "carat gold",
    ],
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
# watches are structurally mixed-material (movement, crystal, case back, sometimes a
# steel/ceramic bezel) even when cased in solid gold, and a stated "total weight" prices
# all of that as if it were gold — so these are excluded outright, not floored/flagged
WATCH_RE = re.compile(
    r"\b(watch(es)?|wrist\s?watch(es)?|pocket\s?watch(es)?|chronograph(s)?|"
    r"timepiece(s)?|movement|watch\s?case|watch\s?band|watch\s?strap|"
    r"watch\s?head)\b", re.I)
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
# Split decimals: sellers type "13,25g" (European comma) and "12. 23 grams" (stray space
# after the point) constantly. Left alone, the leading digits are orphaned and only the
# fractional part matches GRAM_RE -- "12. 23 grams" reads as 23g, nearly doubling the
# apparent melt value and manufacturing a deal that isn't there. Rejoin them first.
#
# Guards, each load-bearing:
#   (?<![\d.,$])  the number can't already be part of a bigger one, so a price like
#                 "$1,225. 23 grams" won't glue into 225.23 -- it still reads 23g.
#   [.,]          an explicit separator is required; bare "12 23 grams" is NOT joined,
#                 because that's usually a count next to a weight ("12 rings 23 grams").
#   \d{1,2}       real thousands separators group in 3s, so this can't eat "1,500 grams".
# Thousands separators, stripped before the decimal-split pass below: "1,500 grams"
# must become 1500, not 500 (GRAM_RE would otherwise match only the trailing group).
# Requires groups of exactly 3 digits, which is what distinguishes it from the
# decimal-comma case above -- "13,25g" has 2 and is left for DECIMAL_SPLIT_RE.
THOUSANDS_RE = re.compile(
    r"(?<![\d.,$])(\d{1,3}(?:,\d{3})+)(?=(?:\.\d+)?\s*(?:g\b|gr\b|grams?\b|dwt\b|pennyweight))",
    re.I)
# A decimal point that got spaced out by a typo: "12. 23 grams", "8 . 5g".
DECIMAL_SPLIT_RE = re.compile(
    r"(?<![\d.,$])(\d+)\s*\.\s*(\d{1,2})(?=\s*(?:g\b|gr\b|grams?\b|dwt\b|pennyweight))",
    re.I)
# European decimal comma: "13,25g". Deliberately allows NO space after the comma.
# A comma FOLLOWED BY A SPACE is a list separator, not a decimal point, and the two
# were previously handled by one space-tolerant rule. That turned
#   "... Size 6, 4 grams"  into  "6.4 grams"
# inventing 2.4g of gold that doesn't exist and making a fairly priced ring look like
# a deal. A real European decimal is written closed up; a size followed by a weight
# is not. When it's ambiguous, taking the smaller number is the safe direction:
# understating weight understates melt, which loses a deal rather than buying a dud.
DECIMAL_COMMA_RE = re.compile(
    r"(?<![\d.,$])(\d+),(\d{1,2})(?=\s*(?:g\b|gr\b|grams?\b|dwt\b|pennyweight))",
    re.I)
# Ring/chain sizing that sits next to the weight and gets mistaken for it.
# Stripped before any weight parsing rather than guarded against case by case.
# Note the size's own fractional part may use a PERIOD or a written fraction, but
# never a comma: in "Size 6, 4 grams" the comma separates size from weight, so
# allowing it here would make this rule swallow the weight it exists to protect.
SIZE_CTX_RE = re.compile(
    r"\b(?:ring\s*|band\s*)?(?:size|sz)\s*[:\-]?\s*"
    r"\d{1,2}(?:\.\d{1,2}|\s*1/2|\s*½|\s*3/4|\s*1/4)?\s*[,;]?", re.I)
DWT_TO_G = 1.55517
# Weight explicitly attributed to gold (so we can price off gold, not total weight)
GOLD_WT_RES = [
    re.compile(r"(\d*\.?\d+)\s?(?:g\b|grams?)\s+of\s+(?:fine\s|pure\s|solid\s)?gold\b", re.I),
    re.compile(r"\bgold\s*(?:weight|content|wt)\b\s*[:\-]?\s*(\d*\.?\d+)\s?(?:g\b|grams?|dwt)?", re.I),
]
# An item specific that names a second, non-gold metal alongside the gold
# A second, non-gold metal sharing the listing. Every one of these inflates the
# stated weight with metal your buyer pays nothing for, so pricing the whole weight
# as gold overstates melt and manufactures a deal that was never there.
#
# This used to be checked ONLY against item specifics on deep-scanned listings, which
# meant the fast path — the great majority of rows — never tested for it at all, and
# every "14k gold and sterling silver lot" sailed straight through to the board.
# Note: "two tone" is deliberately NOT here. A tone is a colour, not a metal, and in
# jewellery it usually means yellow + white GOLD — all of it solid, all of it saleable.
# Treating it as a second metal rejected good pieces. TONE_RE handles it separately.
MIXED_METAL = re.compile(
    r"(mixed\s?metal|base\s?metal|mixed\s?lot"
    r"|\b(?:sterling|925|silver|platinum|plat|titanium|tungsten|stainless|steel|brass"
    r"|copper|pewter|rhodium|palladium)\b"
    r"|with\s?(silver|steel|platinum)"
    r"|gold\s?(and|&|/|\+)\s?(silver|steel|platinum|sterling|925))", re.I)
# Words that legitimately contain a metal name without the item containing that metal.
# Checked first so "silver tone plated" style false positives don't cost a real lot —
# and so "white gold" is never mistaken for a silver item.
MIXED_METAL_OK = re.compile(
    r"(silver\s?tone|silvertone|silver\s?plated|silver\s?colou?r"
    r"|steel\s?blue|no\s?silver|not\s?silver)", re.I)


def has_mixed_metal(text):
    """True when the listing names a second, non-gold metal in its own right."""
    if not text:
        return False
    cleaned = MIXED_METAL_OK.sub(" ", text)
    return bool(MIXED_METAL.search(cleaned))


# Small parts that are routinely a different metal on an otherwise solid gold piece:
# a steel spring bar in a clasp weighs a fraction of a gram on a 20g chain. Naming one
# is not the same as the item being mixed, and treating it as such throws away good
# listings silently — the worst kind of loss, because a rejected listing leaves no
# trace to notice later.
COMPONENT_RE = re.compile(
    r"\b(clasp|claw|lobster|spring\s?ring|jump\s?ring|bail|accent|accents|trim|inlay|"
    r"spacer|bead|tip|end\s?cap|hook|catch|backing|back|post|screw\s?back|"
    r"safety\s?chain|extender|pin|hinge|core|setting)\b", re.I)
# "two tone" / "tri colour" most often means two or three COLOURS OF GOLD (yellow,
# white, rose) — all of it solid gold and all of it saleable. Blocking the phrase
# outright was rejecting perfectly good pieces.
TONE_RE = re.compile(r"\b(two|2|tri|3|multi)[\s-]?(tone|color|colour)\b", re.I)
GOLD_COLOR_RE = re.compile(r"\b(white|yellow|rose|pink|green)\s?gold\b", re.I)
# stone words that describe something negligible or decorative rather than a set stone
STONE_MINOR_RE = re.compile(r"\b(accent|accents|chip|chips|melee)\b", re.I)
# Language that means the piece is genuinely stone-set, whatever adjectives surround
# it. "Set with three small diamonds" is a stone-set ring, and a nearby "small" must
# not buy it an exemption — the stones are still in the stated weight.
STONE_SET_RE = re.compile(
    r"\b(set\s?with|stone[\s-]?set|prong[\s-]?set|bezel[\s-]?set|pav[eé]|"
    r"cluster|solitaire|halo|eternity|channel[\s-]?set|encrusted|studded)\b", re.I)


def material_verdict(text):
    """Grade a listing's materials instead of answering yes/no.

    Three states, because the evidence genuinely comes in three strengths:

      blocked - definitional or primary. "Gold plated" is not gold; "sterling silver
                and 14k gold ring" is mostly silver. No amount of context rescues these.
      suspect - a second material is named, but in a position that suggests a minor
                component ("steel spring clasp"), or a phrase that is ambiguous by
                nature ("two tone", which is usually two colours of gold). KEPT, tagged,
                slightly demoted, and prioritised for a detail call that can settle it.
      clear   - nothing found.

    The point of the middle state is that a binary filter has to choose which way to be
    wrong, and choosing "reject" makes the error invisible: a false positive shows up on
    your board where you can flag it, while a false negative is a deal you simply never
    saw. Suspects stay visible and get resolved on evidence."""
    if not text:
        return {"state": "clear", "reason": "", "tags": []}
    tags = []

    if NOT_SOLID.search(text):
        return {"state": "blocked", "reason": "plated, filled or clad — not solid gold",
                "tags": ["plated"]}
    if WATCH_RE.search(text):
        return {"state": "blocked", "reason": "watch — structurally mixed material",
                "tags": ["watch"]}
    if EXTRA_EXCLUDE_RE and EXTRA_EXCLUDE_RE.search(text):
        return {"state": "blocked", "reason": "matched one of your own exclude words",
                "tags": ["user_exclude"]}

    cleaned = MIXED_METAL_OK.sub(" ", text)

    # --- second metal: primary material, or a minor component? ---
    metal_hits = list(MIXED_METAL.finditer(cleaned))
    if metal_hits:
        def is_component(m):
            # look a few words either side for a component noun, or a leading "with"
            lo, hi = max(0, m.start() - 28), min(len(cleaned), m.end() + 28)
            window = cleaned[lo:hi]
            # Must name an actual small part. A bare "with" is not enough: "14k gold
            # with a sterling silver clasp" is a component, "14k gold with a stainless
            # steel band" is the main body of the piece wearing the same preposition.
            return bool(COMPONENT_RE.search(window))
        if all(is_component(m) for m in metal_hits):
            tags.append("component_metal")
        else:
            names = ", ".join(sorted({m.group(0).lower() for m in metal_hits}))
            return {"state": "blocked",
                    "reason": f"names {names} as a main material — the weight isn't all gold",
                    "tags": ["mixed_primary"]}

    # --- tone words: two colours of gold, or two metals? ---
    if TONE_RE.search(text):
        if GOLD_COLOR_RE.search(text) and not metal_hits:
            pass                       # "two tone white and yellow gold" — all gold
        else:
            tags.append("tone_ambiguous")

    # --- stones ---
    stone_txt = re.sub(r"diamond[\s-]?cut", "", text, flags=re.I)
    stone_txt = re.sub(r"\b(no|without|free\s?of|minus)\s+(stone|stones|gem|gems|"
                       r"gemstone|gemstones|diamond|diamonds)\b", "", stone_txt, flags=re.I)
    stone_txt = re.sub(r"\b(stone|gem|diamond)[\s-]?free\b", "", stone_txt, flags=re.I)
    sm = HAS_STONE.search(stone_txt)
    if sm:
        lo, hi = max(0, sm.start() - 28), min(len(stone_txt), sm.end() + 28)
        if STONE_SET_RE.search(stone_txt):
            return {"state": "blocked",
                    "reason": f"stone-set piece ({sm.group(0).lower()}) — stated weight isn't all gold",
                    "tags": ["stones"]}
        if STONE_MINOR_RE.search(stone_txt[lo:hi]):
            tags.append("stone_accent")
        else:
            return {"state": "blocked",
                    "reason": f"stones present ({sm.group(0).lower()}) — stated weight isn't all gold",
                    "tags": ["stones"]}

    if tags:
        why = {"component_metal": "another metal named, but only on a small component",
               "tone_ambiguous": "two/tri-tone — may be two colours of gold, may be two metals",
               "stone_accent": "accent stones mentioned — likely minor, but weight may include them"}
        return {"state": "suspect", "reason": "; ".join(why[t] for t in tags), "tags": tags}
    return {"state": "clear", "reason": "", "tags": []}


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
    plain grams, pennyweight (dwt), and split decimals where the separator is a comma
    or has stray spaces around it (13,25g / 12. 23 grams / 8 . 5g). Fractions are
    checked first so '1/2 gram' isn't misread as 2 grams."""
    if not text:
        return None
    # Drop ring-size phrases first: "Size 6, 4 grams" must read as 4g, not 6.4g.
    text = SIZE_CTX_RE.sub(" ", text)
    text = THOUSANDS_RE.sub(lambda m: m.group(1).replace(",", ""), text)
    text = DECIMAL_SPLIT_RE.sub(r"\1.\2", text)
    text = DECIMAL_COMMA_RE.sub(r"\1.\2", text)
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
    # Same normalisation as extract_grams, kept deliberately identical: a size/decimal
    # rule that only applies on one of the two paths is a silent disagreement about
    # what a listing weighs, depending on which parser happened to reach it first.
    text = SIZE_CTX_RE.sub(" ", text)
    text = THOUSANDS_RE.sub(lambda m: m.group(1).replace(",", ""), text)
    text = DECIMAL_SPLIT_RE.sub(r"\1.\2", text)
    text = DECIMAL_COMMA_RE.sub(r"\1.\2", text)
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
    """Kept as the single yes/no gate, but now only 'blocked' means no. Suspects pass
    through so they can be seen, tagged and resolved rather than silently dropped."""
    return material_verdict(text)["state"] != "blocked"


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
    for k in ("query_prior_runs", "max_revives", "reserve_runs",
              "retire_rel_median", "retire_batch_cap", "retire_min_live",
              "ag_fee", "ag_optional_min", "ag_optional_max", "ag_required_min",
              "ag_score_penalty", "ag_confirm_min_score", "ag_detail_share",
              "suspect_score_penalty", "reject_sample_max",
              "strong_score",
              "payout_pct", "trap_under_pct", "max_detail_calls",
              "offer_max_over_pct", "offer_target_under_pct",
              "min_feedback_pct", "min_feedback_score", "results_per_query",
              "daily_call_budget", "runs_per_day", "fb_half_life_days",
              "fb_weight_span", "fb_seller_block_bad", "explore_frac",
              "promote_min_deals", "retire_min_runs", "starvation_cap", "exploit_share",
              "history_max", "spot_stale_max_hours"):
        if isinstance(s.get(k), (int, float)):
            cfg[k] = s[k]
    # per-key merge so you can override one weight without restating the whole table
    if isinstance(s.get("query_weights"), dict):
        for k, v in s["query_weights"].items():
            if isinstance(v, (int, float)):
                cfg["query_weights"][k] = float(v)
    for k in ("explore_enabled", "retire_protect_liked"):
        if isinstance(s.get(k), bool):
            cfg[k] = s[k]
    if s.get("ag_mode") in ("require", "prefer", "off"):
        cfg["ag_mode"] = s["ag_mode"]
    for k in ("pinned_queries", "disabled_queries", "revived_queries"):
        if isinstance(s.get(k), list):
            cfg[k] = [str(q).strip() for q in s[k] if str(q).strip()]
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
    # ---- one-shot manual run override ----
    # The dashboard's Run drawer writes manual_queries + manual_run_id into
    # settings.json, then dispatches the workflow. The engine consumes it exactly once:
    # the id it last honoured is recorded in query_stats.json, so the scheduled sweeps
    # that follow go back to normal rotation instead of repeating your ad-hoc sweep
    # forever. Done this way because the workflow deploys to Pages and never commits
    # back to the repo, so the engine can't clear the flag by editing settings itself.
    if isinstance(s.get("manual_queries"), list) and s.get("manual_run_id"):
        mq = [q.strip() for q in s["manual_queries"] if isinstance(q, str) and q.strip()]
        if mq:
            cfg["manual_queries"] = mq
            cfg["manual_run_id"] = str(s["manual_run_id"])
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
        # defensive per-event parsing: one malformed tap (bad ts, wrong types) must
        # never crash the sweep — skip it and keep learning from the rest
        try:
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
        except Exception:
            continue

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
    disabled = set(cfg.get("disabled_queries") or ())
    return [q for q in pool
            if q not in disabled
            and known.get(q, {}).get("status") not in ("retired", "promoted")]


def feedback_counts(cfg):
    """👍/👎 totals per search term, undecayed and WEIGHTED BY REASON.

    Separate from load_feedback()'s decayed multipliers on purpose: those nudge how a
    listing is *alerted*, these decide whether a search keeps its slot.

    The reason matters, and it used to be thrown away here. Every 👎 was counted at
    full weight regardless of why it was given, which meant the fb_effects table —
    written precisely so that "overpriced" and "not my style" never punish anything —
    was honoured for alerting and silently ignored for rotation and retirement, the
    decisions that actually kill a search term.

    That mattered most for the defect categories. When a listing is junk because the
    PARSER let it through (mixed materials, a misread weight, plating we failed to
    catch), the search term did its job — it found a solid-gold listing matching the
    words asked for. Punishing the term for a filter bug retires good terms and leaves
    the bug in place, and the retirement looks evidence-based while measuring nothing
    but our own defects. Those verdicts now carry zero rotation weight and are routed
    to the parsing-defect backlog instead, where they belong."""
    counts = {}
    eff = cfg.get("fb_effects", {})
    try:
        with open(cfg.get("feedback_file", "feedback.json")) as f:
            events = json.load(f).get("events", [])
    except Exception:
        return counts
    for e in events:
        if not isinstance(e, dict):
            continue
        q = (e.get("query") or "").strip()
        if not q:
            continue
        c = counts.setdefault(q, {"up": 0, "down": 0})
        if e.get("verdict") == "good":
            c["up"] += 1
            continue
        cat = e.get("category") or "other"
        # An uncategorised 👎 is a plain "this search found junk" and keeps full weight.
        w = 1.0 if e.get("category") is None else float(
            (eff.get(cat) or {}).get("query", 0.0))
        c["down"] += w
    return counts


def build_defect_backlog(cfg, deep_drops=None):
    """Collect evidence that our own filters are the problem, for manual review.

    Two sources, both meaning "this shouldn't have reached the board":
      1. your 👎 taps whose reason is a defect category (plated / weight / stones /
         mixed) — cases the filters missed and you caught by eye
      2. listings the engine itself dropped on the post-detail material re-check

    This file is EVIDENCE ONLY. Nothing here ever auto-edits a regex, a filter or a
    score: a rule that rewrites itself from noisy signals will happily learn its way
    into excluding your best listings, and you'd have no way to see it happen. The
    backlog tells you which pattern is leaking and how often, and you change the rule
    deliberately or not at all."""
    from collections import Counter
    out = {"generated": datetime.now(timezone.utc).isoformat(),
           "by_category": {}, "engine_drops": [], "samples": []}
    defect_cats = set(cfg.get("defect_categories", []))
    try:
        with open(cfg.get("feedback_file", "feedback.json")) as f:
            events = json.load(f).get("events", [])
    except Exception:
        events = []
    cats = Counter()
    for e in events:
        if not isinstance(e, dict) or e.get("verdict") == "good":
            continue
        cat = e.get("category")
        if cat in defect_cats:
            cats[cat] += 1
            if len(out["samples"]) < 60:
                out["samples"].append({"category": cat, "query": e.get("query", ""),
                                       "id": e.get("id", ""), "ts": e.get("ts")})
    out["by_category"] = dict(cats)
    drops = deep_drops or []
    out["engine_drops"] = drops[:60]
    out["engine_drop_reasons"] = dict(Counter(d.get("reason", "?") for d in drops))
    out["total"] = sum(cats.values()) + len(drops)
    try:
        with open(cfg.get("defects_file", "parsing_defects.json"), "w") as f:
            json.dump(out, f, indent=2)
    except Exception as e:
        print(f"[defects] couldn't write backlog: {e}")
    return out


def query_value(st, cfg, fb=None):
    """Points-per-run for one search. Single definition of 'is this search earning
    its slot', used for both ranking and retirement so the two can never disagree."""
    w = cfg.get("query_weights", {})
    fb = fb or {}
    pts = (w.get("strong_ag", 3.0)   * st.get("strong_ag", 0)
         + w.get("strong", 1.5)      * st.get("strong", 0)
         + w.get("weak_ag", 1.5)     * st.get("weak_ag", 0)
         + w.get("weak", 0.0)        * st.get("weak", 0)
         + w.get("trap", -1.0)       * st.get("traps", 0)
         + w.get("thumbs_up", 3.0)   * fb.get("up", 0)
         + w.get("thumbs_down", -3.0)* fb.get("down", 0))
    return pts / (st.get("runs", 0) + max(1, cfg.get("query_prior_runs", 3)))


def real_deals(st):
    """Deals that weren't traps — the old retirement rule counted traps as signs of
    life, so a search returning nothing but traps was immortal."""
    return sum(st.get(k, 0) for k in ("strong", "strong_ag", "weak", "weak_ag")) or st.get("deals", 0)


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

    # One-shot manual override: run exactly the terms picked in the Run drawer, once.
    mrid = cfg.get("manual_run_id") or ""
    if cfg.get("manual_queries") and mrid and mrid != stats["meta"].get("manual_run_done"):
        mq = list(dict.fromkeys(cfg["manual_queries"]))
        stats["meta"]["manual_run_done"] = mrid
        stats["meta"]["manual_run_at"] = datetime.now(timezone.utc).isoformat()
        print(f"[manual] one-shot run of {len(mq)} hand-picked term(s) (id {mrid[:8]}) "
              f"— normal rotation resumes next sweep")
        return mq, [], sorts

    rpd = estimate_runs_per_day(cfg)
    # Leave room for unscheduled manual sweeps: divide the day's budget by the runs we
    # expect PLUS a reserve, so ad-hoc "run now" clicks come out of slack rather than
    # out of tomorrow's quota. Derived from the detected cadence, never a fixed number.
    reserve = max(0, int(cfg.get("reserve_runs", 2)))
    per_run = cfg["daily_call_budget"] / (rpd + reserve)
    pages = max(1, -(-cfg["results_per_query"] // 50))          # ceil, calls per query per sort
    slots = int((per_run - cfg["max_detail_calls"] - 3) / (len(sorts) * pages))
    slots = max(4, slots)

    qs = stats["queries"]
    pinned = [q for q in (cfg.get("pinned_queries") or []) if q]
    disabled = set(cfg.get("disabled_queries") or ())
    # core = your settings list + explorers that earned promotion, minus retirees.
    # A pinned query survives auto-retirement; a disabled one is dropped outright,
    # whatever the engine thinks of its numbers.
    promoted = [q for q, st in qs.items()
                if st.get("status") == "promoted" and q not in cfg["queries"]]
    active = [q for q in list(cfg["queries"]) + promoted + pinned
              if q not in disabled
              and (q in pinned or qs.get(q, {}).get("status") != "retired")]
    active = list(dict.fromkeys(active))          # de-dupe, keep order

    fbc = feedback_counts(cfg)
    def value(q):
        # query_mult is intentionally unused here: 👍/👎 are now explicit, full-weight
        # terms inside query_value() rather than a decayed multiplier, so applying both
        # would count your feedback twice. load_feedback() still drives alerting.
        return query_value(qs.get(q, {}), cfg, fbc.get(q))

    explore = []
    if cfg["explore_enabled"]:
        pool = explore_candidates(cfg, stats)
        n_ex = min(len(pool), max(0, round(slots * cfg["explore_frac"])))
        if n_ex:
            pool.sort(key=lambda q: (qs.get(q, {}).get("runs", 0),
                                     qs.get(q, {}).get("last_rc", 0)))
            explore = pool[:n_ex]

    # two-tier core fill: proven earners are never evicted by rotation —
    # exploit_share of slots goes to top value, the rest to least-recently-run.
    # Pinned queries are seated first and never compete for the remaining slots.
    core_slots = max(0, slots - len(explore))
    pins = [q for q in pinned if q in set(active)][:core_slots]
    # Starvation guarantee: anything that hasn't run in starvation_cap sweeps is
    # seated right after the pins, ahead of ranking. Without this, a low-scoring
    # search can be permanently crowded out and never gather the runs it needs to
    # either earn its place or be retired on evidence — it just sits unjudged.
    cap = max(1, int(cfg.get("starvation_cap", 6)))
    starved = [q for q in active
               if q not in set(pins)
               and (rc - qs.get(q, {}).get("last_rc", -10**9)) > cap][:max(0, core_slots - len(pins))]
    seated = set(pins) | set(starved)
    remaining = [q for q in active if q not in seated]
    open_slots = max(0, core_slots - len(pins) - len(starved))
    ranked = sorted(remaining, key=lambda q: -value(q))
    n_top = min(open_slots, max(1, int(open_slots * cfg.get("exploit_share", 0.7)))) if open_slots else 0
    core = pins + starved + ranked[:n_top]
    rest = sorted((q for q in remaining if q not in set(core)),
                  key=lambda q: qs.get(q, {}).get("last_rc", -10**9))
    core += rest[:max(0, open_slots - n_top)]

    est = (len(core) + len(explore)) * len(sorts) * pages + cfg["max_detail_calls"]
    print(f"[budget] ~{per_run:.0f} calls/run available ({cfg['daily_call_budget']}/day "
          f"÷ ~{rpd}+{reserve} runs) -> {slots} query slots · sorts: {'+'.join(sorts)} · "
          f"{len(core)} core ({len(pins)} pinned, {len(starved)} starved) + {len(explore)} explore · "
          f"{len(disabled)} disabled · est {est} calls this run")
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
    # Break each hit down by kind so the dashboard can show what a search actually
    # brings back, not just how much. A search returning 20 traps is not the same
    # as one returning 20 strong deals, and the bar chart should say so at a glance.
    strong_at = cfg.get("strong_score", 70)
    for r in rows:
        st = qs.get(r.get("query") or "")
        if not st:
            continue
        ag = bool(r.get("auth_guaranteed"))
        if r["trap"]:
            st["traps"] = st.get("traps", 0) + 1
        elif r.get("offer_only"):
            # tracked, but deliberately NOT counted as a deal: these are over melt as
            # listed and only become deals if a seller accepts an offer. Counting them
            # would promote a query on listings that never actually cleared the bar.
            st["offers"] = st.get("offers", 0) + 1
        else:
            st["deals"] = st.get("deals", 0) + 1
            key = ("strong" if (r.get("score") or 0) >= strong_at else "weak") + ("_ag" if ag else "")
            st[key] = st.get(key, 0) + 1
        if ag:
            st["ag"] = st.get("ag", 0) + 1
        if r.get("mixed_lot"):
            st["mixed"] = st.get("mixed", 0) + 1

    pinned = set(cfg.get("pinned_queries") or ())
    disabled = set(cfg.get("disabled_queries") or ())
    revive = set(cfg.get("revived_queries") or ())
    fbc = feedback_counts(cfg)
    promoted, retired = [], []
    for q, st in qs.items():
        # your explicit calls override the engine's automatic judgement in both directions
        if q in pinned:
            st["pinned"] = True
            if st.get("status") == "retired":
                st["status"] = None       # un-retire: you asked for this one back
            continue
        # "Bring back" from the dashboard: wipe the record and let it compete again
        # from scratch. Capped, because a search that keeps failing shouldn't be able
        # to consume a slot forever on repeat second chances.
        if q in revive and st.get("status") == "retired":
            if st.get("revives", 0) < cfg.get("max_revives", 2):
                st.update({"status": None, "runs": 0, "deals": 0, "traps": 0,
                           "strong": 0, "strong_ag": 0, "weak": 0, "weak_ag": 0,
                           "ag": 0, "mixed": 0})
                st["revives"] = st.get("revives", 0) + 1
                st.pop("retired_score", None)
                print(f"[explore] REVIVED {q!r} — fresh trial "
                      f"({st['revives']}/{cfg.get('max_revives', 2)})")
                continue
        st.pop("pinned", None)
        if q in disabled:
            st["status"] = "disabled"
            continue
        if st.get("status") == "disabled":
            st["status"] = None           # re-enabled from the dashboard
        if (st.get("origin") == "explore" and st.get("status") != "promoted"
                and real_deals(st) >= cfg["promote_min_deals"]):
            st["status"] = "promoted"
            promoted.append(q)
        # Retirement is decided in a second pass below, once every term's score for
        # this sweep is known — a relative rule can't be evaluated one term at a time.
    # ---- retirement pass: relative, capped, and floored ----
    # Judged against the live pool's own median so the bar tracks actual market
    # conditions instead of a number that was right on the day it was typed.
    #
    # Retirement only applies to genuinely experimental terms: still-trialing explore
    # queries that haven't earned promotion yet. Core queries and anything already
    # promoted are permanent from here on — they proved themselves (or were hand-picked
    # to begin with) and shouldn't get culled later just because the relative bar moved
    # against them on a slow week. This also keeps the median itself honest: mixing a
    # 375-run established query into the same pool as a 3-run new candidate would judge
    # brand-new terms against a bar built mostly from established ones' momentum.
    live = {q: st for q, st in qs.items()
            if st.get("status") not in ("retired", "disabled", "promoted")
            and st.get("origin") != "core"
            and q not in pinned}
    scores = sorted(query_value(st, cfg, fbc.get(q)) for q, st in live.items())
    median = scores[len(scores) // 2] if scores else 0.0
    # A negative or zero median means the whole pool is struggling (dry market, gold
    # spike compressing every margin). Culling the bottom of an already-bad pool just
    # shrinks your coverage right when you need breadth most, so only the truly dead
    # — terms that have never found a single real deal — go in that case.
    rel = float(cfg.get("retire_rel_median", 0.35))
    bar = median * rel if median > 0 else None
    protect_liked = bool(cfg.get("retire_protect_liked", True))

    cand = []
    for q, st in live.items():
        if st.get("runs", 0) < cfg["retire_min_runs"]:
            continue
        fb = fbc.get(q) or {}
        if protect_liked and fb.get("up", 0) > fb.get("down", 0):
            continue                       # you've vouched for this one
        score = query_value(st, cfg, fb)
        if real_deals(st) == 0:
            cand.append((q, score, "no real deals in %d runs" % st["runs"]))
        elif bar is not None and score < bar:
            cand.append((q, score, f"score {score:.2f} < {bar:.2f} (35% of pool median)"))

    cand.sort(key=lambda t: t[1])          # worst first
    n_live = len(live)
    floor = int(cfg.get("retire_min_live", 12))
    room = max(0, n_live - floor)
    for q, score, why in cand[:min(int(cfg.get("retire_batch_cap", 2)), room)]:
        qs[q]["status"] = "retired"
        qs[q]["retired_score"] = round(score, 3)
        qs[q]["retired_why"] = why
        retired.append(q)

    for q in promoted:
        print(f"[explore] PROMOTED to core: {q!r} — add it to your query list to lock it in")
    for q in retired:
        print(f"[stats] retired ({qs[q].get('retired_why')}): {q!r}")
    if cand and not retired:
        print(f"[stats] {len(cand)} term(s) below the bar but kept "
              f"({n_live} live, floor {floor}) — pool too small to cull")
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


def auth_guaranteed(item):
    """True if eBay itself qualifies this item for the Authenticity Guarantee program
    at no cost to the buyer. Checked against both response shapes eBay uses: the
    item_summary/search "qualifiedPrograms" array, and the item-detail
    "authenticityGuarantee" container (present on getItem responses)."""
    if "AUTHENTICITY_GUARANTEE" in (item.get("qualifiedPrograms") or []):
        return True
    return bool(item.get("authenticityGuarantee"))


def ag_status(item, cfg, detail=None):
    """Resolve what authentication is available on a listing, and what it costs.

    Returns (state, fee, confirmed):
      state     - "included" | "optional" | "none" | "unknown"
      fee       - dollars you must pay to get the guarantee (0.0 unless "optional")
      confirmed - True when this came from eBay's own data rather than a price-band guess

    The distinction that matters: eBay only returns the `addonServices` container on
    getItem/getItemByLegacyId. The search response (ItemSummary) carries
    `qualifiedPrograms` and nothing else, so from a search hit alone we can prove AG is
    *included* but we can NOT tell whether the paid add-on is available. That case comes
    back as "unknown" with a band-based guess, and is only upgraded to a confirmed
    "optional"/"none" by spending a detail call. Never treat an unconfirmed guess as a
    guarantee — that is exactly the mistake this whole feature exists to prevent."""
    fee_default = float(cfg.get("ag_fee", 40.0))

    # --- confirmed paths: eBay's own data ---
    for svc in (detail or item).get("addonServices") or []:
        if (svc.get("serviceType") or "") != "AUTHENTICITY_GUARANTEE":
            continue
        sel = (svc.get("selection") or "").upper()
        raw = ((svc.get("serviceFee") or {}).get("value"))
        try:
            fee = float(raw) if raw is not None else fee_default
        except (TypeError, ValueError):
            fee = fee_default
        if sel == "REQUIRED":
            # mandatory AG is eBay-funded for the buyer; a nonzero fee here would be
            # eBay changing the deal, so honour whatever they actually returned
            return ("included", round(fee, 2), True)
        if sel == "OPTIONAL":
            return ("optional", round(fee, 2), True)

    if auth_guaranteed(detail or item) or auth_guaranteed(item):
        return ("included", 0.0, True)

    # A detail call came back with no AG addon and no AG container: that is a real,
    # confirmed "you cannot buy authentication for this item".
    if detail is not None:
        return ("none", 0.0, True)

    # --- unconfirmed: infer from the published price bands ---
    # Only ever a hint for prioritising which listings are worth a detail call.
    try:
        price = float((item.get("price") or {}).get("value", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    ship = _ship_cost(item)
    total = price + ship
    if total >= float(cfg.get("ag_required_min", 500.0)):
        # should have been caught by qualifiedPrograms; if it wasn't, the listing is
        # probably AG-ineligible (branded-but-not-partner, non-continental-US seller)
        return ("unknown", 0.0, False)
    if float(cfg.get("ag_optional_min", 200.0)) <= total <= float(cfg.get("ag_optional_max", 499.99)):
        return ("unknown", round(fee_default, 2), False)
    return ("none", 0.0, False)   # under the floor: AG isn't offered at any price


def search(token, query, limit, sort="price", cfg=None):
    out, offset = [], 0
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
    # Deliberately unrestricted beyond format/condition/location. Authenticity Guarantee
    # and mixed-karat lots are NOT filtered here: narrowing the eBay query throws away
    # listings we can never get back, and the AG filter in particular also requires
    # deliveryCountry + deliveryPostalCode or eBay silently returns nothing at all.
    # Both are tagged per row instead and shown/hidden in the dashboard, the same way
    # traps are — collect wide, filter at the glass.
    base_filter = ("buyingOptions:{FIXED_PRICE},conditions:{USED|UNSPECIFIED},"
                   "itemLocationCountry:US")
    while offset < limit:
        page = min(50, limit - offset)
        r = requests.get("https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers=headers,
            params={"q": query,
                    "filter": base_filter,
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
                  gold_specific=False, mixed_karats=None, detail=None):
    if not seller_ok(item, cfg):
        return None
    price = float((item.get("price") or {}).get("value", 0) or 0)
    if price <= 0 or grams <= 0:
        return None
    ship = _ship_cost(item)
    page_per_g = PURITY[karat] * spot24

    # ---- authentication: what's available, what it costs, which way you'd buy ----
    # A listing with a purchasable guarantee gives you two genuinely different deals:
    # buy it unprotected at face cost, or pay the fee and buy it protected. Score BOTH
    # and take whichever is better, which is what a rational buyer does anyway.
    #
    # Scoring only the fee-inclusive version created a cliff: a thin deal whose margin
    # couldn't absorb the fee got quietly re-priced without it and outranked a healthier
    # deal that correctly carried the fee. Taking the max of two monotonic branches
    # removes the discontinuity instead of papering over it.
    mat = material_verdict(f"{title_text or item.get('title','')}")
    ag_state, ag_fee, ag_confirmed = ag_status(item, cfg, detail=detail)
    ag = (ag_state == "included")
    ag_mode = cfg.get("ag_mode", "prefer")
    tax = 1 + cfg["tax_pct"]

    base_cost = (price + ship) * tax
    raw_all_in_g = base_cost / grams
    # Over melt as listed. Normally that's the end of it — but a listing that takes
    # offers isn't priced yet, it's just anchored. If the ask is close enough that a
    # realistic offer clears melt, it belongs on the board as a deal you'd have to
    # negotiate into, not a listing that silently disappeared. It carries a NEGATIVE
    # score (how far under water the ask is) so it sorts below every real deal and can
    # never win an alert, while still being visible and sortable alongside them.
    offer_only = False
    if raw_all_in_g >= page_per_g:
        if "BEST_OFFER" not in (item.get("buyingOptions") or []):
            return None
        over_by = (raw_all_in_g - page_per_g) / page_per_g
        if over_by > float(cfg.get("offer_max_over_pct", 0.25)):
            return None                    # no realistic offer bridges this gap
        offer_only = True
    raw_under_by = (page_per_g - raw_all_in_g) / page_per_g

    # trap on the RAW discount, never the fee-adjusted one: "too good to be true" is a
    # property of the seller's asking price, and letting a $40 fee shrink it would pull
    # plated fakes back under the threshold and present them as ordinary deals
    is_trap = raw_under_by >= cfg["trap_under_pct"]

    # A suspect is worth less than a clear listing of identical economics, but far more
    # than nothing — which is what rejecting it was worth. Small and configurable.
    mat_pen = int(cfg.get("suspect_score_penalty", 8)) if mat["state"] == "suspect" else 0
    pen_none = int(cfg.get("ag_score_penalty", 12))
    pen_unk = pen_none // 2                # unconfirmed guess: half the confidence
    def _sc(ub, pen):
        # An offer-only listing has no positive margin to score yet — what matters is
        # how far the ask has to come down. Score it as the negative of that gap, so
        # -8 reads "8% over melt, needs an offer" and sorts below every real deal
        # while still ranking offer candidates sensibly against each other.
        if offer_only:
            return -min(100, max(1, round(-ub * 100)))
        base = deal_score(max(ub, 0.0), cfg["payout_pct"], cfg["trap_under_pct"])
        if ag_mode in ("prefer", "require") and not is_trap:
            base = max(0, base - pen)
        return max(0, base - mat_pen) if not is_trap else base

    # candidate ways to buy this: (state, cost, score penalty, fee paid)
    if ag_mode == "off":
        opts = [("off", base_cost, 0, 0.0)]
    elif ag_state == "included":
        opts = [("included", base_cost, 0, 0.0)]
    elif ag_state in ("optional", "unknown"):
        opts = [(ag_state, base_cost + ag_fee * tax,
                 0 if ag_state == "optional" else pen_unk, ag_fee)]
        if ag_mode != "require":           # buying it unprotected is still on the table
            opts.append(("none", base_cost, pen_none, 0.0))
    else:                                  # no authentication available at any price
        if ag_mode == "require":
            return None
        opts = [("none", base_cost, pen_none, 0.0)]

    best = None
    for st, c, pen, fee in opts:
        ub = (page_per_g - c / grams) / page_per_g
        cand = (_sc(ub, pen), st, c, ub, fee)
        # tie-break on real margin: when two ways of buying score the same (both
        # floored at 0, say), present the one that actually leaves money on the table
        if best is None or (cand[0], cand[3]) > (best[0], best[3]):
            best = cand
    score, buy_state, cost, under_by, charge_ag = best
    all_in_g = cost / grams
    # What to actually offer. Aimed at the same believable margin real deals land in,
    # working backwards from melt through the same cost stack (shipping, tax, and the
    # authentication fee if this is a listing you'd want protected).
    offer_price = offer_profit = None
    if offer_only:
        target_under = float(cfg.get("offer_target_under_pct", 0.10))
        target_cost = page_per_g * (1 - target_under) * grams
        # charge_ag is whichever fee the chosen buy path carries, so the offer accounts
        # for it instead of quoting a price that only works unauthenticated
        offer_price = round(max(0.0, (target_cost - charge_ag * tax) / tax - ship), 2)
        offer_profit = round(page_per_g * grams * cfg["payout_pct"] - target_cost, 2)
    # true when paying for the guarantee would wipe out the margin, i.e. this is only
    # a deal if you're willing to take it unauthenticated
    ag_fee_kills_margin = (ag_state in ("optional", "unknown") and ag_fee > 0
                           and (page_per_g - (base_cost + ag_fee * tax) / grams) <= 0)

    title = title_text or item.get("title", "")
    reason = ""
    if karat == 24 and BAR_RE.search(title):    # 24k "bullion" under melt is almost always fake
        is_trap = True
        reason = "24k bar/bullion priced under melt — almost always counterfeit or plated"
    elif is_trap:
        # discount this deep past melt isn't a real deal; explain the most likely cause
        if PLATED_RE.search(title):
            reason = (f"{round(raw_under_by*100)}% under melt and the title shows plating shorthand "
                      f"— the weight is base metal with a thin gold layer, not solid gold")
        elif raw_under_by >= 0.85:
            reason = (f"{round(raw_under_by*100)}% under melt — that's not a discount, the weight "
                      f"is real but the metal almost certainly isn't solid gold (plated/filled)")
        else:
            reason = (f"{round(raw_under_by*100)}% under melt — too far below spot to be a genuine "
                      f"deal; likely wrong karat, inflated weight, or a non-gold core")
    # soft flag: a steep-but-not-trap discount is the kind most often caused by a
    # wrong/total weight, so prompt a closer look without hiding it
    mixed = bool(mixed_karats and len(mixed_karats) > 1)
    verify = (not is_trap) and (raw_under_by >= 0.40 or mixed)

    if photos is None:
        # additionalImages is the real gallery count; thumbnailImages is ~always a
        # single thumb, which made every fast-path listing look like "2 photos"
        photos = 1 + len(item.get("additionalImages") or item.get("thumbnailImages") or [])
    payout = page_per_g * grams * cfg["payout_pct"]
    seller = item.get("seller") or {}
    s_score = int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None else None
    s_pct = float(seller["feedbackPercentage"]) if seller.get("feedbackPercentage") else None

    # mixed-grade items: excluded by default (currently steering clear of lots /
    # Mixed-karat lots are kept and priced at the lowest karat present (a conservative
    # floor), then flagged so the dashboard can show or hide them. They used to be
    # dropped outright, which silently discarded genuinely good scrap lots.
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
        if 0.05 <= raw_under_by <= 0.35:
            why.append("believable margin, not too-good")
        if ag_state == "included":
            why.insert(0, "eBay authenticates this before it ships")
        elif ag_state == "optional" and ag_confirmed:
            why.insert(0, f"authentication can be added for ${ag_fee:.0f}")
    deal_why = ", ".join(why[:3])
    if offer_only:
        # lead with the ask, since that's the whole action item on these
        deal_why = (f"offer ${offer_price:,.0f} to clear melt "
                    f"(asking {abs(round(raw_under_by*100, 1))}% over)"
                    + (f" · {deal_why}" if deal_why else ""))



    return {
        "score": score,
        # Score on melt alone, before any authentication penalty. Needed because the
        # penalty must never gate the detail call that would REMOVE the penalty: using
        # the penalised score to decide what's worth confirming is a deadlock — the
        # listing is docked for being unconfirmed, the docking drops it under the
        # confirmation floor, so it stays unconfirmed forever.
        "melt_score": deal_score(raw_under_by, cfg["payout_pct"], cfg["trap_under_pct"]),
        "under_pct": round(under_by * 100, 1),
        "raw_under_pct": round(raw_under_by * 100, 1),
        "trap": is_trap, "trap_reason": reason, "deal_why": deal_why,
        "verify": verify, "gold_wt": gold_specific, "auth_guaranteed": ag,
        # AG block: state drives the badge, ag_fee drives the maths, ag_confirmed says
        # whether this is eBay's word or our price-band guess. The UI must show the
        # difference — an unconfirmed guess is not a guarantee.
        # Material grade: "clear" or "suspect". A suspect is a listing we chose to show
        # you rather than throw away on a guess — see material_verdict().
        "material": mat["state"], "material_why": mat["reason"], "material_tags": mat["tags"],
        "ag_state": ag_state, "ag_buy_as": buy_state, "ag_fee": round(charge_ag, 2),
        "ag_fee_quoted": round(ag_fee, 2), "ag_confirmed": ag_confirmed,
        "ag_fee_kills_margin": ag_fee_kills_margin,
        "mixed_lot": mixed, "mixed_note": mixed_note,
        "offer": "BEST_OFFER" in (item.get("buyingOptions") or []),
        # offer_only: priced over melt as listed, but takes offers and is close enough
        # that offer_price would clear melt. Carries a negative score; never alerted on.
        "offer_only": offer_only,
        "offer_price": offer_price, "offer_profit": offer_profit,
        "seller_pct": s_pct, "seller_score": s_score, "seller_user": seller.get("username", ""), "photos": photos,
        "id": item.get("itemId", ""),
        "karat": f"{karat}K", "grams": grams, "price": round(price, 2), "ship": round(ship, 2),
        "all_in_per_g": round(all_in_g, 2), "page_per_g": round(page_per_g, 2),
        "raw_all_in_per_g": round(raw_all_in_g, 2),
        # net of the authentication fee when one has to be paid — this is the number
        # that actually lands in your pocket, not the pre-fee melt spread
        "profit": round(payout - cost, 2),
        "raw_profit": round(payout - base_cost, 2),
        "title": title[:110], "url": item.get("itemWebUrl", ""),
        "listed": item.get("itemCreationDate", ""),
    }


def evaluate(item, spot24, cfg, detail=None):
    """Title-only evaluation. `detail` is optional and carries ONLY the authentication
    facts: it's passed when a getItem call has already confirmed AG status but the
    deep-scan re-read couldn't recover a weight (no grams in the description). Without
    it, a confirmed row would be re-priced down the unconfirmed path and end up wearing
    a "guaranteed" badge over a score computed from a price-band guess."""
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
                         gold_specific=bool(gold_g), mixed_karats=ks, detail=detail)


def deep_disqualifies(item, detail):
    """Re-check materials against the full listing once a detail payload is in hand.

    The fast path can only read the title, so a listing titled "14k Gold Ring 5g" whose
    description says "with sterling silver band" passes every title filter there is.
    Any time we've already paid for a getItem call (AG confirmation, weight recovery),
    the description is sitting right there and the check costs nothing extra.

    Returns a short reason string when the listing should be dropped, else None."""
    if not detail:
        return None
    desc = strip_html(detail.get("description", ""))
    aspects = " ".join(
        f"{a.get('name','')}: {' '.join(a.get('values', []))}"
        for a in (detail.get("localizedAspects") or [])
    )
    blob = f"{item.get('title','')} {aspects} {desc}"
    # Same graded verdict as the title path, deliberately. Running the blunt check here
    # would undo the grading: a chain kept as a suspect because its only other metal was
    # a clasp would be killed by the description mentioning that same clasp again.
    v = material_verdict(blob)
    return v["reason"] if v["state"] == "blocked" else None


def needs_description(item, cfg):
    title = item.get("title", "")
    if not is_solid_no_stones(title):
        return False
    if not karat_from_text(title):
        return False
    if extract_grams(title):
        return False
    price = float((item.get("price") or {}).get("value", 0) or 0)
    return price > 0


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
    if has_mixed_metal(aspects):
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
                         photos=photos, gold_specific=bool(gold_g), mixed_karats=ks,
                         detail=detail)


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
    if row.get("offer_only"):
        # negative by design (how far over melt the ask sits) — clamping to 0 here would
        # flatten every offer candidate into the break-even pile and lose the ordering
        row["score"] = max(-100, min(-1, round(row["score"] * qm * sm)))
    else:
        row["score"] = max(0, min(100, round(row["score"] * qm * sm)))


def collect(token, queries, spot24, cfg, sort, deep):
    # sort can be one sort string or a list of them. Sweeping both "price"
    # (cheapest/underpriced) and "newlyListed" (fresh) catches deals that a
    # single sort would miss — the cheapest list never shows a brand-new
    # mid-priced bargain, and the newest list never shows an old underpriced one.
    sorts = sort if isinstance(sort, (list, tuple)) else [sort]
    rows, seen, candidates = [], set(), []
    # Every listing we throw away on materials, with the reason and enough of the
    # listing to judge it by eye. Without this, a filter that is too strict looks
    # exactly like a quiet market: you can flag a bad listing that reaches the board,
    # but you can never flag one that never arrived.
    rejects = []
    # itemId -> raw search item, so the AG confirmation pass below can re-price a row
    # against its original listing without a second search call
    by_id = {}
    for q in queries:
        for srt in sorts:
            print(f"Searching ({srt}): {q}")
            for item in search(token, q, cfg["results_per_query"], sort=srt, cfg=cfg):
                iid = item.get("itemId")
                if iid in seen:
                    continue
                seen.add(iid)
                mv = material_verdict(item.get("title", ""))
                if mv["state"] == "blocked":
                    # only worth reviewing if it would otherwise have been a deal:
                    # a weightless or overpriced listing tells you nothing about the filter
                    if len(rejects) < int(cfg.get("reject_sample_max", 120)):
                        pr = float((item.get("price") or {}).get("value", 0) or 0)
                        rejects.append({
                            "id": iid, "title": item.get("title", "")[:110],
                            "url": item.get("itemWebUrl", ""), "price": round(pr, 2),
                            "query": q, "reason": mv["reason"], "tags": mv["tags"],
                            "grams": extract_grams(item.get("title", "")),
                            "karat": karat_from_text(item.get("title", "")),
                        })
                    continue
                row = evaluate(item, spot24, cfg)
                if row:
                    by_id[iid] = item
                    row["query"] = q
                    _apply_trust(row, cfg)
                    rows.append(row)
                elif deep and cfg["deep_scan"] and needs_description(item, cfg):
                    item["_src_query"] = q
                    candidates.append(item)

    # The detail-call budget buys two different things, so split it explicitly rather
    # than letting whichever pass runs first eat the lot:
    #   1. AG confirmation - turn a price-band guess into eBay's actual answer about
    #      whether authentication is purchasable, and for how much
    #   2. weight recovery - pull grams out of descriptions for listings whose title
    #      never stated a weight
    total_cap = int(cfg["max_detail_calls"])
    dropped_deep = []          # listings the description disqualified after a detail call
    ag_cap = int(total_cap * float(cfg.get("ag_detail_share", 0.5))) if cfg.get("ag_mode") != "off" else 0
    used = 0
    ratelimited = False

    if ag_cap and rows:
        # Only worth confirming where the answer could change a decision: unresolved
        # AG status, not already a trap, and scoring well enough that you'd act on it.
        floor = int(cfg.get("ag_confirm_min_score", 45))
        # Gate on melt_score, not score: see the note on melt_score in evaluate_core.
        pend = [r for r in rows
                if not r.get("ag_confirmed") and not r.get("trap")
                and r.get("ag_state") in ("unknown", "none")
                and (r.get("melt_score") or r.get("score") or 0) >= floor]
        pend.sort(key=lambda r: -(r.get("melt_score") or r.get("score") or 0))
        if pend:
            print(f"Confirming Authenticity Guarantee on {min(ag_cap, len(pend))} "
                  f"of {len(pend)} unresolved listing(s)")
        confirmed_ag = 0
        for r in pend[:ag_cap]:
            item = by_id.get(r.get("id"))
            if not item:
                continue
            detail = get_item_detail(token, r.get("id"))
            used += 1
            if detail == "RATELIMIT":
                print("  ! eBay rate limit hit, stopping AG confirmation")
                ratelimited = True; break
            if not detail:
                continue
            state, fee, _ = ag_status(item, cfg, detail=detail)
            if state in ("included", "optional"):
                confirmed_ag += 1
            # Re-price the row now the fee is known for certain. Re-running the whole
            # evaluation keeps one definition of the maths instead of duplicating it.
            # Now that the full listing is in hand, re-check what the title couldn't
            # show. Doing this BEFORE the fallback matters: evaluate_deep returns None
            # both when the listing is disqualified AND when it simply has no weight in
            # the description, and falling back on the first case would re-admit a row
            # the description just told us to drop.
            why = deep_disqualifies(item, detail)
            # Log how each suspect resolved. Over enough sweeps this says whether a tag
            # like "component_metal" is mostly fine or mostly junk — the number needed
            # to tune the filter deliberately instead of by feel.
            for t in (r.get("material_tags") or []):
                bucket = cfg.setdefault("_suspect_outcomes", {}).setdefault(
                    t, {"kept": 0, "dropped": 0})
                bucket["dropped" if why else "kept"] += 1
            if why:
                print(f"  - dropped {r.get('id')}: {why}")
                dropped_deep.append({"id": r.get("id"), "query": r.get("query", ""),
                                     "title": r.get("title", ""), "reason": why})
                rows[rows.index(r)] = None
                continue
            # Re-price against the confirmed facts. The deep read can still legitimately
            # fail (no weight stated anywhere), so the fallback must be handed `detail`
            # — otherwise the row keeps guess-based economics while claiming confirmed
            # status.
            idx = rows.index(r)
            fresh = (evaluate_deep(item, detail, spot24, cfg)
                     or evaluate(item, spot24, cfg, detail=detail))
            if fresh:
                fresh["query"] = r.get("query", "")
                _apply_trust(fresh, cfg)
                rows[idx] = fresh
            else:
                # couldn't re-price at all: keep the old row but don't let it advertise
                # a confirmation its numbers never used
                r["ag_confirmed"] = False
            time.sleep(0.1)
        rows[:] = [r for r in rows if r is not None]
        print(f"  {confirmed_ag} listing(s) confirmed buyable with authentication"
              + (f" · {len(dropped_deep)} dropped on material re-check" if dropped_deep else ""))
        cfg.setdefault("_deep_drops", []).extend(dropped_deep)

    if deep and cfg["deep_scan"] and candidates and not ratelimited:
        candidates.sort(key=lambda it: float((it.get("price") or {}).get("value", 1e9) or 1e9))
        cap = max(0, total_cap - used)
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
    # Rank rejects by how much they'd have been worth if the filter was wrong, so the
    # review pile leads with the ones where a mistake actually cost you money.
    for rj in rejects:
        g, k = rj.get("grams"), rj.get("karat")
        rj["would_be_melt"] = round(PURITY[k] * spot24 * g, 2) if (g and k in PURITY) else None
        rj["would_be_under_pct"] = (
            round((rj["would_be_melt"] - rj["price"]) / rj["would_be_melt"] * 100, 1)
            if rj["would_be_melt"] and rj["price"] and rj["would_be_melt"] > 0 else None)
    rejects.sort(key=lambda r: -(r.get("would_be_under_pct") or -999))
    cfg.setdefault("_rejects", []).extend(rejects)
    if rejects:
        from collections import Counter
        by = Counter(t for r in rejects for t in r["tags"])
        print(f"  filtered out {len(rejects)} listing(s) on materials: "
              + ", ".join(f"{k}×{v}" for k, v in by.most_common()))
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
    _FBC = feedback_counts(CONFIG)          # raw 👍/👎 per search, for the payload's score
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
        # Re-alert on a cadence rather than once at the outage's start. The old
        # first-run-only rule meant a filter mistake could sit dead for a day with
        # a single stale notification and a healthy-looking dead-man's switch.
        stale_h = None
        try:
            stale_h = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(carried_from)).total_seconds() / 3600
        except Exception:
            pass
        streak = int(prev.get("zero_streak") or 0) + 1
        if streak == 1 or streak % cfg.get("zero_alert_every", 6) == 0:
            notify("GScout sweep failed",
                   f"{streak} consecutive sweeps returned 0 results ({why})."
                   + (f" Last good data is {stale_h:.0f}h old." if stale_h else "")
                   + " Check filters/quota — the engine keeps retrying.",
                   priority="high")
    zero_streak = (int(prev.get("zero_streak") or 0) + 1) if carried_from else 0

    # ---- ended listings, tracked engine-side ----
    # This used to be derived purely in the browser by diffing localStorage across
    # visits, which meant the Dead Listings tab was empty on any device that hadn't
    # personally watched a listing disappear — a fresh browser, a phone, or after a
    # storage clear. Computing it here makes it real data that travels with the site.
    ended = list(prev.get("ended") or [])
    if not carried_from:
        cur_ids = {r["id"] for r in deals + traps if r.get("id")}
        known = {e.get("id") for e in ended}
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in (prev.get("deals") or []) + (prev.get("traps") or []):
            rid = row.get("id")
            if rid and rid not in cur_ids and rid not in known:
                ended.append({**row, "ended_at": now_iso})
                known.add(rid)
        # a listing that came back (relisted, or a sweep that missed it) isn't dead
        ended = [e for e in ended if e.get("id") not in cur_ids]
        keep_days = int(CONFIG.get("ended_keep_days", 14))
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        def _fresh(e):
            try:
                return datetime.fromisoformat(e.get("ended_at", "")) >= cutoff
            except Exception:
                return True
        ended = [e for e in ended if _fresh(e)][-400:]

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
        "zero_streak": zero_streak,
        # compact per-query tally embedded here as well as query_stats.json: results.json
        # is always redeployed, whereas query_stats.json rides the Actions cache and
        # silently resets to empty if that cache is ever evicted.
        # Effective settings echoed back so the dashboard never has to hardcode a
        # mirror of an engine default. If a knob changes here, the UI follows on the
        # next sweep with nothing to keep in sync by hand.
        "config_echo": {k: CONFIG.get(k) for k in (
            "strong_score", "max_revives", "retire_min_runs",
            "alert_min_score", "query_weights", "query_prior_runs", "reserve_runs",
            "payout_pct", "trap_under_pct", "explore_frac", "daily_call_budget",
            "promote_min_deals", "fb_weight_span", "history_max", "runs_per_day",
            "starvation_cap", "exploit_share", "min_feedback_pct",
            "retire_rel_median", "retire_batch_cap", "retire_min_live",
            "retire_protect_liked", "ended_keep_days",
            "ag_mode", "ag_fee", "ag_optional_min", "ag_optional_max",
            "ag_required_min", "ag_score_penalty", "ag_confirm_min_score",
            "ag_detail_share", "max_detail_calls",
            "suspect_score_penalty", "reject_sample_max")},
        # AG rollup so the dashboard can headline "how much of today's board is
        # actually protected" without recomputing it from every row
        "ag_summary": {
            "included":  sum(1 for d in deals if d.get("ag_state") == "included"),
            "optional":  sum(1 for d in deals if d.get("ag_state") == "optional"),
            "none":      sum(1 for d in deals if d.get("ag_state") == "none"),
            "unknown":   sum(1 for d in deals if d.get("ag_state") == "unknown"),
            "confirmed": sum(1 for d in deals if d.get("ag_confirmed")),
        },
        "manual_run": {"id": qstats["meta"].get("manual_run_done"),
                       "at": qstats["meta"].get("manual_run_at")},
        "query_perf": {q: {"runs": s.get("runs", 0), "deals": s.get("deals", 0),
                           "traps": s.get("traps", 0), "strong": s.get("strong", 0),
                           "strong_ag": s.get("strong_ag", 0), "weak": s.get("weak", 0),
                           "weak_ag": s.get("weak_ag", 0), "ag": s.get("ag", 0),
                           "mixed": s.get("mixed", 0), "origin": s.get("origin", "core"),
                           "status": s.get("status"), "pinned": bool(s.get("pinned")),
                           "revives": s.get("revives", 0),
                           "score": round(query_value(s, CONFIG, _FBC.get(q)), 3)}
                       for q, s in sorted(qstats.get("queries", {}).items(),
                                          key=lambda kv: -query_value(kv[1], CONFIG,
                                                                      _FBC.get(kv[0])))[:200]},
        "total_profit": round(sum(d["profit"] for d in deals if d["profit"] > 0), 2),
        "settings_used": {
            "payout_pct": CONFIG["payout_pct"], "trap_under_pct": CONFIG["trap_under_pct"],
            "min_feedback_pct": CONFIG["min_feedback_pct"],
            "alert_min_score": ALERT["min_score"], "queries": CONFIG["queries"],
        },
        # The review pile: what the material filter threw away this sweep, worst-loss
        # first, plus how the surviving listings graded. Together these are the only
        # way to see BOTH kinds of error — a bad listing that got through is on your
        # board, a good one that didn't is in here.
        "rejects": sorted(CONFIG.get("_rejects") or [],
                          key=lambda r: -(r.get("would_be_under_pct") or -999))[:120],
        "rejects_count": len(CONFIG.get("_rejects") or []),
        "material_summary": {
            "clear":   sum(1 for d in deals if d.get("material") == "clear"),
            "suspect": sum(1 for d in deals if d.get("material") == "suspect"),
            "rejected": len(CONFIG.get("_rejects") or []),
            "reject_tags": dict(__import__("collections").Counter(
                t for r in (CONFIG.get("_rejects") or []) for t in r.get("tags", []))),
            "suspect_tags": dict(__import__("collections").Counter(
                t for d in deals for t in (d.get("material_tags") or []))),
            # how suspects resolved once a detail call could settle them
            "suspect_outcomes": CONFIG.get("_suspect_outcomes", {}),
        },
        "defects": (lambda d: {"total": d.get("total", 0),
                               "by_category": d.get("by_category", {}),
                               "engine_drop_reasons": d.get("engine_drop_reasons", {})})(
            build_defect_backlog(CONFIG, CONFIG.get("_deep_drops"))),
        "learning": {
            "feedback_events": n_fb,
            "sellers_blocked": sorted(sblock),
            "queries_ran": len(ran), "explored": explore_q,
            "promoted": promoted, "retired": retired,
            "sorts": sorts,
        },
        "ended": ended,
        "ended_count": len(ended),
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

    # --- Source 4: last known spot from our own history, if fresh enough ---
    # A total outage of all three live sources shouldn't kill the sweep: gold moves
    # little enough intraday that recent spot still prices melt margins usefully.
    # Window is spot_stale_max_hours (settings-controllable); label makes it visible
    # in logs and chart tooltips so a stale price is never mistaken for live.
    try:
        with open(CONFIG["history_file"]) as f:
            hist = json.load(f)
        for h in reversed(hist):
            if not h.get("spot_oz") or h.get("price_src", "").startswith("history"):
                continue   # never chain stale-on-stale
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(h["t"])).total_seconds() / 3600
            if age_h <= CONFIG["spot_stale_max_hours"]:
                print(f"[price] ALL live sources down — using last known spot "
                      f"${h['spot_oz']:.2f}/oz from history ({age_h:.1f}h old)")
                return float(h["spot_oz"]), f"history ({age_h:.0f}h stale)"
            break   # newest usable record is already too old; stop looking
    except Exception as e:
        print(f"[price] history fallback failed: {e}")

    raise RuntimeError("All 3 gold price sources failed and no fresh history — "
                       "check network and APIs")


if __name__ == "__main__":
    main()
