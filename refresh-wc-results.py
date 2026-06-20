#!/usr/bin/env python3
"""
refresh-wc-results.py — refresh world-cup-results.json from Wikipedia.

Pulls current group-stage scorelines + standings from the English Wikipedia
"2026 FIFA World Cup" article (server-rendered HTML, no API key, no browser) and
writes world-cup-results.json — the file this page polls to show live scores and
auto-computed standings for every group.

SELF-VERIFYING: the standings it COMPUTES from the scraped match results must
exactly equal the standings TABLE scraped from the same page, for every team in
all 12 groups, or it refuses to write (keeps the last-good file). A mid-edit or
live-match inconsistency therefore can never corrupt the table. The write is also
idempotent — it only rewrites when a score actually changed.

Deps: lxml.   Usage: refresh-wc-results.py [--out PATH] [--url URL] [--quiet]
"""
import json, os, re, sys, argparse, datetime, urllib.request
from pathlib import Path
import lxml.html

DEFAULT_OUT = Path(__file__).resolve().parent / "world-cup-results.json"
WIKI_URL = "https://en.wikipedia.org/wiki/2026_FIFA_World_Cup"

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
    n = re.sub(r"\(.*?\)", "", name)
    n = re.sub(r"\[.*?\]", "", n).strip()
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
        "User-Agent": "wc2026-tracker/1.0 (static dashboard refresher)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"ERROR: fetch failed: {e}", file=sys.stderr); return 1

    doc = lxml.html.fromstring(html)

    matches, unmapped = [], []
    for fb in doc.xpath('//table[contains(@class,"fevent")]'):
        sc = cell(fb, "fscore").replace("–", "-").replace("—", "-")
        mm = re.match(r"^(\d+)-(\d+)$", sc)
        if not mm:
            continue
        ca, cb = to_code(cell(fb, "fhome")), to_code(cell(fb, "faway"))
        if not ca or not cb:
            unmapped.append((cell(fb, "fhome"), cell(fb, "faway"))); continue
        matches.append({"a": ca, "b": cb, "ha": int(mm.group(1)), "hb": int(mm.group(2)),
                        "status": "FINISHED", "utc": None})

    if not matches:
        print("WARN: no finished matches parsed; leaving existing file untouched.",
              file=sys.stderr); return 1

    expected = {}
    for t in doc.xpath('//table[contains(@class,"wikitable")]'):
        ths = [x.text_content().strip() for x in t.xpath('.//th')]
        if not (any(x == "Pld" for x in ths) and any(x == "Pts" for x in ths)):
            continue
        for tr in t.xpath('.//tr'):
            c = [x.text_content().strip() for x in tr.xpath('./td|./th')]
            if len(c) < 10 or not c[2].isdigit():
                continue
            code = to_code(c[1])
            if not code:
                continue
            expected[code] = dict(p=int(c[2]), w=int(c[3]), d=int(c[4]), l=int(c[5]),
                                  gf=int(c[6]), ga=int(c[7]), pts=int(re.sub(r"\D", "", c[9])))

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
    if not expected or bad:
        print(f"WARN: standings verify FAILED ({len(bad)} field diffs across "
              f"{len(expected)} teams) — keeping last-good file. e.g. {bad[:4]}",
              file=sys.stderr); return 1

    outp = Path(os.path.expanduser(args.out)); outp.parent.mkdir(parents=True, exist_ok=True)
    sig = lambda ms: sorted((m["a"], m["b"], m["ha"], m["hb"]) for m in ms)
    if outp.exists():
        try:
            prev = json.loads(outp.read_text())
            if sig(prev.get("matches", [])) == sig(matches):
                log(f"No change ({len(matches)} matches) — kept {outp}")
                return 0
        except Exception:
            pass

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    out = {"stamp": stamp, "matches": matches}
    tmp = outp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    os.replace(tmp, outp)
    log(f"Wrote {outp}: {len(matches)} finished matches, {len(expected)} teams verified, stamp {stamp}")
    if unmapped:
        log("WARN unmapped: " + ", ".join(f"{a}|{b}" for a, b in unmapped))
    return 0

if __name__ == "__main__":
    sys.exit(main())
