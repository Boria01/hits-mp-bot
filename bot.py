import os
import re
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

# --- НАСТРОЙКИ ИЗ GITHUB SECRETS ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@hits_mp")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

ENV_GEMINI_MODELS = os.environ.get("GEMINI_MODELS", "").strip()
PARTNER_SEARCH_TEMPLATE = os.environ.get("PARTNER_SEARCH_TEMPLATE", "").strip()
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "20"))
MAX_RUNTIME_SECONDS = int(5.5 * 3600)
MAX_AI_CALLS_PER_CYCLE = 4

HISTORY_FILE = "history.json"
CHANNELS_POOL_FILE = "channels_pool.json"
MSK_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Реальные проверенные живые каналы по AliExpress и гаджетам
INITIAL_SEED_CHANNELS = [
    "ali_muzhik",
    "aliexpress_rus",
    "nahodki_aliexpress",
    "kitaec_ru",
    "aligadgets",
    "ali_express_sale",
    "aliexpress_top_ru",
]

IGNORED_USERNAMES = {
    "s", "joinchat", "share", "proxy", "iv", "addstickers", "setlanguage",
    "botfather", "telegram", "durov", "username", "login", "contact", "vcnews", "addlist",
    "tgstat", "telemetr", "tlgrm",
}

CHANNEL_TOPIC_KEYWORDS = [
    "aliexpress", "алиэкспресс", "али", "яндекс маркет", "маркет",
    "гаджет", "техник", "электроник", "инструмент", "автотовар",
    "находк", "распаковк", "кита", "скидк", "обзор", "товар", "покупк",
]

POST_STOP_WORDS = [
    "wildberries", "вайлдберриз", "артикул wb", "арт. wb", "арт wb",
    "на вб", "с вб", "артикул озон", "арт. озон", "готовый образ", "лук на",
]

POST_PRODUCT_KEYWORDS = [
    "цена", "руб", "₽", "$", "скидк", "находк", "распаковк", "заказать",
    "купить", "aliexpress", "алиэкспресс", "яндекс", "маркет",
    "обзор", "гаджет", "устройств", "аккумулятор", "мощност", "usb", "заказ",
]

MODEL_COOLDOWN = {}


class QuotaExhausted(Exception):
    pass


def log(msg: str):
    now_str = datetime.now(MSK_TZ).strftime("%H:%M:%S")
    print(f"[{now_str} МСК] {msg}", flush=True)


def load_json_list(filepath: str, default: list) -> list:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return list(default)


def save_and_push_state(history: list, channels_pool: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-1500:], f, ensure_ascii=False, indent=2)

    with open(CHANNELS_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(channels_pool[-300:], f, ensure_ascii=False, indent=2)

    try:
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "add", HISTORY_FILE, CHANNELS_POOL_FILE], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update bot state"], check=False)
            subprocess.run(["git", "pull", "--rebase", "-X", "ours"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"Состояние сохранено (история: {len(history)}, каналов в базе: {len(channels_pool)}).")
    except Exception as e:
        log(f"Ошибка синхронизации с GitHub: {e}")


def init_gemini_client_and_models():
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=15000)
    )

    if ENV_GEMINI_MODELS:
        models = [m.strip() for m in ENV_GEMINI_MODELS.split(",") if m.strip()]
        log(f"Использую заданные модели: {models}")
        return client, models

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
        if any(bad in lower_name for bad in ["embedding", "imagen", "veo", "tts", "audio", "aqa", "image", "computer-use", "pro"]):
            continue

        methods = getattr(m, "supported_generation_methods", None)
        if methods and "generateContent" not in methods:
            continue

        candidates.append(clean_name)

    candidates.sort(key=lambda x: (
        0 if ("flash" in x.lower() and "lite" not in x.lower()) else (1 if "flash-lite" in x.lower() else 2),
        x
    ))
    log(f"Список моделей из API (0 токенов потрачено): {candidates}")
    return client, candidates


def discover_real_channels_from_catalogs(channels_pool: list):
    """Автопоиск реальных живых каналов по AliExpress и гаджетам из открытых каталогов."""
    own_channel = CHANNEL_ID.lstrip("@").lower()
    catalog_urls = [
        "https://tlgrm.ru/channels?q=aliexpress",
        "https://tlgrm.ru/channels?q=%D0%B0%D0%BB%D0%B8%D1%8D%D0%BA%D1%81%D0%BF%D1%80%D0%B5%D1%81%D1%81",
        "https://tlgrm.ru/channels?q=%D0%B3%D0%B0%D0%B4%D0%B6%D0%B5%D1%82%D1%8B",
        "https://tlgrm.ru/channels?q=%D1%8F%D0%BD%D0%B4%D0%B5%D0%BA%D1%81+%D0%BC%D0%B0%D1%80%D0%BA%D0%B5%D1%82",
    ]
    url = random.choice(catalog_urls)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            found = re.findall(r"(?:t\.me/|/channels/@)([a-zA-Z0-9_]{5,32})", resp.text)
            added = 0
            for ch in found:
                ch_l = ch.lower()
                if ch_l in IGNORED_USERNAMES or ch_l == own_channel or ch_l.endswith("bot"):
                    continue
                if "wb" in ch_l or "wildberries" in ch_l:
                    continue
                if ch_l not in [c.lower() for c in channels_pool]:
                    channels_pool.append(ch)
                    added += 1
            if added:
                log(f"Из каталога добавлено новых каналов: +{added} (всего в пуле: {len(channels_pool)})")
    except Exception as e:
        log(f"Ошибка обхода каталога каналов: {e}")


def is_target_channel(channel_name: str, soup: BeautifulSoup) -> bool:
    ch_lower = channel_name.lower()
    if "wb" in ch_lower or "wildberries" in ch_lower:
        return False

    title_el = soup.select_one(".tgme_channel_info_header_title")
    desc_el = soup.select_one(".tgme_channel_info_description")
    title_text = title_el.get_text(" ", strip=True).lower() if title_el else ""
    desc_text = desc_el.get_text(" ", strip=True).lower() if desc_el else ""

    # Если канал вообще не существует, title_text пустой
    if not title_text:
        return False

    if "wildberries" in title_text or "вайлдберриз" in title_text:
        return False

    full_header = f"{ch_lower} {title_text} {desc_text}"
    return any(kw in full_header for kw in CHANNEL_TOPIC_KEYWORDS)


def looks_like_target_post(text: str) -> bool:
    t_lower = text.lower()
    if len(t_lower) < 25 or len(t_lower) > 1800:
        return False

    if any(sw in t_lower for sw in POST_STOP_WORDS):
        return False

    has_keyword = any(kw in t_lower for kw in POST_PRODUCT_KEYWORDS)
    has_digits = bool(re.search(r"\d{2,}", t_lower))
    return has_keyword and has_digits


def parse_telegram_channel_and_discover(channel: str, channels_pool: list) -> list:
    url = f"https://t.me/s/{channel}"
    own_channel = CHANNEL_ID.lstrip("@").lower()
    results = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log(f"Канал @{channel} недоступен (HTTP {resp.status_code}), удаляю из пула.")
            if channel in channels_pool:
                channels_pool.remove(channel)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")

        if not is_target_channel(channel, soup):
            log(f"Канал @{channel} не существует или не по теме, удаляю из пула.")
            if channel in channels_pool:
                channels_pool.remove(channel)
            return []

        discovered = re.findall(r"t\.me/(?:s/)?([a-zA-Z0-9_]{5,32})", resp.text)
        for d_ch in discovered:
            d_l = d_ch.lower()
            if d_l in IGNORED_USERNAMES or d_l == own_channel or d_l.endswith("bot"):
                continue
            if "wb" in d_l or "wildberries" in d_l:
                continue
            if d_l not in [c.lower() for c in channels_pool]:
                channels_pool.append(d_ch)

        messages = soup.select(".tgme_widget_message_wrap")
        for msg in messages:
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

            video_tag = msg.select_one("video.tgme_widget_message_video")
            if video_tag and video_tag.get("src"):
                results.append({
                    "id": f"tg:{post_id}",
                    "type": "video",
                    "media_url": video_tag["src"],
                    "raw_text": raw_text
                })
                continue

            photo_wrap = msg.select_one("a.tgme_widget_message_photo_wrap")
            if photo_wrap and photo_wrap.get("style"):
                bg_match = re.search(r"url\(['\"]?(https?://[^'\")]+)['\"]?\)", photo_wrap["style"])
                if bg_match:
                    results.append({
                        "id": f"tg:{post_id}",
                        "type": "photo",
                        "media_url": bg_match.group(1),
                        "raw_text": raw_text
                    })

        log(f"@{channel}: целевых постов на странице: {len(results)}")
    except Exception as e:
        log(f"Ошибка чтения @{channel}: {e}")

    return results


def clean_donor_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"erid:\s*\S+", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_post_content(client, models: list, raw_text: str) -> dict:
    cleaned = clean_donor_text(raw_text)
    current_date = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов» (упор на AliExpress и Яндекс Маркет).
Сегодняшняя дата: {current_date} (учитывай текущий сезон, не пиши про весну/лето, если сейчас осень/зима).

Исходный текст:
\"\"\"{cleaned}\"\"\"

Требования:
1. Верни строго JSON: {{"is_product": false}}, если это:
   - НЕ физический товар (реклама канала, бота, казино, ставок, курсов, кредитов, подписки);
   - подборка/коллаж из нескольких разных вещей одежды (женский образ/лук);
   - товар, привязанный строго к Wildberries.
2. Если это ОДИН конкретный крутой товар (гаджет, электроника, инструмент, автотовар, полезная вещь для дома/кухни), верни строго JSON без markdown-обёртки:
{{
  "is_product": true,
  "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале",
  "description": "Живое описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками. Без воды, без упоминаний WB/Ozon, без чужих каналов, ссылок и erid.",
  "search_query": "Точное название товара для поиска на AliExpress и Яндекс Маркете (4-7 слов)"
}}
"""
    now = time.time()
    for m in list(models):
        if MODEL_COOLDOWN.get(m, 0) > now:
            continue
        try:
            resp = client.models.generate_content(model=m, contents=prompt)
            txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip(), flags=re.DOTALL).strip()
            log(f"Успешный ответ от {m}")
            return json.loads(txt)
        except json.JSONDecodeError:
            log(f"Модель {m} вернула невалидный JSON — отбрасываю пост.")
            return {"is_product": False}
        except Exception as e:
            s = str(e)
            if "404" in s:
                log(f"Модель {m} не существует (404) — убираю из списка.")
                models.remove(m)
                continue
            if "429" in s or "RESOURCE_EXHAUSTED" in s or "503" in s:
                log(f"Модель {m} вернула 429/503 — ставлю кулдаун 15 мин.")
                MODEL_COOLDOWN[m] = time.time() + 15 * 60
                continue
            raise

    raise QuotaExhausted()


def send_media_to_telegram(media_type: str, media_path: str, caption: str):
    endpoint = "sendVideo" if media_type == "video" else "sendPhoto"
    field_name = "video" if media_type == "video" else "photo"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}"

    with open(media_path, "rb") as mf:
        files = {field_name: mf}
        data = {
            "chat_id": CHANNEL_ID,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        }
        if media_type == "video":
            data["supports_streaming"] = True

        resp = requests.post(url, data=data, files=files, timeout=60)
        resp.raise_for_status()
        res_json = resp.json()
        if not res_json.get("ok"):
            raise RuntimeError(f"Ошибка Telegram API: {res_json}")


def run_single_post_cycle(client, models: list, history: list, channels_pool: list):
    if len(channels_pool) < 10:
        discover_real_channels_from_catalogs(channels_pool)

    if not channels_pool:
        channels_pool.extend(INITIAL_SEED_CHANNELS)

    sample_size = min(6, len(channels_pool))
    selected_channels = random.sample(channels_pool, sample_size)
    log(f"Проверяю каналы ({len(channels_pool)} в базе): {selected_channels}")

    video_posts = []
    photo_posts = []

    for ch in selected_channels:
        posts = parse_telegram_channel_and_discover(ch, channels_pool)
        for p in reversed(posts):
            if p["id"] in history:
                continue
            if p["type"] == "video":
                video_posts.append(p)
            else:
                photo_posts.append(p)

    candidates = video_posts + photo_posts
    log(f"Свежих кандидатов (не из истории): {len(candidates)} (видео: {len(video_posts)}, фото: {len(photo_posts)})")

    ai_calls = 0
    for item in candidates:
        if item["id"] in history:
            continue
        if ai_calls >= MAX_AI_CALLS_PER_CYCLE:
            log(f"Достигнут лимит {MAX_AI_CALLS_PER_CYCLE} AI-запросов за цикл — останавливаю перебор.")
            break

        ai_calls += 1
        try:
            ai_data = generate_post_content(client, models, item["raw_text"])
        except QuotaExhausted:
            log("Квота AI временно исчерпана по всем моделям — ухожу в паузу на 30 мин.")
            save_and_push_state(history, channels_pool)
            return None
        except Exception as e:
            log(f"Ошибка AI для {item['id']}: {e}")
            history.append(item["id"])
            continue

        if not ai_data.get("is_product"):
            log(f"Пост {item['id']} отсеян AI (не одиночный товар / WB / реклама).")
            history.append(item["id"])
            continue

        title = ai_data["title"]
        desc = ai_data["description"]
        search_query = ai_data["search_query"]

        if PARTNER_SEARCH_TEMPLATE:
            encoded_q = urllib.parse.quote(search_query)
            link = PARTNER_SEARCH_TEMPLATE.format(query=encoded_q)
            footer = f'👉 <a href="{link}">Найти на маркетплейсе</a>'
        else:
            footer = f"🔎 <b>Поиск на маркетплейсах:</b> <code>{search_query}</code>"

        caption = f"<b>{title}</b>\n\n{desc}\n\n{footer}"

        ext = ".mp4" if item["type"] == "video" else ".jpg"
        temp_file = f"temp_media{ext}"

        try:
            m_resp = requests.get(item["media_url"], headers=HEADERS, timeout=40)
            m_resp.raise_for_status()
            with open(temp_file, "wb") as f:
                f.write(m_resp.content)

            send_media_to_telegram(item["type"], temp_file, caption)
            log(f"ОПУБЛИКОВАНО В КАНАЛ: {item['id']} -> {title}")

            history.append(item["id"])
            save_and_push_state(history, channels_pool)
            return True
        except Exception as e:
            log(f"Ошибка отправки {item['id']} в Telegram: {e}")
            history.append(item["id"])
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    save_and_push_state(history, channels_pool)
    return False


def main():
    start_time = time.time()
    history = load_json_list(HISTORY_FILE, [])
    channels_pool = load_json_list(CHANNELS_POOL_FILE, INITIAL_SEED_CHANNELS)

    client, models = init_gemini_client_and_models()
    log(f"Старт цикла 5.5 ч. Интервал после публикации: {POST_INTERVAL_MINUTES} мин.")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUNTIME_SECONDS:
            log("Прошло 5.5 часов. Завершаю сессию для перезапуска.")
            break

        published = run_single_post_cycle(client, models, history, channels_pool)

        if published is None:
            wait_seconds = 30 * 60
        elif not published:
            log("Подходящих свежих постов пока нет — следующая попытка через 5 минут.")
            wait_seconds = 5 * 60
        else:
            wait_seconds = POST_INTERVAL_MINUTES * 60

        remaining = MAX_RUNTIME_SECONDS - (time.time() - start_time)
        if remaining <= 60:
            log("Время сессии истекает, выхожу для передачи эстафеты.")
            break

        sleep_duration = min(wait_seconds, remaining)
        log(f"Сплю {int(sleep_duration // 60)} мин...")
        time.sleep(sleep_duration)


if __name__ == "__main__":
    main()
        
