#!/usr/bin/env python3
"""Food Roulette — Flask + SocketIO backend."""

import math
import os
import random
import string
import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit, join_room as sio_join_room

app = Flask(__name__, static_folder=".")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "food-roulette-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

rooms = {}

LAT = 32.0761450
LON = 34.7809560

TARGET_TAGS = {
    "burgers":       ["burger","burgers","hamburger"],
    "pizza":         ["pizza"],
    "mexican":       ["mexican","tex-mex","tacos","burrito"],
    "fried chicken": ["chicken","fried chicken","wings"],
}

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept":          "application/json",
    "X-Forwarded-For": "82.80.1.1",   # Israeli Bezeq IP — Wolt geo check
    "X-Real-IP":       "82.80.1.1",
}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def fetch_restaurants(lat=None, lon=None):
    lat = lat or LAT
    lon = lon or LON
    url  = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    section = next(
        (s for s in data.get("sections", []) if s.get("name") == "restaurants-delivering-venues"),
        None
    )
    if not section:
        return []

    # Extract city/country from top-level response for correct Wolt deep-links
    wolt_city    = (data.get("city") or "tel-aviv").lower().replace(" ", "-")
    city_data    = data.get("city_data") or {}
    wolt_country = (city_data.get("country_code_alpha3") or "ISR").lower()

    results = []; seen = set()
    for item in section.get("items", []):
        v = item.get("venue", {})
        if not v: continue
        slug = v.get("slug", "")
        if slug in seen: continue
        seen.add(slug)
        score  = v.get("rating", {}).get("score", 0) or 0
        volume = v.get("rating", {}).get("volume", 0) or 0
        if volume < 50: continue
        tags = [t.lower() for t in v.get("tags", [])]
        cat  = next((c for c, kw in TARGET_TAGS.items() if any(k in tags for k in kw)), None)
        if not cat: continue
        loc    = v.get("location") or []
        dist_m = haversine(lat, lon, loc[1], loc[0]) if len(loc) == 2 else None
        raw    = (item.get("image") or {}).get("url") or (v.get("brand_image") or {}).get("url") or ""
        results.append({
            "name": v.get("name",""), "slug": slug, "category": cat,
            "score": score, "volume": volume, "online": v.get("online", False),
            "desc": (v.get("short_description") or "").replace("\n"," ").strip()[:90],
            "estimate": v.get("estimate_range",""), "dist_m": dist_m,
            "image_url": (raw + "?w=600") if raw else "",
            "wolt": f"https://wolt.com/en/{wolt_country}/{wolt_city}/restaurant/{slug}",
        })
    results.sort(key=lambda x: (-x["score"], -x["volume"]))
    return results


def apply_filters(rests, cats=None, max_radius=None, fast_only=False, min_score=8.0):
    out = []
    for r in rests:
        if not r.get("online"): continue
        if r.get("score", 0) < min_score: continue
        if cats and r["category"] not in cats: continue
        if max_radius and (r.get("dist_m") is None or r["dist_m"] > max_radius): continue
        if fast_only:
            try:
                if int(r["estimate"].split("-")[-1]) > 45: continue
            except Exception:
                pass
        out.append(r)
    return out


# ── Helpers ─────────────────────────────────────────────────────
def gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


# ── Routes ──────────────────────────────────────────────────────
@app.route("/")
@app.route("/party")
def index():
    return send_from_directory(".", "party.html")

@app.route("/solo")
def solo():
    return send_from_directory(".", "index.html")

@app.route("/og-image.png")
def og_image():
    return send_from_directory(".", "og-image.png")

@app.route("/api/restaurants")
def api_restaurants():
    try:
        lat = float(request.args.get("lat", LAT))
        lon = float(request.args.get("lon", LON))
        return jsonify({"ok": True, "restaurants": fetch_restaurants(lat, lon)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool-count")
def api_pool_count():
    try:
        lat        = float(request.args.get("lat", LAT))
        lon        = float(request.args.get("lon", LON))
        cats_param = request.args.get("cats", "")
        cats       = [c.strip() for c in cats_param.split(",") if c.strip()] or None
        max_radius = int(request.args.get("max_radius", 0)) or None
        fast_only  = request.args.get("fast_only", "false").lower() == "true"
        min_score  = float(request.args.get("min_score", 8.0))
        rests      = fetch_restaurants(lat, lon)
        pool       = apply_filters(rests, cats=cats, max_radius=max_radius,
                                   fast_only=fast_only, min_score=min_score)
        return jsonify({"ok": True, "count": len(pool)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/proxy/wolt")
def api_proxy_wolt():
    """Proxy Wolt's restaurant API with Israeli IP headers — bypasses geo-block on Railway."""
    try:
        lat = request.args.get("lat", LAT)
        lon = request.args.get("lon", LON)
        url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={lat}&lon={lon}"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        response = app.response_class(
            response=resp.content,
            status=200,
            mimetype="application/json"
        )
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── SocketIO: game events ────────────────────────────────────────
@socketio.on("create_room")
def on_create_room(data):
    host_name    = (data.get("host_name") or "Host").strip() or "Host"
    option_count = 10          # default; host can change in lobby
    player_count = 20          # no real cap — anyone can join
    cats         = list(TARGET_TAGS.keys())   # all categories by default
    max_radius   = None        # no distance filter by default
    fast_only    = False
    min_score    = 8.0
    lat          = float(data.get("lat", LAT))
    lon          = float(data.get("lon", LON))

    try:
        rests = fetch_restaurants(lat, lon)
    except Exception as e:
        emit("game_error", {"msg": f"Failed to fetch restaurants: {e}"})
        return

    pool   = apply_filters(rests, cats=cats, max_radius=max_radius,
                           fast_only=fast_only, min_score=min_score)
    chosen = random.sample(pool, min(option_count, len(pool)))
    code   = gen_code()

    rooms[code] = {
        "host_sid":     request.sid,
        "player_count": player_count,
        "option_count": option_count,
        "players":      {request.sid: {"name": host_name, "done": False, "votes": {}, "superlike": None}},
        "restaurants":  chosen,
        "pool_count":   len(pool),
        "status":       "waiting",
        "filters":      {"cats": cats, "max_radius": max_radius,
                         "fast_only": fast_only, "min_score": min_score},
        "location":     {"lat": lat, "lon": lon},
    }

    sio_join_room(code)
    emit("room_created", {
        "code":         code,
        "restaurants":  chosen,
        "pool_count":   len(pool),
        "player_count": player_count,
        "players":      [rooms[code]["players"][request.sid]["name"]],
        "filters":      rooms[code]["filters"],
        "option_count": option_count,
    })


@socketio.on("join_game")
def on_join_game(data):
    code = (data.get("code") or "").strip().upper()
    name = (data.get("name") or "Player").strip() or "Player"

    if code not in rooms:
        emit("game_error", {"msg": f"Room '{code}' not found."})
        return

    room = rooms[code]

    if room["status"] != "waiting":
        emit("game_error", {"msg": "Game already started — join the next round!"})
        return

    # No hard player cap — room is open until game starts

    sio_join_room(code)
    room["players"][request.sid] = {"name": name, "done": False, "votes": {}, "superlike": None}
    player_names = [p["name"] for p in room["players"].values()]

    emit("joined_game", {
        "code":         code,
        "restaurants":  room["restaurants"],
        "pool_count":   room.get("pool_count", 0),
        "player_count": room["player_count"],
        "players":      player_names,
    })

    socketio.emit("player_joined", {
        "players":      player_names,
        "player_count": room["player_count"],
        "can_start":    len(room["players"]) >= 2,
    }, room=code)


@socketio.on("cast_vote")
def on_cast_vote(data):
    code = (data.get("code") or "").upper()
    slug = data.get("slug")
    vote = bool(data.get("vote"))
    if code not in rooms:
        return
    room = rooms[code]
    sid  = request.sid
    if sid not in room["players"]:
        return
    room["players"][sid]["votes"][slug] = vote
    total_rests = len(room["restaurants"])
    voted_counts = {p["name"]: len(p["votes"]) for p in room["players"].values()}
    socketio.emit("vote_progress", {"progress": voted_counts, "total": total_rests}, room=code)
    if len(room["players"][sid]["votes"]) >= total_rests:
        room["players"][sid]["done"] = True
    if all(p["done"] for p in room["players"].values()):
        _finish_game(code)


@socketio.on("cast_superlike")
def on_cast_superlike(data):
    code = (data.get("code") or "").upper()
    slug = data.get("slug")
    if code not in rooms:
        return
    room = rooms[code]
    sid  = request.sid
    if sid not in room["players"]:
        return
    player = room["players"][sid]
    if player.get("superlike") is not None:
        emit("game_error", {"msg": "You already used your superlike!"})
        return
    player["superlike"] = slug
    player["votes"][slug] = True
    total_rests  = len(room["restaurants"])
    voted_counts = {p["name"]: len(p["votes"]) for p in room["players"].values()}
    socketio.emit("superlike_cast", {"slug": slug, "progress": voted_counts, "total": total_rests}, room=code)
    if len(player["votes"]) >= total_rests:
        player["done"] = True
    if all(p["done"] for p in room["players"].values()):
        _finish_game(code)


@socketio.on("cast_ranking")
def on_cast_ranking(data):
    code     = (data.get("code") or "").upper()
    rankings = data.get("rankings") or []
    if code not in rooms:
        return
    room = rooms[code]
    sid  = request.sid
    if sid not in room["players"] or room["status"] != "ranking":
        return
    room["players"][sid]["rankings"] = rankings
    room["players"][sid]["done"]     = True
    done_count  = sum(1 for p in room["players"].values() if p["done"])
    total_count = len(room["players"])
    socketio.emit("ranking_progress", {"done": done_count, "total": total_count}, room=code)
    if done_count >= total_count:
        _finish_ranking(code)


def _finish_ranking(code):
    room      = rooms[code]
    room["status"] = "done"
    n_players = len(room["players"])
    rank_sums   = {r["slug"]: 0   for r in room["restaurants"]}
    rank_counts = {r["slug"]: 0   for r in room["restaurants"]}
    for p in room["players"].values():
        for pos, slug in enumerate(p.get("rankings", []), start=1):
            if slug in rank_sums:
                rank_sums[slug]   += pos
                rank_counts[slug] += 1
    results = []
    for r in room["restaurants"]:
        slug = r["slug"]
        cnt  = rank_counts.get(slug, 0)
        avg  = round(rank_sums[slug] / cnt, 2) if cnt else 999
        results.append({"restaurant": r, "avg_rank": avg, "yes": n_players, "no": 0, "total": n_players, "pct": 100, "superlike_count": 0})
    results.sort(key=lambda x: x["avg_rank"])
    socketio.emit("game_result", {"winner": results[0] if results else None, "results": results, "mode": "ranking"}, room=code)


def _finish_game(code):
    room      = rooms[code]
    n_players = len(room["players"])
    results = []; perfect = []; superlikes_pool = []
    for r in room["restaurants"]:
        yes_votes       = sum(1 for p in room["players"].values() if p["votes"].get(r["slug"]) is True)
        superlike_count = sum(1 for p in room["players"].values() if p.get("superlike") == r["slug"])
        pct = yes_votes / n_players if n_players else 0
        entry = {"restaurant": r, "yes": yes_votes, "no": n_players - yes_votes,
                 "total": n_players, "pct": round(pct * 100), "superlike_count": superlike_count}
        results.append(entry)
        if pct == 1.0:
            perfect.append(r)
        elif superlike_count > 0:
            superlikes_pool.append(r)
    results.sort(key=lambda x: (-x["superlike_count"], -x["yes"]))
    finalist_slugs = {r["slug"] for r in perfect}
    finalists = list(perfect)
    for r in superlikes_pool:
        if r["slug"] not in finalist_slugs:
            finalists.append(r); finalist_slugs.add(r["slug"])
    if len(finalists) > 1:
        room["status"] = "ranking"
        room["restaurants"] = finalists
        for p in room["players"].values():
            p["done"] = False; p["votes"] = {}; p["rankings"] = []
        socketio.emit("qualification_round", {"restaurants": finalists, "previous_results": results}, room=code)
        return
    if len(finalists) == 1:
        room["status"] = "done"
        entry = next(e for e in results if e["restaurant"]["slug"] == finalists[0]["slug"])
        socketio.emit("game_result", {"winner": entry, "results": results, "mode": "vote"}, room=code)
        return
    room["status"] = "done"
    winner = None
    for entry in results:
        if entry["pct"] >= 70 and (winner is None or entry["yes"] > winner["yes"]):
            winner = entry
    socketio.emit("game_result", {"winner": winner, "results": results, "mode": "vote"}, room=code)


@socketio.on("start_game")
def on_start_game(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return
    room = rooms[code]
    if room.get("host_sid") != request.sid:
        emit("game_error", {"msg": "Only the host can start the game."})
        return
    if room["status"] != "waiting":
        return
    if len(room["players"]) < 2:
        emit("game_error", {"msg": "Need at least 2 players to start."})
        return
    room["status"] = "voting"
    socketio.emit("game_started", {"restaurants": room["restaurants"]}, room=code)


@socketio.on("repull_restaurants")
def on_repull_restaurants(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return
    room = rooms[code]
    if room.get("host_sid") != request.sid or room["status"] != "waiting":
        return
    # Accept optional filter updates from lobby settings
    f = room.get("filters", {})
    if "categories" in data:   f["cats"]       = data["categories"]
    if "max_radius"  in data:  f["max_radius"]  = data.get("max_radius")
    if "fast_only"   in data:  f["fast_only"]   = bool(data["fast_only"])
    if "min_score"   in data:  f["min_score"]   = float(data["min_score"])
    room["filters"] = f
    if "option_count" in data:
        room["option_count"] = max(2, min(40, int(data["option_count"])))
    loc = room.get("location", {})
    try:
        rests  = fetch_restaurants(loc.get("lat", LAT), loc.get("lon", LON))
        pool   = apply_filters(rests, cats=f.get("cats"), max_radius=f.get("max_radius"),
                               fast_only=f.get("fast_only", False), min_score=f.get("min_score", 8.0))
        chosen = random.sample(pool, min(room["option_count"], len(pool)))
        room["restaurants"] = chosen
        room["pool_count"]  = len(pool)
        socketio.emit("restaurants_updated", {
            "restaurants": chosen, "pool_count": len(pool),
            "option_count": room["option_count"],
        }, room=code)
    except Exception as e:
        emit("game_error", {"msg": f"Failed to repull: {e}"})


@socketio.on("restart_game")
def on_restart_game(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return
    room = rooms[code]
    f   = room.get("filters", {})
    loc = room.get("location", {})
    try:
        rests  = fetch_restaurants(loc.get("lat", LAT), loc.get("lon", LON))
        pool   = apply_filters(rests, cats=f.get("cats"), max_radius=f.get("max_radius"),
                               fast_only=f.get("fast_only", False), min_score=f.get("min_score", 8.0))
        if len(pool) >= room["option_count"]:
            room["restaurants"] = random.sample(pool, room["option_count"])
    except Exception:
        pass
    for p in room["players"].values():
        p["done"] = False; p["votes"] = {}; p["superlike"] = None
    room["status"] = "voting"
    socketio.emit("game_started", {"restaurants": room["restaurants"]}, room=code)


@socketio.on("disconnect")
def on_disconnect():
    for code in list(rooms.keys()):
        room = rooms.get(code)
        if not room:
            continue
        if request.sid in room["players"]:
            player_name = room["players"][request.sid]["name"]
            del room["players"][request.sid]
            if not room["players"]:
                del rooms[code]
            else:
                socketio.emit("player_left", {
                    "name":    player_name,
                    "players": [p["name"] for p in room["players"].values()],
                }, room=code)
            break


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
