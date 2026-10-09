"""SimHost: одна проверка игрового сервера -> сообщение в Discord через вебхук.

Запускается по расписанию в GitHub Actions. Состояние (кто был онлайн
в прошлый раз) хранится в state.json в репозитории.

Опрос сервера: Killing Floor отвечает по собственному протоколу Unreal
(порт «игровой + 1», у вас 7708), а не по Steam A2S. Скрипт пробует оба.
"""
import json
import os
import re
import socket
import struct
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timezone

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
HOST = os.getenv("SERVER_HOST", "direct.simhost2026.ru")
# Порты через запятую. Для каждого пробуются оба протокола: Unreal и Steam A2S.
PORTS = [int(p) for p in os.getenv("QUERY_PORT", "7708,28852,7707").replace(" ", "").split(",") if p]
TITLE = os.getenv("SERVER_TITLE", "Simhost")
BOT_NAME = os.getenv("BOT_NAME", "SimHost")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# ----------------------------------------------------------------------------
# Протокол Unreal (Killing Floor / UT2004)
# ----------------------------------------------------------------------------

REQ_DETAILS = b"\x79\x00\x00\x00\x00"
REQ_RULES = b"\x79\x00\x00\x00\x01"
REQ_PLAYERS = b"\x79\x00\x00\x00\x02"


def _decode(raw):
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", "replace")


class Reader:
    def __init__(self, data):
        self.b = data
        self.i = 0

    def left(self):
        return len(self.b) - self.i

    def int32(self):
        v = struct.unpack_from("<i", self.b, self.i)[0]
        self.i += 4
        return v

    def pstr(self):
        n = self.b[self.i]
        self.i += 1
        if n & 0x80:  # юникод: (n - 0x80) символов UTF-16, включая завершающий
            cnt = n & 0x7F
            raw = self.b[self.i:self.i + cnt * 2]
            self.i += cnt * 2
            txt = raw.decode("utf-16-le", "replace")
        else:
            raw = self.b[self.i:self.i + n]
            self.i += n
            raw = re.sub(rb"\x1b[\x00-\xff]{3}", b"", raw)  # цветовые коды до декодирования
            txt = _decode(raw)
        txt = txt.replace("\x00", "")
        return re.sub(r"\x1b[\s\S]{3}", "", txt).strip()  # цветовые коды ников


def _recv_parts(sock, header, timeout):
    """Собрать все датаграммы с нужным заголовком (ответ может прийти в нескольких)."""
    sock.settimeout(timeout)
    parts = []
    while True:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            break
        if data[:5] == header:
            parts.append(data[5:])
            sock.settimeout(0.7)
    if not parts:
        raise TimeoutError("нет ответа (timed out)")
    return b"".join(parts)


def _ask(sock, addr, request, timeout=4):
    sock.sendto(request, addr)
    return _recv_parts(sock, b"\x80\x00\x00\x00" + request[4:5], timeout)


def fetch_unreal(ip, port):
    addr = (ip, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        payload = _ask(sock, addr, REQ_DETAILS)
        try:
            r = Reader(payload)
            r.int32()      # server id
            r.pstr()       # server ip
            r.int32()      # game port
            r.int32()      # query port
            r.pstr()       # server name
            map_name = r.pstr()
            game_type = r.pstr()
        except (struct.error, IndexError):
            raise ValueError("не удалось разобрать ответ details: " + payload[:80].hex())

        names = []
        try:
            r = Reader(_ask(sock, addr, REQ_PLAYERS))
            while r.left() >= 4:
                r.int32()   # id игрока
                name = r.pstr()
                r.int32()   # ping
                r.int32()   # score
                r.int32()   # stats id
                if name:
                    names.append(name)
        except (struct.error, IndexError):
            pass  # обрезанный хвост: оставляем то, что успели разобрать
        except TimeoutError:
            pass

        rules = {}
        try:
            r = Reader(_ask(sock, addr, REQ_RULES))
            while r.left() > 0:
                k = r.pstr().lower()
                v = r.pstr()
                if k:
                    rules.setdefault(k, v)
        except (struct.error, IndexError, TimeoutError):
            pass

        if rules:
            print("правила сервера:", json.dumps(rules, ensure_ascii=False)[:600])
        shown_type = None
        for key in ("gametype", "game type", "gamemode", "game mode", "servermode"):
            if rules.get(key):
                shown_type = rules[key]
                break
        return map_name, names, str(shown_type or game_type or "—")
    finally:
        sock.close()


# ----------------------------------------------------------------------------
# Steam A2S (запасной вариант)
# ----------------------------------------------------------------------------

def fetch_a2s(ip, port):
    import a2s  # импорт здесь: библиотека нужна только как запасной вариант

    addr = (ip, port)
    info = a2s.info(addr, timeout=4)
    players = a2s.players(addr, timeout=4)
    game_type = None
    try:
        rules = a2s.rules(addr, timeout=4)
        for key in ("GameType", "gametype", "game_type", "GameMode", "mutator_gametype"):
            if key in rules:
                game_type = rules[key]
                break
    except Exception:
        pass
    names = [p.name.strip() for p in players if p.name and p.name.strip()]
    return info.map_name, names, str(game_type or info.game or "—")


def fetch():
    ip = socket.gethostbyname(HOST)
    print(f"Опрашиваю {HOST} ({ip}), порты: {PORTS}")
    errors = []
    for port in PORTS:
        for label, func in (("Unreal", fetch_unreal), ("A2S", fetch_a2s)):
            try:
                result = func(ip, port)
                print(f"Ответил порт {port} по протоколу {label}")
                return result
            except Exception as e:
                msg = f"порт {port} / {label}: {type(e).__name__}: {e}"
                print(msg)
                errors.append(msg)
    raise RuntimeError("ни один порт не ответил")


# ----------------------------------------------------------------------------
# Состояние и отправка в Discord
# ----------------------------------------------------------------------------

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
