#!/usr/bin/env python3
"""Food Roulette — Flask + SocketIO backend. Restaurant data fetched client-side."""

import os
import random
import string
from flask import Flask, send_from_directory
from flask_socketio import SocketIO, emit, join_room as sio_join_room

app = Flask(__name__, static_folder=".")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "food-roulette-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory game rooms
rooms = {}  # code -> room dict


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


# ── SocketIO: game events ────────────────────────────────────────
@socketio.on("create_room")
def on_create_room(data):
    host_name    = (data.get("host_name") or "Host").strip() or "Host"
    player_count = max(2, min(8, int(data.get("player_count", 4))))
    restaurants  = data.get("restaurants") or []
    pool_count   = int(data.get("pool_count", len(restaurants)))

    if not restaurants:
        emit("game_error", {"msg": "No restaurants to play with — check your filters."})
        return

    code = gen_code()
    rooms[code] = {
        "host_sid":     request.sid,
        "player_count": player_count,
        "option_count": len(restaurants),
        "players":      {request.sid: {"name": host_name, "done": False, "votes": {}, "superlike": None}},
        "restaurants":  restaurants,
        "pool_count":   pool_count,
        "status":       "waiting",
    }

    sio_join_room(code)
    emit("room_created", {
        "code":         code,
        "restaurants":  restaurants,
        "pool_count":   pool_count,
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


@socketio.on("submit_restaurants")
def on_submit_restaurants(data):
    """Host submits a fresh restaurant list (after repull)."""
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return
    room = rooms[code]
    if room.get("host_sid") != request.sid or room["status"] != "waiting":
        return
    restaurants = data.get("restaurants") or []
    pool_count  = int(data.get("pool_count", len(restaurants)))
    if not restaurants:
        return
    room["restaurants"] = restaurants
    room["pool_count"]  = pool_count
    socketio.emit("restaurants_updated", {"restaurants": restaurants, "pool_count": pool_count}, room=code)


@socketio.on("restart_game")
def on_restart_game(data):
    code = (data.get("code") or "").upper()
    if code not in rooms:
        return
    room = rooms[code]
    for p in room["players"].values():
        p["done"] = False; p["votes"] = {}; p["superlike"] = None
    room["status"] = "voting"
    # Ask host to supply a fresh restaurant list; fall back to current list after 3s
    emit("request_restart_restaurants", {"code": code})
    # Guests see the same list if host doesn't respond
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


# Need request context for socket handlers
from flask import request   # noqa: E402 (must be after app init)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
