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

# Интервал между постами в минутах (180 = каждые 3 часа, 120 = каждые 2 часа)
POST_INTERVAL_MINUTES = int(os.environ.get("POST_INTERVAL_MINUTES", "180"))

# Максимальное время жизни одного запуска (5.5 часов в секундах), после чего GitHub перезапустит воркфлоу
MAX_RUNTIME_SECONDS = int(5.5 * 3600)

HISTORY_FILE = "history.json"
MSK_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

DISCOVERY_QUERIES = [
    'site:t.me/s "находки с маркетплейсов"',
    'site:t.me/s "полезные гаджеты"',
    'site:t.me/s "товары для дома" маркетплейс',
    'site:t.me/s "автотовары" алиэкспресс маркетплейс',
    'site:t.me/s "крутые находки" wildberries ozon',
    'site:t.me/s "инструменты" aliexpress находки',
    'site:t.me/s "распаковка" находки маркетплейс',
    'site:t.me/s "яндекс маркет" находки',
]

OUT_OF_STOCK_PHRASES = [
    "нет в наличии",
    "товар закончился",
    "раскупили",
    "временно отсутствует",
    "товар не найден",
    "страница не найдена",
    "больше не продается",
    "больше не продаётся",
    "out of stock",
    "sold out",
]


def log(msg: str):
    now_str = datetime.now(MSK_TZ).strftime("%H:%M:%S")
    print(f"[{now_str} МСК] {msg}", flush=True)


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_and_push_history(history: list):
    """Сохраняет history.json и сразу пушит в GitHub, чтобы при перезапуске не было дублей."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-800:], f, ensure_ascii=False, indent=2)

    try:
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "add", HISTORY_FILE], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update post history"], check=False)
            subprocess.run(["git", "push"], check=False)
            log("История успешно сохранена в репозиторий.")
    except Exception as e:
        log(f"Не удалось запушить history.json: {e}")


def get_working_gemini_client_and_model():
    """Динамически получает список моделей из API и проверяет каждую на работоспособность."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    raw_models = list(client.models.list())
    candidates = []

    for m in raw_models:
        name = getattr(m, "name", "")
        if not name:
            continue
        clean_name = name.replace("models/", "")
        lower_name = clean_name.lower()

        if "gemini" not in lower_name:
            continue
        if any(bad in lower_name for bad in ["embedding", "imagen", "veo", "tts", "audio", "aqa", "image", "computer-use"]):
            continue

        methods = getattr(m, "supported_generation_methods", None)
        if methods and "generateContent" not in methods:
            continue

        candidates.append(clean_name)

    if not candidates:
        raise RuntimeError("API не вернул ни одной текстовой модели Gemini для этого ключа.")

    candidates.sort(key=lambda x: (0 if "flash" in x.lower() else 1, x))
    log(f"Кандидаты моделей из API: {candidates}")

    for model_name in candidates:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents="Ответь одним словом: ОК"
            )
            if resp and resp.text:
                log(f"Выбрана рабочая модель Gemini: {model_name}")
                return client, model_name
        except Exception as e:
            log(f"Модель {model_name} недоступна, пробую следующую... ({str(e)[:120]})")
            continue

    raise RuntimeError("Ни одна из возвращённых API моделей не ответила на тестовый запрос.")


def discover_telegram_channels() -> list:
    query = random.choice(DISCOVERY_QUERIES)
    log(f"Ищу свежие каналы в Telegram по теме: {query}")
    own_channel = CHANNEL_ID.lstrip("@").lower()
    found_channels = []

    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        matches = re.findall(r"t\.me/s/([a-zA-Z0-9_]{5,32})", resp.text)
        for ch in matches:
            ch_lower = ch.lower()
            if ch_lower == own_channel or ch_lower in [c.lower() for c in found_channels]:
                continue
            found_channels.append(ch)
    except Exception as e:
        log(f"Ошибка автопоиска каналов: {e}")

    random.shuffle(found_channels)
    return found_channels[:6]


def is_product_link_alive(url: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code >= 400:
            return False

        final_path = urllib.parse.urlparse(resp.url).path.strip("/")
        if final_path == "":
            return False

        page_text = resp.text.lower()
        for phrase in OUT_OF_STOCK_PHRASES:
            if phrase in page_text:
                log(f"Товар распродан ('{phrase}'): {url}")
                return False

        return True
    except Exception:
        return False


def fetch_pepper_deals() -> list:
    url = "https://www.pepper.ru/"
    deals = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        articles = soup.select("article.thread")
        for art in articles[:12]:
            thread_id = art.get("id")
            if not thread_id:
                continue
            item_id = f"pepper:{thread_id}"

            full_classes = " ".join(art.get("class", [])).lower()
            if "expired" in full_classes or "истек" in art.get_text(" ").lower():
                continue

            title_el = art.select_one("a.thread-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            deal_page_url = title_el.get("href", "")

            price_el = art.select_one(".thread-price")
            price = price_el.get_text(strip=True) if price_el else ""

            img_el = art.select_one("img.thread-image")
            img_url = img_el.get("src") if img_el else ""

            desc_el = art.select_one(".userHtml")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""

            store_url = ""
            if deal_page_url:
                try:
                    d_resp = requests.get(deal_page_url, headers=HEADERS, timeout=15)
                    d_soup = BeautifulSoup(d_resp.text, "html.parser")
                    btn = d_soup.select_one("a[data-t='dealBtn'], a.btn--mode-primary[href*='visit']")
                    if btn and btn.get("href"):
                        visit_link = btn["href"]
                        if visit_link.startswith("/"):
                            visit_link = "https://www.pepper.ru" + visit_link
                        v_resp = requests.get(visit_link, headers=HEADERS, timeout=15, allow_redirects=True)
                        parsed = urllib.parse.urlparse(v_resp.url)
                        store_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                except Exception:
                    pass

            deals.append({
                "id": item_id,
                "type": "photo",
                "media_url": img_url,
                "store_url": store_url,
                "raw_text": f"Товар: {title}\nЦена по скидке: {price}\nОписание: {desc}"
            })
    except Exception as e:
        log(f"Ошибка парсинга Pepper.ru: {e}")

    return deals


def fetch_video_posts_from_channel(channel: str) -> list:
    url = f"https://t.me/s/{channel}"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")
    results = []

    for msg in messages:
        msg_div = msg.select_one(".tgme_widget_message")
        if not msg_div:
            continue
        post_id = msg_div.get("data-post")
        if not post_id:
            continue

        video_tag = msg.select_one("video.tgme_widget_message_video")
        if not video_tag or not video_tag.get("src"):
            continue

        text_div = msg.select_one(".tgme_widget_message_text")
        raw_text = text_div.get_text("\n").strip() if text_div else ""
        if len(raw_text) < 20:
            continue

        results.append({
            "id": f"tg:{post_id}",
            "type": "video",
            "media_url": video_tag["src"],
            "store_url": "",
            "raw_text": raw_text
        })

    return results


def clean_donor_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_post_content(client, model_name: str, raw_text: str) -> dict:
    cleaned = clean_donor_text(raw_text)
    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов».
На основе исходного текста составь описание товара для поста.

Исходный текст:
\"\"\"{cleaned}\"\"\"

Требования:
1. Если это НЕ физический товар (а подписка, тариф связи, игра, курс, услуга, новости, реклама другого канала/бота/казино), верни строго JSON: {{"is_product": false}}
2. Если это конкретный физический товар, верни строго JSON без markdown-обёртки:
{{
  "is_product": true,
  "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале",
  "description": "Живое описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками. Без воды, без чужих каналов, без ссылок и без erid.",
  "search_query": "Точное название товара для поиска на маркетплейсе (4-7 слов)"
}}
"""
    response = client.models.generate_content(model=model_name, contents=prompt)
    raw_resp = response.text.strip()
    raw_resp = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_resp, flags=re.DOTALL).strip()
    return json.loads(raw_resp)


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


def run_single_post_cycle(client, model_name: str, history: list) -> bool:
    """Пытается найти и опубликовать 1 свежий товар. Возвращает True, если пост опубликован."""
    video_candidates = []
    for channel in discover_telegram_channels():
        try:
            posts = fetch_video_posts_from_channel(channel)
            if posts:
                video_candidates.extend(list(reversed(posts))[:2])
        except Exception as e:
            log(f"Не удалось прочитать @{channel}: {e}")

    pepper_candidates = fetch_pepper_deals()

    candidates = []
    max_len = max(len(video_candidates), len(pepper_candidates))
    for i in range(max_len):
        if i < len(video_candidates):
            candidates.append(video_candidates[i])
        if i < len(pepper_candidates):
            candidates.append(pepper_candidates[i])

    for item in candidates:
        if item["id"] in history:
            continue

        log(f"Проверяю кандидата {item['id']}...")

        if item["store_url"]:
            if not is_product_link_alive(item["store_url"]):
                history.append(item["id"])
                save_and_push_history(history)
                continue

        if not item["media_url"]:
            history.append(item["id"])
            save_and_push_history(history)
            continue

        try:
            ai_data = generate_post_content(client, model_name, item["raw_text"])
        except Exception as e:
            log(f"Ошибка Gemini для {item['id']}: {e}")
            continue

        if not ai_data.get("is_product"):
            log(f"Кандидат {item['id']} не является физическим товаром, пропускаю.")
            history.append(item["id"])
            save_and_push_history(history)
            continue

        title = ai_data["title"]
        desc = ai_data["description"]
        search_query = ai_data["search_query"]

        if PARTNER_SEARCH_TEMPLATE:
            encoded_q = urllib.parse.quote(search_query)
            link = PARTNER_SEARCH_TEMPLATE.format(query=encoded_q)
            footer = f'👉 <a href="{link}">Найти на маркетплейсе</a>'
        elif item["store_url"]:
            footer = f'👉 <a href="{item["store_url"]}">Смотреть на маркетплейсе</a>'
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
            log(f"Успешно опубликовано: {item['id']}")

            history.append(item["id"])
            save_and_push_history(history)
            return True
        except Exception as e:
            log(f"Ошибка при публикации {item['id']}: {e}")
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    return False


def main():
    start_time = time.time()
    history = load_history()
    client, model_name = get_working_gemini_client_and_model()

    log(f"Запуск рабочего цикла на {MAX_RUNTIME_SECONDS / 3600:.1f} ч. Интервал постинга: {POST_INTERVAL_MINUTES} мин. (Ночной режим отключён)")

    while True:
        elapsed = time.time() - start_time
        if elapsed >= MAX_RUNTIME_SECONDS:
            log("Прошло 5.5 часов. Завершаю текущую сессию для передачи эстафеты следующему запуску.")
            break

        published = run_single_post_cycle(client, model_name, history)

        if published:
            wait_seconds = POST_INTERVAL_MINUTES * 60
        else:
            log("Подходящих товаров не найдено, повторная попытка через 10 минут.")
            wait_seconds = 10 * 60

        remaining = MAX_RUNTIME_SECONDS - (time.time() - start_time)
        if remaining <= 60:
            log("Время сессии подходит к концу, выхожу для перезапуска.")
            break

        sleep_duration = min(wait_seconds, remaining)
        log(f"Следующая публикация/проверка через {int(sleep_duration // 60)} мин.")
        time.sleep(sleep_duration)


if __name__ == "__main__":
    main()
