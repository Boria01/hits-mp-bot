import os
import re
import html
import json
import time
import random
import subprocess
import urllib.parse
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# --- НАСТРОЙКИ ИЗ GITHUB SECRETS / VARIABLES ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@hits_mp")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

# Если задано в Secrets — берём оттуда; если пусто — получаем список из API (0 квоты тратится)
ENV_GEMINI_MODELS = os.environ.get("GEMINI_MODELS", "").strip()

PARTNER_SEARCH_TEMPLATE = os.environ.get("PARTNER_SEARCH_TEMPLATE", "").strip()
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "20"))
MAX_AI_CALLS_PER_DAY = int(os.environ.get("MAX_AI_CALLS_PER_DAY", "180"))
MAX_RUNTIME_SECONDS = int(5.5 * 3600)

BATCH_SIZE = 5
MAX_AI_BATCH_CALLS_PER_CYCLE = 2
MAX_ATTEMPTS_PER_POST = 3

HISTORY_FILE = "history.json"
CHANNELS_POOL_FILE = "channels_pool.json"
REJECTED_FILE = "rejected_channels.json"
STATE_FILE = "state.json"
MSK_TZ = timezone(timedelta(hours=3))

MAX_VIDEO_BYTES = 48 * 1024 * 1024   # лимит Bot API ~50 МБ
MAX_PHOTO_BYTES = 9 * 1024 * 1024    # лимит sendPhoto 10 МБ

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

INITIAL_SEED_CHANNELS = [
    "ali_muzhik",
    "aliexpress_rus",
    "nahodki_aliexpress",
    "kitaec_ru",
    "aligadgets",
    "ali_express_sale",
    "aliexpress_top_ru",
    "ali_pro",
    "gadget_ali",
    "china_goods_top",
]
SEED_LOWER = {s.lower() for s in INITIAL_SEED_CHANNELS}

IGNORED_USERNAMES = {
    "s", "joinchat", "share", "proxy", "iv", "addstickers", "setlanguage",
    "botfather", "telegram", "durov", "username", "login", "contact",
    "vcnews", "addlist", "tgstat", "telemetr", "tlgrm", "pikabu", "ixbt_official",
}

CHANNEL_TOPIC_KEYWORDS = (
    "aliexpress", "алиэкспресс", "яндекс маркет", "яндекс.маркет",
    "гаджет", "техник", "электроник", "инструмент", "автотовар",
    "находк", "распаковк", "китай", "скидк", "обзор", "товар", "покупк",
)

POST_STOP_WORDS = (
    "wildberries", "вайлдберриз", "артикул wb", "арт. wb", "арт wb",
    "на вб", "с вб", "артикул озон", "арт. озон", "готовый образ", "лук на",
    "казино", "ставки на спорт", "букмекер", "криптовалют", "заработок в интернете",
    "подписывайся на наш", "розыгрыш среди подписчиков", "итоги конкурса",
)

POST_FILTER_KEYWORDS = (
    "руб", "₽", "цена", "скидк", "$", "купить", "заказ",
    "гаджет", "аккумулятор", "мощност", "обзор", "распаковк", "aliexpress", "алиэкспресс",
)

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ---
MODEL_COOLDOWN = {}
ATTEMPTS = {}
STATE = {"last_post_ts": 0.0, "ai_day": "", "ai_calls": 0}
REJECTED = []


class QuotaExhausted(Exception):
    pass


def log(msg: str):
    now_str = datetime.now(MSK_TZ).strftime("%H:%M:%S")
    print(f"[{now_str} МСК] {msg}", flush=True)


# ---------------------------------------------------------------- файлы/git

def load_json(filepath: str, default):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(default, list) and isinstance(data, list):
                    return data
                if isinstance(default, dict) and isinstance(data, dict):
                    return data
        except Exception:
            pass
    return default.copy()


def save_and_push_state(history: list, channels_pool: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-2500:], f, ensure_ascii=False, indent=2)
    with open(CHANNELS_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(channels_pool[-300:], f, ensure_ascii=False, indent=2)
    with open(REJECTED_FILE, "w", encoding="utf-8") as f:
        json.dump(REJECTED[-1000:], f, ensure_ascii=False, indent=2)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)

    try:
        files = [HISTORY_FILE, CHANNELS_POOL_FILE, REJECTED_FILE, STATE_FILE]
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "add", *files], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update bot state"], check=False)
            subprocess.run(["git", "pull", "--rebase", "-X", "ours"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"Состояние сохранено (история: {len(history)}, каналов: {len(channels_pool)}, ЧС: {len(REJECTED)}).")
    except Exception as e:
        log(f"Ошибка синхронизации с GitHub: {e}")


# ---------------------------------------------------------------- инициализация AI

def init_gemini_client_and_models():
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=45000)
    )

    if ENV_GEMINI_MODELS:
        models = [m.strip() for m in ENV_GEMINI_MODELS.split(",") if m.strip()]
        log(f"Использую заданные модели из ENV: {models}")
        return client, models

    # models.list() не расходует дневную квоту generateContent и защищает от 404 при смене версий
    raw_models = list(client.models.list())
    candidates = []

    for m in raw_models:
        name = getattr(m, "name", "")
        if not name:
            continue
        clean_name = name.replace("models/", "")
        lower_name = clean_name.lower()

        if not ("gemini" in lower_name or "gemma-3" in lower_name):
            continue
        if any(bad in lower_name for bad in ("embedding", "imagen", "veo", "tts", "audio", "aqa", "image", "computer-use", "pro")):
            continue

        actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None)
        if actions and "generateContent" not in actions:
            continue

        candidates.append(clean_name)

    candidates.sort(key=lambda x: (
        0 if "flash-lite" in x.lower() else (1 if "flash" in x.lower() else 2),
        x
    ))
    log(f"Список моделей из API (0 квоты потрачено): {candidates}")
    return client, candidates


# ---------------------------------------------------------------- каналы

def reject_channel(channel: str):
    if channel.lower() not in [r.lower() for r in REJECTED]:
        REJECTED.append(channel)


def try_add_channel(ch: str, channels_pool: list, own_channel: str) -> bool:
    ch_l = ch.lower()
    if ch_l in IGNORED_USERNAMES or ch_l == own_channel or ch_l.endswith("bot"):
        return False
    if "wb" in ch_l or "wildberries" in ch_l or "news" in ch_l:
        return False
    if ch_l in [r.lower() for r in REJECTED]:
        return False
    if ch_l in [c.lower() for c in channels_pool]:
        return False
    channels_pool.append(ch)
    return True


def discover_channels_from_mysku(channels_pool: list):
    own_channel = CHANNEL_ID.lstrip("@").lower()
    url = "https://mysku.club/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            found = re.findall(r"t\.me/([a-zA-Z0-9_]{5,32})", resp.text)
            added = sum(1 for ch in found if try_add_channel(ch, channels_pool, own_channel))
            if added:
                log(f"С mysku.club добавлено каналов-кандидатов: +{added} (всего: {len(channels_pool)})")
    except Exception as e:
        log(f"Ошибка обхода mysku.club: {e}")


def is_valid_target_channel(channel_name: str, soup: BeautifulSoup) -> bool:
    ch_lower = channel_name.lower()
    if "wb" in ch_lower or "wildberries" in ch_lower:
        return False

    title_el = soup.select_one(".tgme_channel_info_header_title")
    desc_el = soup.select_one(".tgme_channel_info_description")
    title_text = title_el.get_text(" ", strip=True).lower() if title_el else ""
    desc_text = desc_el.get_text(" ", strip=True).lower() if desc_el else ""

    if not title_text:
        return False
    if "wildberries" in title_text or "вайлдберриз" in title_text:
        return False
    if ch_lower in SEED_LOWER:
        return True

    full_header = f"{ch_lower} {title_text} {desc_text}"
    return any(kw in full_header for kw in CHANNEL_TOPIC_KEYWORDS)


# ---------------------------------------------------------------- тексты

def clean_donor_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"erid:\s*\S+", "", text, flags=re.IGNORECASE)
    return text.strip()


def make_text_hash(raw_text: str) -> str:
    cleaned = clean_donor_text(raw_text).lower()
    norm = re.sub(r"\W+", "", cleaned)[:80]
    return f"txt:{norm}" if len(norm) >= 20 else ""


def looks_like_target_post(text: str) -> bool:
    t_lower = text.lower()
    if len(t_lower) < 35 or len(t_lower) > 1800:
        return False
    if any(sw in t_lower for sw in POST_STOP_WORDS):
        return False
    has_digits = bool(re.search(r"\d{2,}", t_lower))
    has_kw = any(k in t_lower for k in POST_FILTER_KEYWORDS)
    return has_digits and has_kw


def parse_telegram_channel_and_discover(channel: str, channels_pool: list) -> list:
    url = f"https://t.me/s/{channel}"
    own_channel = CHANNEL_ID.lstrip("@").lower()
    results = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 404:
            log(f"Канал @{channel} не найден (404).")
            if channel.lower() not in SEED_LOWER:
                if channel in channels_pool:
                    channels_pool.remove(channel)
                reject_channel(channel)
            return []
        if resp.status_code != 200:
            log(f"Канал @{channel}: HTTP {resp.status_code}, пропускаю на этот проход.")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        if not is_valid_target_channel(channel, soup):
            log(f"Канал @{channel} пустой или не по теме — в чёрный список.")
            if channel in channels_pool and channel.lower() not in SEED_LOWER:
                channels_pool.remove(channel)
                reject_channel(channel)
            return []

        for d_ch in re.findall(r"t\.me/(?:s/)?([a-zA-Z0-9_]{5,32})", resp.text):
            try_add_channel(d_ch, channels_pool, own_channel)

        for msg in soup.select(".tgme_widget_message_wrap"):
            msg_div = msg.select_one(".tgme_widget_message")
            if not msg_div:
                continue
            post_id = msg_div.get("data-post")
            if not post_id:
                continue

            text_div = msg.select_one(".tgme_widget_message_text")
            raw_text = text_div.get_text("\n").strip() if text_div else ""
            if not looks_like_target_post(raw_text):
                continue

            base = {
                "id": f"tg:{post_id}",
                "txt_hash": make_text_hash(raw_text),
                "raw_text": raw_text,
            }

            video_tag = msg.select_one("video.tgme_widget_message_video")
            if video_tag and video_tag.get("src"):
                results.append({**base, "type": "video", "media_url": video_tag["src"]})
                continue

            photo_wrap = msg.select_one("a.tgme_widget_message_photo_wrap")
            if photo_wrap and photo_wrap.get("style"):
                bg = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", photo_wrap["style"])
                if bg:
                    results.append({**base, "type": "photo", "media_url": bg.group(1)})

        log(f"@{channel}: кандидатов с медиа: {len(results)}")
    except Exception as e:
        log(f"Ошибка чтения @{channel}: {e}")

    return results


# ---------------------------------------------------------------- Gemini

def count_ai_call():
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    if STATE.get("ai_day") != today:
        STATE["ai_day"] = today
        STATE["ai_calls"] = 0
    STATE["ai_calls"] += 1


def ai_daily_limit_reached() -> bool:
    today = datetime.now(MSK_TZ).strftime("%Y-%m-%d")
    if STATE.get("ai_day") != today:
        return False
    return STATE.get("ai_calls", 0) >= MAX_AI_CALLS_PER_DAY


def generate_batch_content(client, models: list, batch: list) -> dict:
    """Одна пачка постов -> один запрос. Возвращает {idx: entry}."""
    if ai_daily_limit_reached():
        log(f"Суточный лимит AI-запросов ({MAX_AI_CALLS_PER_DAY}) исчерпан.")
        raise QuotaExhausted()

    current_date = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    items_block = []
    for idx, item in enumerate(batch, start=1):
        cleaned = clean_donor_text(item["raw_text"])[:700]
        items_block.append(f'IDX: {idx}\nТЕКСТ:\n"""{cleaned}"""')
    joined_items = "\n\n---\n\n".join(items_block)

    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов» (упор на AliExpress и Яндекс Маркет).
Сегодняшняя дата: {current_date} (учитывай текущий сезон, не пиши про весну/лето, если сейчас осень/зима).

Проанализируй каждый пост (по его числовому IDX) и верни строго JSON-массив объектов (без markdown-обёртки).
Для каждого IDX:
- Если это НЕ физический товар (новость, мем, реклама канала, бота, казино, ставок, курсов, кредитов, подписки), подборка/коллаж из нескольких разных вещей одежды (женский образ/лук) или товар строго с Wildberries/Ozon — верни:
  {{"idx": 1, "is_product": false}}
- Если это ОДИН конкретный крутой товар (гаджет, электроника, инструмент, автотовар, полезная вещь для дома/кухни) — верни:
  {{
    "idx": 1,
    "is_product": true,
    "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале (без HTML-тегов)",
    "description": "Живое описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками. Без HTML-тегов, без воды, без упоминаний WB/Ozon, без чужих каналов, ссылок и erid.",
    "search_query": "Точное название товара для поиска на AliExpress и Яндекс Маркете (4-7 слов)"
  }}

Посты для проверки:
{joined_items}
"""
    now = time.time()
    for m in list(models):
        if MODEL_COOLDOWN.get(m, 0) > now:
            continue
        try:
            kwargs = {"model": m, "contents": prompt}
            if "gemma" not in m.lower():
                kwargs["config"] = types.GenerateContentConfig(response_mime_type="application/json")

            count_ai_call()
            resp = client.models.generate_content(**kwargs)
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.text or "").strip(), flags=re.DOTALL).strip()
            parsed = json.loads(txt)
            if not isinstance(parsed, list):
                parsed = [parsed]

            result_by_idx = {}
            for entry in parsed:
                if isinstance(entry, dict) and "idx" in entry:
                    try:
                        result_by_idx[int(entry["idx"])] = entry
                    except (ValueError, TypeError):
                        pass

            log(f"Батч из {len(batch)} постов обработан через {m} (ответов: {len(result_by_idx)}, вызов #{STATE['ai_calls']}/{MAX_AI_CALLS_PER_DAY})")
            return result_by_idx
        except json.JSONDecodeError:
            log(f"Модель {m} вернула невалидный JSON — посты остаются на повтор.")
            return {}
        except Exception as e:
            s = str(e)
            if "404" in s:
                log(f"Модель {m} не существует (404) — убираю из списка.")
                models.remove(m)
                continue
            if any(code in s for code in ("429", "RESOURCE_EXHAUSTED", "503", "504", "DEADLINE_EXCEEDED")):
                log(f"Модель {m} недоступна/таймаут — кулдаун 15 мин.")
                MODEL_COOLDOWN[m] = time.time() + 15 * 60
                continue
            raise

    raise QuotaExhausted()


# ---------------------------------------------------------------- Telegram

def build_safe_caption(title: str, desc: str, search_query: str) -> str:
    raw_title = title.strip()
    raw_desc = desc.strip()

    if PARTNER_SEARCH_TEMPLATE:
        encoded_q = urllib.parse.quote(search_query.strip())
        link = PARTNER_SEARCH_TEMPLATE.replace("{query}", encoded_q)
        footer = f'👉 <a href="{html.escape(link, quote=True)}">Найти на маркетплейсе</a>'
    else:
        footer = f"🔎 <b>Поиск на маркетплейсах:</b> <code>{html.escape(search_query.strip())}</code>"

    safe_title = html.escape(raw_title)
    overhead = len(f"<b>{safe_title}</b>\n\n\n\n{footer}")
    max_desc_len = max(120, 900 - overhead)
    if len(raw_desc) > max_desc_len:
        raw_desc = raw_desc[: max_desc_len - 1].rstrip() + "…"

    return f"<b>{safe_title}</b>\n\n{html.escape(raw_desc)}\n\n{footer}"


def download_media(url: str, path: str, media_type: str):
    limit = MAX_VIDEO_BYTES if media_type == "video" else MAX_PHOTO_BYTES
    with requests.get(url, headers=HEADERS, timeout=40, stream=True) as r:
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
        if size > limit:
            raise RuntimeError(f"Файл слишком большой ({size // 1024 // 1024} МБ)")
        written = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                written += len(chunk)
                if written > limit:
                    raise RuntimeError("Файл превысил лимит при скачивании")
                f.write(chunk)


def send_media_to_telegram(media_type: str, media_path: str, caption: str):
    endpoint = "sendVideo" if media_type == "video" else "sendPhoto"
    field_name = "video" if media_type == "video" else "photo"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}"

    with open(media_path, "rb") as mf:
        data = {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}
        if media_type == "video":
            data["supports_streaming"] = True
        resp = requests.post(url, data=data, files={field_name: mf}, timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:300]}")
        res_json = resp.json()
        if not res_json.get("ok"):
            raise RuntimeError(f"Ошибка Telegram API: {res_json}")


# ---------------------------------------------------------------- подготовка

def mark_item_seen(history: list, item: dict):
    if item["id"] not in history:
        history.append(item["id"])
    if item.get("txt_hash") and item["txt_hash"] not in history:
        history.append(item["txt_hash"])


def prepare_next_post(client, models: list, history: list, channels_pool: list, start_time: float):
    unsaved = 0
    empty_loops = 0

    while True:
        if MAX_RUNTIME_SECONDS - (time.time() - start_time) <= 90:
            if unsaved:
                save_and_push_state(history, channels_pool)
            return None

        if len(channels_pool) < 8:
            discover_channels_from_mysku(channels_pool)
            for seed in INITIAL_SEED_CHANNELS:
                if seed not in channels_pool:
                    channels_pool.append(seed)

        selected = random.sample(channels_pool, min(6, len(channels_pool)))
        log(f"Ищу пост по каналам ({len(channels_pool)} в базе): {selected}")

        video_posts, photo_posts = [], []
        seen_hashes = set()

        for ch in selected:
            for p in reversed(parse_telegram_channel_and_discover(ch, channels_pool)):
                if p["id"] in history:
     
