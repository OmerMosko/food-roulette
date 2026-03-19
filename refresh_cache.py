#!/usr/bin/env python3
"""Refresh restaurants_cache.json from Wolt and push to GitHub."""
import json, math, os, subprocess, sys
import requests

LAT, LON = 32.0761450, 34.7809560
TARGET_TAGS = {
    "burgers":       ["burger","burgers","hamburger"],
    "pizza":         ["pizza"],
    "mexican":       ["mexican","tex-mex","tacos","burrito"],
    "fried chicken": ["chicken","fried chicken","wings"],
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}

def haversine(lat1,lon1,lat2,lon2):
    R=6371000; p1,p2=math.radians(lat1),math.radians(lat2)
    a=(math.sin((p2-p1)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(lon2-lon1)/2)**2)
    return round(R*2*math.atan2(math.sqrt(a),math.sqrt(1-a)))

def fetch():
    url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={LAT}&lon={LON}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    section = next((s for s in data.get("sections",[]) if s.get("name")=="restaurants-delivering-venues"), None)
    if not section:
        raise RuntimeError("No restaurant section in Wolt response")

    results = []; seen = set()
    for item in section.get("items",[]):
        v = item.get("venue",{})
        if not v: continue
        slug = v.get("slug","")
        if slug in seen: continue
        seen.add(slug)
        score  = v.get("rating",{}).get("score",0) or 0
        volume = v.get("rating",{}).get("volume",0) or 0
        tags   = [t.lower() for t in v.get("tags",[])]
        cat    = next((c for c,kw in TARGET_TAGS.items() if any(k in tags for k in kw)), None)
        if not cat or volume < 50: continue
        loc     = v.get("location") or []
        dist_m  = haversine(LAT,LON,loc[1],loc[0]) if len(loc)==2 else None
        raw_img = (item.get("image") or {}).get("url") or (v.get("brand_image") or {}).get("url") or ""
        results.append({
            "name": v.get("name",""), "slug": slug, "category": cat,
            "score": score, "volume": volume, "online": v.get("online", True),
            "desc": (v.get("short_description") or "").replace("\n"," ").strip()[:90],
            "estimate": v.get("estimate_range",""), "dist_m": dist_m,
            "image_url": (raw_img+"?w=600") if raw_img else "",
            "wolt": f"https://wolt.com/en/isr/tel-aviv/restaurant/{slug}",
        })
    results.sort(key=lambda x: (-x["score"],-x["volume"]))
    return results

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cache_path = os.path.join(here, "restaurants_cache.json")

    print("Fetching from Wolt…")
    try:
        restaurants = fetch()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Got {len(restaurants)} restaurants")

    # Load old cache to check if anything changed
    old = []
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            old = json.load(f)

    if len(old) == len(restaurants) and {r["slug"] for r in old} == {r["slug"] for r in restaurants}:
        print("No changes — skipping push")
        return

    with open(cache_path, "w") as f:
        json.dump(restaurants, f)
    print(f"Saved {len(restaurants)} restaurants (was {len(old)})")

    # Commit and push
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("No GITHUB_TOKEN — skipping push (cache saved locally)")
        return
    remote = f"https://{token}@github.com/OmerMosko/food-roulette.git"
    cmds = [
        ["git", "-C", here, "add", "restaurants_cache.json"],
        ["git", "-C", here, "commit", "-m", f"chore: refresh restaurant cache ({len(restaurants)} venues)",
         "--author", "Omer Mosko <69163768+OmerMosko@users.noreply.github.com>"],
        ["git", "-C", here, "push", remote, "main"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Git error: {result.stderr}", file=sys.stderr)
            sys.exit(1)
    print("Pushed to GitHub ✓")

if __name__ == "__main__":
    main()
