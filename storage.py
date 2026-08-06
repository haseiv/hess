"""
Простое хранилище настроек по каждому серверу (guild) в JSON-файле.
Для небольшого/среднего бота этого достаточно. При росте нагрузки
можно заменить на SQLite/Postgres, не меняя интерфейс функций.
"""

import json
import os
import copy
from threading import Lock

# Каталог для данных. В Docker задаётся через ENV DATA_DIR=/app/data
# (примонтируйте туда volume, чтобы настройки переживали перезапуск).
# Если переменная не задана — пишем рядом с проектом, как раньше.
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.dirname(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)
_DATA_FILE = os.path.join(_DATA_DIR, "data.json")
_lock = Lock()

# Настройки по умолчанию для каждого нового сервера
_DEFAULT_GUILD = {
    "log_channel": None,  # канал для логов защиты
    "protection": {
        "antispam": True,
        "antispam_limit": 5,        # сообщений
        "antispam_interval": 5,     # за сколько секунд
        "antispam_timeout": 300,    # мут в секундах при спаме
        "antiinvite": True,         # удалять чужие приглашения
        "antiraid": True,
        "antiraid_joins": 6,        # заходов
        "antiraid_interval": 10,    # за сколько секунд = рейд
        "antinuke": True,
        "antinuke_limit": 3,        # опасных действий
        "antinuke_interval": 12,    # за сколько секунд
        "whitelist": [],            # id пользователей, которым всё можно
    },
    "tickets": {
        "category": None,           # id категории для тикетов
        "support_role": None,       # id роли поддержки
        "log_channel": None,        # канал логов тикетов
        "panel_title": "🎫 Поддержка",
        "panel_description": "Нажмите кнопку ниже, чтобы создать тикет. "
                             "Команда поддержки ответит вам как можно скорее.",
        "counter": 0,               # счётчик номеров тикетов
        "open": {},                 # {channel_id: {"user": id, "claimed_by": id|null}}
    },
}


def _load():
    if not os.path.exists(_DATA_FILE):
        return {}
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data):
    tmp = _DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DATA_FILE)  # атомарная запись


def _deep_merge(defaults, actual):
    """Дополняет actual недостающими ключами из defaults."""
    result = copy.deepcopy(defaults)
    for key, value in actual.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def get_guild(guild_id):
    """Вернуть настройки сервера (с подставленными значениями по умолчанию)."""
    with _lock:
        data = _load()
        raw = data.get(str(guild_id), {})
        return _deep_merge(_DEFAULT_GUILD, raw)


def save_guild(guild_id, settings):
    """Сохранить настройки сервера целиком."""
    with _lock:
        data = _load()
        data[str(guild_id)] = settings
        _save(data)


def update_guild(guild_id, **changes):
    """Обновить настройки верхнего уровня точечно."""
    settings = get_guild(guild_id)
    settings.update(changes)
    save_guild(guild_id, settings)
    return settings
