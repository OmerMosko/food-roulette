# 🎉 Food Roulette

Multiplayer food decision app for Tel Aviv — Tinder-style swiping to agree on where to order.

## Features
- 🎲 Solo mode — spin a random restaurant
- 🎮 Party mode — real-time multiplayer rooms, everyone swipes, 70%+ wins
- ⭐ Superlike — 1 per player, guarantees a restaurant reaches the finals
- 🔥 Tiebreaker ranking round for ties
- 🎵 Web Audio sound effects
- 🛵 Direct Wolt ordering link on winner

## Stack
- Python 3 + Flask + Flask-SocketIO
- Vanilla JS, single HTML file, no build step
- Wolt API for live restaurant data (Tel Aviv)

## Deploy to Railway
1. Fork/clone this repo
2. Connect to [Railway](https://railway.app)
3. Deploy — done

## Local Development
```bash
pip install -r requirements.txt
python server.py
```
Open `http://localhost:8765`
