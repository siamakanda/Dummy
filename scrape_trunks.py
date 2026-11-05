"""Step 1 of Dummy: read trunk names per carrier from the switch portal -> data/trunks.csv

Examples
  python scrape_trunks.py --discover              # save HTML/screenshots of key pages (to tune selectors)
  python scrape_trunks.py --all                   # read the real carrier ids from /carriers.php, then sweep
  python scrape_trunks.py --ids 41                # one carrier
  python scrape_trunks.py --ids 1-120             # a range (carriers with no trunk cards are skipped)
  python scrape_trunks.py --all --headed          # watch the browser
"""
import argparse, csv, json, time
import config
from parse import extract_trunks, extract_carriers, carrier_name
from portal import Portal

TRUNK_FIELDS = ["carrier_id", "carrier_name", "trunk_id", "trunk_name", "prefix", "status", "cost_deck"]
CARRIER_FIELDS = ["carrier_id", "carrier_name"]

def parse_ids(spec: str) -> list[int]:
    ids = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-"); ids += range(int(a), int(b) + 1)
        else:
            ids.append(int(part))
    return ids

def discover(p: Portal):
    for name, path in [("rate_decks", "rate_decks"),
                       ("carriers", "carriers.php"),
                       ("carrier_trunks_59", "carrier_trunks.php?id=59")]:
        p.goto(path); p.snapshot(name)
        try:
            srcs = p.page.eval_on_selector_all("script[src]", "els => els.map(e => e.src)")
            (config.SNAP_DIR / f"{name}_scripts.txt").write_text("\n".join(srcs), encoding="utf-8")
        except Exception:
            pass
        print("saved", name)
    # The upload page may render a form, or immediately download the template -> guard both.
    try:
        p.goto("rate_deck_upload.php?id=37")
        p.snapshot("rate_deck_upload_37")
        print("saved rate_deck_upload_37")
    except Exception as e:
        print("skipped rate_deck_upload_37:", e)
    try:
        print("saved template ->", p.download_template())
    except Exception as e:
        print("template download failed:", e)
    print(f"Snapshots in {config.SNAP_DIR}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="41", help="carrier ids, e.g. 41 or 1-120 (ignored with --all)")
    ap.add_argument("--all", action="store_true", help="read every carrier id from /carriers.php")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--out", default=str(config.DATA_DIR / "trunks.csv"))
    ap.add_argument("--carriers-out", default=str(config.DATA_DIR / "carriers.csv"))
    ap.add_argument("--delay", type=float, default=0.5)
    a = ap.parse_args()

    with Portal(headless=not a.headed) as p:
        if a.discover:
            return discover(p)

        if a.all:
            ids = [c["carrier_id"] for c in extract_carriers(p.carriers_html())]
            print(f"discovered {len(ids)} carriers from /carriers.php")
        else:
            ids = parse_ids(a.ids)

        rows, carriers, skipped = [], [], []
        for cid in ids:
            html = p.carrier_trunks_html(cid)
            (config.SNAP_DIR / f"carrier_{cid}.html").write_text(html, encoding="utf-8")
            trunks = extract_trunks(html)
            if not trunks:
                skipped.append(cid); print(f"carrier {cid}: no trunk cards, skipped"); continue
            cname = carrier_name(html, f"carrier_{cid}")
            carriers.append({"carrier_id": cid, "carrier_name": cname})
            for t in trunks:
                rows.append({"carrier_id": cid, "carrier_name": cname, **t})
            print(f"carrier {cid} ({cname}): {len(trunks)} trunks")
            time.sleep(a.delay)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRUNK_FIELDS)
        w.writeheader(); w.writerows(rows)
    with open(a.carriers_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CARRIER_FIELDS)
        w.writeheader(); w.writerows(carriers)
    print(f"\nWrote {len(rows)} trunks to {a.out} ({len(carriers)} carriers)")
    print(f"Wrote {len(carriers)} carriers to {a.carriers_out}; skipped carriers: {skipped or 'none'}")

if __name__ == "__main__":
    main()
