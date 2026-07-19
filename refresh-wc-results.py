#!/usr/bin/env python3
"""
refresh-wc-results.py — token-free live updater for the World Cup 2026 dashboard.

Pulls current group-stage scorelines + standings from the English Wikipedia
"2026 FIFA World Cup" article (server-rendered HTML — no API key, no browser)
and writes  world-cup-results.json , the file the dashboard's "All 12 Groups"
section polls to show live scores + auto-computed standings for every group.

Why this exists instead of fetch-wc-results.py:
  fetch-wc-results.py uses football-data.org, which needs a paid-signup API
  token that was never provisioned. Wikipedia needs none. This script is the
  active updater; fetch-wc-results.py stays as an alternative if a token is
  ever added (identical output shape).

SELF-VERIFYING (the important bit): the standings it COMPUTES from the scraped
match results must exactly equal the standings TABLE scraped from the same page,
for every team in all 12 groups. If they disagree (mid-edit, a live match whose
running score isn't yet in the table, or a markup change that breaks parsing) it
REFUSES to write and keeps the last-good file. So the dashboard can never show a
corrupted table — at worst it's one ~20-min cycle behind during a live game.

Deps: lxml (present on both Macs). Output shape (superset of fetch-wc-results.py):
  {"stamp":"YYYY-MM-DD HH:MM",
   "matches":[{"a":HOME,"b":AWAY,"ha":int,"hb":int,"status":"FINISHED","utc":null}, ...],
   "knockout":[{"round":"R32","a":CODE|null,"b":CODE|null,"a_label":str,"b_label":str,
                "ha":int|null,"hb":int|null,"status":"FINISHED"|"SCHEDULED",
                "aet":bool,"pens":[h,a]|null,"winner":CODE|null}, ...]}
`matches` stays GROUP-ONLY (what the group tables compute from). Knockout fixtures
are a separate array so they never corrupt group standings — see the parse loop.

Self-contained (team/alias maps inlined, mirroring fetch-wc-results.py) so it can
run from anywhere on the always-on Mini without a vault-path dependency.

Usage: refresh-wc-results.py [--out PATH] [--url URL] [--quiet]
"""
import json, os, re, sys, argparse, datetime, urllib.request
from pathlib import Path
import lxml.html

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "00-home/cockpit/world-cup-results.json"
WIKI_URL = "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup"

# 48 valid 3-letter codes (our scheme) + Wikipedia team-name aliases.
# Mirrors fetch-wc-results.py — keep in sync if the team list ever changes.
CODES = {
 "MEX","RSA","KOR","CZE","CAN","BIH","QAT","SUI","BRA","MAR","HAI","SCO",
 "USA","PAR","AUS","TUR","GER","CUW","CIV","ECU","NED","JPN","SWE","TUN",
 "BEL","EGY","IRN","NZL","ESP","CPV","KSA","URU","FRA","SEN","IRQ","NOR",
 "ARG","ALG","AUT","JOR","POR","COD","UZB","COL","ENG","CRO","GHA","PAN",
}
ALIASES = {
 "mexico":"MEX","south africa":"RSA","south korea":"KOR","korea republic":"KOR",
 "czechia":"CZE","czech republic":"CZE","canada":"CAN",
 "bosnia & herzegovina":"BIH","bosnia and herzegovina":"BIH","bosnia-herzegovina":"BIH",
 "qatar":"QAT","switzerland":"SUI","brazil":"BRA","morocco":"MAR","haiti":"HAI",
 "scotland":"SCO","united states":"USA","usa":"USA","paraguay":"PAR",
 "australia":"AUS","turkiye":"TUR","türkiye":"TUR","turkey":"TUR","germany":"GER",
 "curacao":"CUW","curaçao":"CUW","ivory coast":"CIV","cote d'ivoire":"CIV",
 "côte d'ivoire":"CIV","ecuador":"ECU","netherlands":"NED","japan":"JPN",
 "sweden":"SWE","tunisia":"TUN","belgium":"BEL","egypt":"EGY","iran":"IRN",
 "ir iran":"IRN","new zealand":"NZL","spain":"ESP","cape verde":"CPV",
 "cabo verde":"CPV","saudi arabia":"KSA","uruguay":"URU","france":"FRA",
 "senegal":"SEN","iraq":"IRQ","norway":"NOR","argentina":"ARG","algeria":"ALG",
 "austria":"AUT","jordan":"JOR","portugal":"POR","dr congo":"COD",
 "congo dr":"COD","democratic republic of the congo":"COD",
 "democratic republic of congo":"COD","uzbekistan":"UZB",
 "colombia":"COL","england":"ENG","croatia":"CRO","ghana":"GHA","panama":"PAN",
}
def _norm(s): return re.sub(r"[^a-z]", "", (s or "").lower())
NORMIDX = {_norm(k): v for k, v in ALIASES.items()}

def to_code(name):
    n = re.sub(r"\(.*?\)", "", name)        # drop "(H, A)" host/seed markers
    n = re.sub(r"\[.*?\]", "", n).strip()   # drop "[a]" footnotes
    return ALIASES.get(n.lower()) or NORMIDX.get(_norm(n))

def cell(box, cls):
    x = box.xpath('.//*[contains(@class,"%s")]' % cls)
    return x[0].text_content().strip() if x else ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--url", default=WIKI_URL)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    req = urllib.request.Request(args.url, headers={
        "User-Agent": "vault-wc-results/1.0 (personal dashboard; contact via vault owner)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr); return 1

    doc = lxml.html.fromstring(html)

    # ---- match results: table.fevent with th.fhome / .fscore / .faway ----
    # Each fixture sits under a section heading. Group fixtures live under
    # Group_A..Group_L; knockout fixtures under the round headings below. We must
    # SEPARATE the two: only GROUP fixtures may feed the group-standings compute/
    # verify (a knockout game counted as a 4th group match is exactly what froze
    # this file from 28 Jun — a team shows Pld 4 vs the wiki table's Pld 3).
    ROUND_HEAD = {                    # heading id -> short round label (bracket order)
        "Round_of_32": "R32", "Round_of_16": "R16", "Quarterfinals": "QF",
        "Semi-finals": "SF", "Semifinals": "SF",
        "Match_for_third_place": "3P", "Third_place_play-off": "3P",
        "Final": "Final",
    }
    def section_id(fb):
        ids = fb.xpath('preceding::div[contains(@class,"mw-heading")]'
                       '[1]//*[self::h2 or self::h3 or self::h4]/@id')
        return ids[-1] if ids else ""

    def short_label(raw):             # "Winner Match 79" -> "W79"; "Loser Match 106" -> "L106"
        m = re.search(r'(Winner|Loser)\s+Match\s+(\d+)', raw, re.I)
        if m:
            return ("W" if m.group(1).lower() == "winner" else "L") + m.group(2)
        return re.sub(r"\[.*?\]", "", raw).strip()[:18]

    matches, knockout, unmapped = [], [], []
    for fb in doc.xpath('//table[contains(@class,"fevent")]'):
        head = section_id(fb)
        is_group = bool(re.match(r"^Group_[A-L]$", head))
        rnd = ROUND_HEAD.get(head)
        if not is_group and not rnd:
            continue                                  # not a tournament fixture we track

        home_raw, away_raw = cell(fb, "fhome"), cell(fb, "faway")
        ca, cb = to_code(home_raw), to_code(away_raw)
        sc = cell(fb, "fscore").replace("–", "-").replace("—", "-")
        mm = re.search(r"(\d+)\s*-\s*(\d+)", sc)      # numeric score => played (knockout may add "(a.e.t.)")

        if is_group:
            if not mm:                                # "Match 53" placeholder / time => skip
                continue
            if not ca or not cb:
                unmapped.append((home_raw, away_raw)); continue
            matches.append({"a": ca, "b": cb, "ha": int(mm.group(1)), "hb": int(mm.group(2)),
                            "status": "FINISHED", "utc": None})
            continue

        # ---- knockout fixture (emit the SLOT even if teams/score are TBD, so the
        # bracket shows structure). Teams that don't map are placeholders → labels.
        ha = int(mm.group(1)) if mm else None
        hb = int(mm.group(2)) if mm else None
        aet = "a.e.t" in sc.lower()
        pens = None
        winner = None
        if mm and ha == hb:                           # drawn after 90'/ET => look for a shootout
            pm = re.search(r"Penalt(?:ies|y).*?(\d+)\s*[-–]\s*(\d+)",
                           fb.text_content(), re.S)
            if pm:
                pens = [int(pm.group(1)), int(pm.group(2))]
        if mm:
            if ha > hb:   winner = ca
            elif hb > ha: winner = cb
            elif pens:    winner = ca if pens[0] > pens[1] else cb
        knockout.append({
            "round": rnd,
            "a": ca, "b": cb,
            "a_label": short_label(home_raw), "b_label": short_label(away_raw),
            "ha": ha, "hb": hb,
            "status": "FINISHED" if mm else "SCHEDULED",
            "aet": aet, "pens": pens, "winner": winner,
        })

    # Group stage is over and Wikipedia moved the group match boxes off the main
    # article into per-group sub-articles (2026-07-18), so the page now yields
    # zero group fixtures. Those results are immutable once played, so reuse the
    # group matches from the last-good file and keep updating the knockout array
    # from the page. Stay hard-failed only when there's nothing to fall back on.
    reused = False
    if not matches:
        try:
            prev0 = json.loads(Path(os.path.expanduser(args.out)).read_text())
            matches = prev0.get("matches") or []
            reused = bool(matches)
        except Exception:
            pass
        if reused:
            log(f"No group fixtures on page — reusing {len(matches)} finished "
                f"group matches from last-good file.")
    if not matches:
        print("WARN: no finished matches parsed; leaving existing file untouched.",
              file=sys.stderr); return 1

    # ---- scrape standings tables (ground truth for verification) ----
    expected = {}
    for t in doc.xpath('//table[contains(@class,"wikitable")]'):
        ths = [x.text_content().strip() for x in t.xpath('.//th')]
        if not (any(x == "Pld" for x in ths) and any(x == "Pts" for x in ths)):
            continue
        for tr in t.xpath('.//tr'):
            c = [x.text_content().strip() for x in tr.xpath('./td|./th')]
            if len(c) < 10 or not c[2].isdigit():     # skip header / malformed
                continue
            code = to_code(c[1])
            if not code:
                continue
            expected[code] = dict(p=int(c[2]), w=int(c[3]), d=int(c[4]), l=int(c[5]),
                                  gf=int(c[6]), ga=int(c[7]), pts=int(re.sub(r"\D", "", c[9])))

    # ---- compute standings from matches, then VERIFY against scraped truth ----
    comp = {c: dict(p=0, w=0, d=0, l=0, gf=0, ga=0, pts=0) for c in CODES}
    for m in matches:
        H, A = comp[m["a"]], comp[m["b"]]; ha, hb = m["ha"], m["hb"]
        H["p"] += 1; A["p"] += 1; H["gf"] += ha; H["ga"] += hb; A["gf"] += hb; A["ga"] += ha
        if ha > hb:   H["w"] += 1; A["l"] += 1; H["pts"] += 3
        elif ha < hb: A["w"] += 1; H["l"] += 1; A["pts"] += 3
        else:         H["d"] += 1; A["d"] += 1; H["pts"] += 1; A["pts"] += 1

    bad = []
    for code, exp in expected.items():
        for f in ("p", "w", "d", "l", "gf", "ga", "pts"):
            if comp[code][f] != exp[f]:
                bad.append((code, f, "got", comp[code][f], "wiki", exp[f]))
    # Reused matches were verified when originally written; if the standings
    # tables also leave the main article, an empty `expected` isn't a parse
    # failure in that mode — but any table that IS present must still agree.
    if (not expected and not reused) or bad:
        print(f"WARN: standings verify FAILED ({len(bad)} field diffs across "
              f"{len(expected)} teams) — keeping last-good file. e.g. {bad[:4]}",
              file=sys.stderr); return 1

    # ---- verified. IDEMPOTENT write: only rewrite when the RESULTS actually
    # changed, never just to bump the stamp. This job polls every ~2 min so a
    # match shows up fast; rewriting unconditionally would churn the stamp (and
    # Obsidian Sync) 700+ times/day for nothing. Mirrors the dashboard
    # generator's anti-churn rule (network-map Discovery Log 2026-06-17). So
    # `stamp` means "as of the last result change", which is the honest reading.
    outp = Path(os.path.expanduser(args.out)); outp.parent.mkdir(parents=True, exist_ok=True)
    sig = lambda ms: sorted((m["a"], m["b"], m["ha"], m["hb"]) for m in ms)
    # Knockout signature also folds in round/status/winner so a scheduled tie
    # flipping to a result (or a shootout resolving) counts as a real change.
    ksig = lambda ks: sorted((k["round"], k.get("a"), k.get("b"), k.get("a_label"),
                              k.get("b_label"), k["ha"], k["hb"], k["status"],
                              tuple(k["pens"]) if k["pens"] else None, k["winner"])
                             for k in ks)
    if outp.exists():
        try:
            prev = json.loads(outp.read_text())
            if sig(prev.get("matches", [])) == sig(matches) and \
               ksig(prev.get("knockout", [])) == ksig(knockout):
                log(f"No change ({len(matches)} group, {len(knockout)} knockout) — "
                    f"kept {outp} (stamp {prev.get('stamp')})")
                return 0
        except Exception:
            pass  # unreadable / old format → fall through and rewrite

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = {"stamp": stamp, "matches": matches, "knockout": knockout}
    tmp = outp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    os.replace(tmp, outp)
    kp = sum(1 for k in knockout if k["status"] == "FINISHED")
    log(f"Wrote {outp}: {len(matches)} group + {len(knockout)} knockout ({kp} played), "
        f"{len(expected)} teams verified, stamp {stamp}")
    if unmapped:
        log("WARN unmapped (add to ALIASES): " + ", ".join(f"{a}|{b}" for a, b in unmapped))
    return 0

if __name__ == "__main__":
    sys.exit(main())
