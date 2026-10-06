"""SimHost: одна проверка игрового сервера -> сообщение в Discord через вебхук.

Запускается по расписанию в GitHub Actions. Состояние (кто был онлайн
в прошлый раз) хранится в state.json в репозитории.
"""
import json
import os
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
HOST = os.getenv("SERVER_HOST", "https://gamemonitoring.ru/killing-floor/servers/10679024")
PORT = int(os.getenv("QUERY_PORT", "7708"))  # у Killing Floor query-порт обычно игровой + 1
TITLE = os.getenv("SERVER_TITLE", "Simhost")
BOT_NAME = os.getenv("BOT_NAME", "SimHost")
STATE_FILE = os.getenv("STATE_FILE", "state.json")


def fetch():
    import a2s  # импорт здесь, чтобы скрипт можно было тестировать без библиотеки

    addr = (HOST, PORT)
    info = a2s.info(addr, timeout=5)
    players = a2s.players(addr, timeout=5)
    game_type = None
    try:
        rules = a2s.rules(addr, timeout=5)
        for key in ("GameType", "gametype", "game_type", "GameMode", "mutator_gametype"):
            if key in rules:
                game_type = rules[key]
                break
    except Exception:
        pass
    names = [p.name.strip() for p in players if p.name and p.name.strip()]
    return info.map_name, names, str(game_type or info.game or "—")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f).get("players", [])
    except (FileNotFoundError, ValueError):
        return []


def save_state(names):
    data = {"players": sorted(names)}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            if json.load(f) == data:
                return  # ничего не изменилось — не трогаем файл
    except (FileNotFoundError, ValueError):
        pass
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def build_payload(title, map_name, names, game_type):
    players_text = "\n".join(names) if names else "—"
    if len(players_text) > 1000:
        players_text = players_text[:1000] + "…"
    return {
        "username": BOT_NAME,
        "embeds": [
            {
                "title": f"🟢 {title}",
                "description": "Игроки на сервере. Заходи!",
                "color": 0x57F287,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "fields": [
                    {"name": "Server", "value": TITLE, "inline": False},
                    {"name": "Map", "value": map_name or "—", "inline": True},
                    {"name": "Game Type", "value": game_type, "inline": True},
                    {"name": f"Players ({len(names)})", "value": players_text, "inline": False},
                ],
            }
        ],
    }


def post(payload):
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "SimHost-bot/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    if not WEBHOOK:
        print("Не задан секрет DISCORD_WEBHOOK_URL")
        return 1

    try:
        map_name, names, game_type = fetch()
    except Exception as e:
        print(f"Сервер не ответил: {e}")
        return 0  # состояние не меняем, попробуем в следующий раз

    prev = Counter(load_state())
    cur = Counter(names)
    joined = list((cur - prev).elements())
    was_empty = sum(prev.values()) == 0

    if joined:
        title = "Game is live!" if was_empty else f"Подключился: {', '.join(joined)}"
        try:
            post(build_payload(title, map_name, names, game_type))
        except Exception as e:
            print(f"Не удалось отправить в Discord: {e}")
            return 1  # состояние не сохраняем, повторим при следующей проверке
        print(f"Отправлено: {title}")
    else:
        print(f"Без изменений. Онлайн: {len(names)}")

    save_state(names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
