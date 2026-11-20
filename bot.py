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

# --- НАСТРОЙКИ ИЗ GITHUB SECRETS ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@hits_mp")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Шаблон твоей партнёрской ссылки (добавишь после модерации ePN / Яндекс Дистрибуции)
PARTNER_SEARCH_TEMPLATE = os.environ.get("PARTNER_SEARCH_TEMPLATE", "").strip()

# Интервал между постами в минутах (120 = каждые 2 часа, 180 = каждые 3 часа)
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "120"))

# Максимальное время жизни одного запуска (5.5 часов в секундах)
MAX_RUNTIME_SECONDS = int(5.5 * 3600)

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

INITIAL_SEED_CHANNELS = [
    "ali_muzhik",
    "nahodki_wb_ozon",
    "top_nahodki_mp",
    "aliexpress_rus",
    "wb_ozon_nahodki",
]

IGNORED_USERNAMES = {
    "s", "joinchat", "share", "proxy", "iv", "addstickers", "setlanguage",
    "botfather", "telegram", "durov", "username", "login", "contact",
}


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
    """Сохраняет history.json и channels_pool.json в репозиторий GitHub."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-1000:], f, ensure_ascii=False, indent=2)

    with open(CHANNELS_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump(channels_pool[-300:], f, ensure_ascii=False, indent=2)

    try:
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "add", HISTORY_FILE, CHANNELS_POOL_FILE], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update bot state and discovered channels"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"Состояние сохранено в GitHub (история: {len(history)}, база каналов: {len(channels_pool)}).")
    except Exception as e:
        log(f"Ошибка git push: {e}")


def get_working_gemini_client_and_models():
    """Возвращает клиент и отсортированный список всех живых текстовых моделей (Gemini + Gemma)."""
    client = genai.Client(api_key=GEMINI_API_KEY)
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
        if any(bad in lower_name for bad in ["embedding", "imagen", "veo", "tts", "audio", "aqa", "image", "computer-use"]):
            continue

        methods = getattr(m, "supported_generation_methods", None)
        if methods and "generateContent" not in methods:
            continue

        candidates.append(clean_name)

    if not candidates:
        raise RuntimeError("API не вернул ни одной текстовой модели для этого ключа.")

    candidates.sort(key=lambda x: (0 if "flash" in x.lower() else (1 if "gemini" in x.lower() else 2), x))
    log(f"Кандидаты моделей из API: {candidates}")

    working = []
    for model_name in candidates:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents="Ответь одним словом: ОК"
            )
            if resp and resp.text:
                log(f"Проверена рабочая модель: {model_name}")
                working.append(model_name)
        except Exception as e:
            log(f"Модель {model_name} недоступна при проверке ({str(e)[:90]})")
            continue

    if not working:
        raise RuntimeError("Ни одна из возвращённых API моделей сейчас не доступна (возможно, исчерпаны квоты по всем моделям ключа).")

    return client, working


def discover_from_open_web(channels_pool: list):
    own_channel = CHANNEL_ID.lstrip("@").lower()
    open_pages = [
        "https://vc.ru/tag/aliexpress",
        "https://vc.ru/tag/wildberries",
        "https://vc.ru/tag/ozon",
        "https://pikabu.ru/tag/AliExpress/hot",
    ]
    url = random.choice(open_pages)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            matches = re.findall(r"t\.me/([a-zA-Z0-9_]{5,32})", resp.text)
            added = 0
            for ch in matches:
                ch_l = ch.lower()
                if ch_l in IGNORED_USERNAMES or ch_l == own_channel or ch_l.endswith("bot"):
                    continue
                if ch_l not in [c.lower() for c in channels_pool]:
                    channels_pool.append(ch)
                    added += 1
            if added:
                log(f"Из открытого веба ({url}) добавлено новых каналов: {added}")
    except Exception as e:
        log(f"Ошибка веб-поиска каналов ({url}): {e}")


def parse_telegram_channel_and_discover(channel: str, channels_pool: list) -> list:
    url = f"https://t.me/s/{channel}"
    own_channel = CHANNEL_ID.lstrip("@").lower()
    results = []

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        log(f"Проверка @{channel} -> HTTP {resp.status_code}")
        if resp.status_code != 200:
            return []

        discovered = re.findall(r"t\.me/(?:s/)?([a-zA-Z0-9_]{5,32})", resp.text)
        new_found = 0
        for d_ch in discovered:
            d_l = d_ch.lower()
            if d_l in IGNORED_USERNAMES or d_l == own_channel or d_l.endswith("bot"):
                continue
            if d_l not in [c.lower() for c in channels_pool]:
                channels_pool.append(d_ch)
                new_found += 1
        if new_found > 0:
            log(f"Из ленты @{channel} обнаружено новых каналов в пул: +{new_found} (всего в пуле: {len(channels_pool)})")

        soup = BeautifulSoup(resp.text, "html.parser")
        messages = soup.select(".tgme_widget_message_wrap")
        log(f"Найдено сообщений на странице @{channel}: {len(messages)}")

        for msg in messages:
            msg_div = msg.select_one(".tgme_widget_message")
            if not msg_div:
                continue
            post_id = msg_div.get("data-post")
            if not post_id:
                continue

            text_div = msg.select_one(".tgme_widget_message_text")
            raw_text = text_div.get_text("\n").strip() if text_div else ""
            if len(raw_text) < 25:
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

    except Exception as e:
        log(f"Ошибка при чтении @{channel}: {e}")

    return results


def clean_donor_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"erid:\s*\S+", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_post_content(client, working_models: list, raw_text: str) -> dict:
    """Пробует сгенерировать пост через список живых моделей; если у текущей кончилась квота 429 — переключается на следующую."""
    cleaned = clean_donor_text(raw_text)
    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов».
На основе исходного текста составь описание товара для поста.

Исходный текст:
\"\"\"{cleaned}\"\"\"

Требования:
1. Если это НЕ конкретный физический товар (а реклама другого канала, бота, казино, ставок, курсов, кредитов, новостей или просто мем), верни строго JSON: {{"is_product": false}}
2. Если это конкретный физический товар (гаджет, вещь для дома, автотовар, одежда, инструмент, электроника), верни строго JSON без markdown-обёртки:
{{
  "is_product": true,
  "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале",
  "description": "Живое описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками. Без воды, без чужих каналов, без ссылок и без erid.",
  "search_query": "Точное название товара для поиска на маркетплейсе (4-7 слов)"
}}
"""
    last_err = None
    for model_name in list(working_models):
        try:
            response = client.models.generate_content(model=model_name, contents=prompt)
            raw_resp = response.text.strip()
            raw_resp = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_resp, flags=re.DOTALL).strip()
            return json.loads(raw_resp)
        except Exception as e:
            err_str = str(e)
            last_err = e
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                log(f"У модели {model_name} исчерпан лимит 429, переключаюсь на следующую модель...")
                working_models.remove(model_name)
                continue
            raise e

    raise RuntimeError(f"Все доступные модели исчерпали лимит или вернули ошибку: {last_err}")


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


def run_single_post_cycle(client, working_models: list, history: list, channels_pool: list) -> bool:
    if not working_models:
        client, refreshed = get_working_gemini_client_and_models()
        working_models.extend(refreshed)

    discover_from_open_web(channels_pool)

    sample_size = min(6, len(channels_pool))
    selected_channels = random.sample(channels_pool, sample_size)
    log(f"Выбраны каналы для обхода в этом цикле: {selected_channels}")

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
    log(f"Всего свежих кандидатов для проверки: {len(candidates)} (видео: {len(video_posts)}, фото: {len(photo_posts)})")

    for item in candidates:
        if item["id"] in history:
            continue

        log(f"Отправляю в AI пост {item['id']} ({item['type']}) через {working_models[0]}...")
        try:
            ai_data = generate_post_content(client, working_models, item["raw_text"])
        except Exception as e:
            log(f"Ошибка AI для {item['id']}: {e}")
            if not working_models:
                break
            continue

        if not ai_data.get("is_product"):
            log(f"Пост {item['id']} отсеян (не товар / реклама).")
            history.append(item["id"])
            save_and_push_state(history, channels_pool)
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
            log(f"УСПЕШНО ОПУБЛИКОВАНО В КАНАЛ: {item['id']} -> {title}")

            history.append(item["id"])
            save_and_push_state(history, channels_pool)
            return True
        except Exception as e:
            log(f"Ошибка отправки {item['id']} в Telegram: {e}")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    save_and_push_state(history, channels_pool)
    return False


def main():
    start_time = time.time()
    history = load_json_list(HISTORY_FILE, [])
    channels_pool = load_json_list(CHANNELS_POOL_FILE, INITIAL_SEED_CHANNELS)

    client, working_models = get_working_gemini_client_and_models()
    log(f"Старт цикла 5.5 ч. Доступные модели: {working_models}. Интервал: {POST_INTERVAL_MINUTES} мин.")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUNTIME_SECONDS:
            log("Прошло 5.5 часов. Завершаю сессию для автоматического перезапуска.")
            break

        published = run_single_post_cycle(client, working_models, history, channels_pool)

        if published:
            wait_seconds = POST_INTERVAL_MINUTES * 60
        else:
            log("Подходящих новых постов не нашлось или лимит моделей исчерпан, следующая попытка через 10 минут.")
            wait_seconds = 10 * 60

        remaining = MAX_RUNTIME_SECONDS - (time.time() - start_time)
        if remaining <= 60:
            log("Время сессии истекает, выхожу для передачи эстафеты.")
            break

        sleep_duration = min(wait_seconds, remaining)
        log(f"Сплю {int(sleep_duration // 60)} мин. до следующей проверки...")
        time.sleep(sleep_duration)


if __name__ == "__main__":
    main()
    
