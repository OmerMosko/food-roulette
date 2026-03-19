#!/usr/bin/env python3
"""Food Roulette — Flask + SocketIO backend."""

import math
import os
import random
import string
from flask import Flask, jsonify, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room as sio_join_room, leave_room

import requests

app = Flask(__name__, static_folder=".")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "food-roulette-secret")

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

LAT = 32.0761450
LON = 34.7809560

TARGET_TAGS = {
    "burgers":       ["burger", "burgers", "hamburger"],
    "pizza":         ["pizza"],
    "mexican":       ["mexican", "tex-mex", "tacos", "burrito"],
    "fried chicken": ["chicken", "fried chicken", "wings"],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
}

# In-memory game rooms
rooms = {}  # code -> room dict


# ── Helpers ─────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((phi2 - phi1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def gen_code():
    while True:
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        if code not in rooms:
            return code


def fetch_restaurants():
    url = f"https://restaurant-api.wolt.com/v1/pages/restaurants?lat={LAT}&lon={LON}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    section = next(
        (s for s in data.get("sections", [])
         if s.get("name") == "restaurants-delivering-venues"),
        None
    )
    if not section:
        return []

    results = []
    seen = set()

    for item in section.get("items", []):
        v = item.get("venue", {})
        if not v:
            continue

        slug = v.get("slug", "")
        if slug in seen:
            continue
        seen.add(slug)

        score  = v.get("rating", {}).get("score", 0) or 0
        volume = v.get("rating", {}).get("volume", 0) or 0
        online = v.get("online", False)
        tags   = [t.lower() for t in v.get("tags", [])]

        cat = None
        for c, keywords in TARGET_TAGS.items():
            if any(k in tags for k in keywords):
                cat = c
                break
        if not cat:
            continue
        if score < 8.0 or volume < 50:
            continue

        desc = (v.get("short_description") or "").replace("\n", " ").strip()[:90]
        loc  = v.get("location") or []
        dist_m = haversine(LAT, LON, loc[1], loc[0]) if len(loc) == 2 else None
        est  = v.get("estimate_range", "")

        raw_img   = (item.get("image") or {}).get("url") or \
                    (v.get("brand_image") or {}).get("url") or ""
        image_url = (raw_img + "?w=600") if raw_img else ""

        results.append({
            "name":      v.get("name", ""),
            "slug":      slug,
            "category":  cat,
            "score":     score,
            "volume":    volume,
            "online":    online,
            "desc":      desc,
            "estimate":  est,
            "dist_m":    dist_m,
            "image_url": image_url,
            "wolt":      f"https://wolt.com/en/isr/tel-aviv/restaurant/{slug}",
        })

    results.sort(key=lambda x: (-x["score"], -x["volume"]))
    return results


def apply_filters(rests, cats=None, max_radius=None, fast_only=False, open_only=True):
    out = []
    for r in rests:
        if open_only and not r["online"]:
            continue
        if cats and r["category"] not in cats:
            continue
        if max_radius and (r.get("dist_m") is None or r["dist_m"] > max_radius):
            continue
        if fast_only:
            try:
                upper = int(r["estimate"].split("-")[-1])
                if upper > 45:
                    continue
            except Exception:
                pass
        out.append(r)
    return out


# ── REST endpoints ───────────────────────────────────────────────
@app.route("/api/restaurants")
def api_restaurants():
    try:
        return jsonify({"ok": True, "restaurants": fetch_restaurants()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/pool-count")
def api_pool_count():
    try:
        cats_param  = request.args.get("cats", "")
        cats        = [c.strip() for c in cats_param.split(",") if c.strip()] or None
        max_radius  = int(request.args.get("max_radius", 0)) or None
        fast_only   = request.args.get("fast_only", "false").lower() == "true"
        rests       = fetch_restaurants()
        pool        = apply_filters(rests, cats=cats, max_radius=max_radius, fast_only=fast_only)
        return jsonify({"ok": True, "count": len(pool)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/party")
def party():
    return send_from_directory(".", "party.html")


# ── SocketIO: game events ────────────────────────────────────────
@socketio.on("create_room")
def on_create_room(data):
    host_name    = (data.get("host_name") or "Host").strip() or "Host"
    player_count = max(2, min(8,  int(data.get("player_count", 4))))
    option_count = max(2, min(40, int(data.get("option_count", player_count))))
    cats         = data.get("categories") or list(TARGET_TAGS.keys())
    max_radius   = data.get("max_radius")   # int or None
    fast_only    = bool(data.get("fast_only", False))

    try:
        rests = fetch_restaurants()
    except Exception as e:
        emit("game_error", {"msg": f"Failed to fetch restaurants: {e}"})
        return

    pool = apply_filters(rests, cats=cats, max_radius=max_radius, fast_only=fast_only)
    if len(pool) < option_count:
        emit("game_error", {
            "msg": f"Only {len(pool)} restaurants match your filters "
                   f"(need at least {option_count}). Try relaxing the filters."
        })
        return

    chosen = random.sample(pool, option_count)
    code   = gen_code()

    rooms[code] = {
        "host_sid":    request.sid,
        "player_count": player_count,
        "option_count": option_count,
        "players":     {request.sid: {"name": host_name, "done": False, "votes": {}, "superlike": None}},
        "restaurants": chosen,
        "status":      "waiting",
        "filters":     {"cats": cats, "max_radius": max_radius, "fast_only": fast_only},
        "pool":        pool,
    }

    sio_join_room(code)
    emit("room_created", {
        "code":         code,
        "restaurants":  chosen,
        "pool_count":   len(pool),
        "player_count": player_count,
        "players":      [rooms[code]["players"][request.sid]["name"]],
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

    if len(room["players"]) >= room["player_count"]:
        emit("game_error", {"msg": "Room is full."})
        return

    sio_join_room(code)
    room["players"][request.sid] = {"name": name, "done": False, "votes": {}, "superlike": None}

    player_names = [p["name"] for p in room["players"].values()]

    emit("joined_game", {
        "code":         code,
        "restaurants":  room["restaurants"],
        "pool_count":   len(room.get("pool") or []),
        "player_count": room["player_count"],
        "players":      player_names,
    })

    socketio.emit("player_joined", {
        "players":      player_names,
        "player_count": room["player_count"],
    }, room=code)

    # Auto-start when full
    if len(room["players"]) >= room["player_count"]:
        room["status"] = "voting"
        socketio.emit("game_started", {"restaurants": room["restaurants"]}, room=code)


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

    # Broadcast vote progress (without revealing individual votes)
    total_rests = len(room["restaurants"])
    voted_counts = {
        p["name"]: len(p["votes"])
        for p in room["players"].values()
    }
    socketio.emit("vote_progress", {"progress": voted_counts, "total": total_rests}, room=code)

    # Mark done when all restaurants judged
    if len(room["players"][sid]["votes"]) >= total_rests:
        room["players"][sid]["done"] = True

    # Check if all done
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
    player["votes"][slug] = True  # also counts as a like

    total_rests = len(room["restaurants"])
    voted_counts = {
        p["name"]: len(p["votes"])
        for p in room["players"].values()
    }

    # Broadcast anonymously — just the slug so everyone sees the ⭐ badge
    socketio.emit("superlike_cast", {
        "slug":     slug,
        "progress": voted_counts,
        "total":    total_rests,
    }, room=code)

    # Move to next card (treated as already voted)
    if len(player["votes"]) >= total_rests:
        player["done"] = True

    if all(p["done"] for p in room["players"].values()):
        _finish_game(code)


@socketio.on("cast_ranking")
def on_cast_ranking(data):
    code     = (data.get("code") or "").upper()
    rankings = data.get("rankings") or []   # [slug1, slug2, ...] best→worst

    if code not in rooms:
        return

    room = rooms[code]
    sid  = request.sid

    if sid not in room["players"] or room["status"] != "ranking":
        return

    room["players"][sid]["rankings"] = rankings
    room["players"][sid]["done"]     = True

    # Progress broadcast
    done_count  = sum(1 for p in room["players"].values() if p["done"])
    total_count = len(room["players"])
    socketio.emit("ranking_progress", {
        "done": done_count, "total": total_count
    }, room=code)

    # All submitted → tally
    if done_count >= total_count:
        _finish_ranking(code)


def _finish_ranking(code):
    room      = rooms[code]
    room["status"] = "done"
    n_players = len(room["players"])

    # Average rank per slug (1-indexed, lower = better)
    rank_sums  = {r["slug"]: 0 for r in room["restaurants"]}
    rank_counts = {r["slug"]: 0 for r in room["restaurants"]}

    for p in room["players"].values():
        for pos, slug in enumerate(p.get("rankings", []), start=1):
            if slug in rank_sums:
                rank_sums[slug]  += pos
                rank_counts[slug] += 1

    results = []
    for r in room["restaurants"]:
        slug = r["slug"]
        cnt  = rank_counts.get(slug, 0)
        avg  = round(rank_sums[slug] / cnt, 2) if cnt else 999
        results.append({
            "restaurant": r,
            "avg_rank":   avg,
            "yes":   n_players,
            "no":    0,
            "total": n_players,
            "pct":   100,
        })

    results.sort(key=lambda x: x["avg_rank"])
    winner = results[0] if results else None

    socketio.emit("game_result", {
        "winner":  winner,
        "results": results,
        "mode":    "ranking",
    }, room=code)


def _finish_game(code):
    room      = rooms[code]
    n_players = len(room["players"])

    results   = []
    perfect   = []   # 100% normal approval
    superlikes_pool = []  # got ≥1 superlike

    for r in room["restaurants"]:
        yes_votes = sum(
            1 for p in room["players"].values()
            if p["votes"].get(r["slug"]) is True
        )
        superlike_count = sum(
            1 for p in room["players"].values()
            if p.get("superlike") == r["slug"]
        )
        pct = yes_votes / n_players if n_players else 0
        entry = {
            "restaurant":     r,
            "yes":            yes_votes,
            "no":             n_players - yes_votes,
            "total":          n_players,
            "pct":            round(pct * 100),
            "superlike_count": superlike_count,
        }
        results.append(entry)
        if pct == 1.0:
            perfect.append(r)
        elif superlike_count > 0:
            superlikes_pool.append(r)

    results.sort(key=lambda x: (-x["superlike_count"], -x["yes"]))

    # Finalists = 100%-voted + superlikes (deduplicated)
    finalist_slugs = {r["slug"] for r in perfect}
    finalists = list(perfect)
    for r in superlikes_pool:
        if r["slug"] not in finalist_slugs:
            finalists.append(r)
            finalist_slugs.add(r["slug"])

    # Multiple finalists → ranking tiebreaker
    if len(finalists) > 1:
        room["status"]      = "ranking"
        room["restaurants"] = finalists
        for p in room["players"].values():
            p["done"]     = False
            p["votes"]    = {}
            p["rankings"] = []
        socketio.emit("qualification_round", {
            "restaurants":      finalists,
            "previous_results": results,
        }, room=code)
        return

    # Single finalist (100% or superlikes) → instant winner
    if len(finalists) == 1:
        room["status"] = "done"
        entry = next(e for e in results if e["restaurant"]["slug"] == finalists[0]["slug"])
        socketio.emit("game_result", {"winner": entry, "results": results, "mode": "vote"}, room=code)
        return

    # Normal finish: find winner at ≥70%
    room["status"] = "done"
    winner = None
    for entry in results:
        if entry["pct"] >= 70 and (winner is None or entry["yes"] > winner["yes"]):
            winner = entry

    socketio.emit("game_result", {"winner": winner, "results": results, "mode": "vote"}, room=code)


@socketio.on("repull_restaurants")
def on_repull_restaurants(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return

    room = rooms[code]
    if room.get("host_sid") != request.sid:
        return  # only host can repull
    if room["status"] != "waiting":
        return  # can't repull after game started

    filters = room["filters"]
    try:
        rests = fetch_restaurants()
        pool  = apply_filters(rests,
                              cats=filters["cats"],
                              max_radius=filters["max_radius"],
                              fast_only=filters["fast_only"])
    except Exception as e:
        emit("game_error", {"msg": f"Failed to fetch restaurants: {e}"})
        return

    n = room.get("option_count", room["player_count"])
    if len(pool) < n:
        emit("game_error", {"msg": f"Only {len(pool)} restaurants match your filters (need {n})."})
        return

    chosen = random.sample(pool, n)
    room["restaurants"] = chosen
    room["pool"] = pool

    socketio.emit("restaurants_updated", {
        "restaurants": chosen,
        "pool_count":  len(pool),
    }, room=code)


@socketio.on("restart_game")
def on_restart_game(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return

    room = rooms[code]
    filters = room["filters"]

    try:
        rests = fetch_restaurants()
        pool  = apply_filters(rests,
                              cats=filters["cats"],
                              max_radius=filters["max_radius"],
                              fast_only=filters["fast_only"])
        n = room.get("option_count", room["player_count"])
        if len(pool) >= n:
            room["restaurants"] = random.sample(pool, n)
    except Exception:
        pass  # keep same restaurants

    for p in room["players"].values():
        p["done"]      = False
        p["votes"]     = {}
        p["superlike"] = None

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
