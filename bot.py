import os
import re
import html
import json
import time
import random
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# --- НАСТРОЙКИ ИЗ GITHUB SECRETS / VARIABLES ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@hits_mp")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()

ENV_GEMINI_MODELS = os.environ.get("GEMINI_MODELS", "").strip()
PARTNER_SEARCH_TEMPLATE = os.environ.get("PARTNER_SEARCH_TEMPLATE", "").strip()
MAX_AI_CALLS_PER_DAY = int(os.environ.get("MAX_AI_CALLS_PER_DAY", "180"))

BATCH_SIZE = 6
MAX_AI_BATCH_CALLS_PER_RUN = 3
MAX_PHOTO_BYTES = 9 * 1024 * 1024

HISTORY_FILE = "history.json"
STATE_FILE = "state.json"
MSK_TZ = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# --- РАСШИРЕННЫЙ СПИСОК ИСТОЧНИКОВ (RSS + HTML) ---
RSS_SOURCES = [
    {"name": "mysku", "url": "https://mysku.club/rss/", "strict_kw": False},
    {"name": "pepper", "url": "https://www.pepper.ru/rss/all", "strict_kw": False},
    {"name": "ixbt_live", "url": "https://www.ixbt.com/live/rss/", "strict_kw": True},
    {"name": "lifehacker_shop", "url": "https://lifehacker.ru/topics/shopping/feed/", "strict_kw": False},
    {"name": "4pda", "url": "https://4pda.to/feed/", "strict_kw": True},
    {"name": "rozetked", "url": "https://rozetked.me/rss", "strict_kw": True},
    {"name": "trashbox", "url": "https://trashbox.ru/feed/", "strict_kw": True},
    {"name": "ferra", "url": "https://www.ferra.ru/exports/rss.xml", "strict_kw": True},
    {"name": "hitech_mail", "url": "https://hi-tech.mail.ru/rss/all/", "strict_kw": True},
    {"name": "overclockers", "url": "https://overclockers.ru/rss/all.rss", "strict_kw": True},
]

# Ключевые слова для новостных сайтов (чтобы отсеять обычные новости от подборок/скидок)
NEWS_SITE_PRODUCT_KEYWORDS = (
    "aliexpress", "алиэкспресс", "яндекс маркет", "яндекс.маркет",
    "скидк", "дешевле", "цена", "руб", "₽", "подборк", "находк",
    "обзор", "гаджет", "аккумулятор", "зарядк", "наушник", "инструмент",
    "автотовар", "пылесос", "смарт-час", "фонар", "мультитул", "купон", "промокод",
)

POST_STOP_WORDS = (
    "wildberries", "вайлдберриз", "артикул wb", "арт. wb", "арт wb",
    "на вб", "с вб", "артикул озон", "арт. озон", "готовый образ", "лук на",
    "казино", "ставки на спорт", "букмекер", "криптовалют", "заработок в интернете",
    "итоги конкурса", "розыгрыш среди подписчиков",
)

MODEL_COOLDOWN = {}
STATE = {"last_post_ts": 0.0, "ai_day": "", "ai_calls": 0}


class QuotaExhausted(Exception):
    pass


def log(msg: str):
    now_str = datetime.now(MSK_TZ).strftime("%H:%M:%S")
    print(f"[{now_str} МСК] {msg}", flush=True)


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


def save_and_push_state(history: list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-3000:], f, ensure_ascii=False, indent=2)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)

    try:
        files = [HISTORY_FILE, STATE_FILE]
        subprocess.run(["git", "config", "--local", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "github-actions[bot]"], check=False)
        subprocess.run(["git", "add", *files], check=False)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], check=False)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update shop bot state"], check=False)
            subprocess.run(["git", "pull", "--rebase", "-X", "ours"], check=False)
            subprocess.run(["git", "push"], check=False)
            log(f"Состояние сохранено в GitHub (в истории: {len(history)} записей).")
    except Exception as e:
        log(f"Ошибка синхронизации с GitHub: {e}")


def init_gemini_client_and_models():
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=45000)
    )

    if ENV_GEMINI_MODELS:
        models = [m.strip() for m in ENV_GEMINI_MODELS.split(",") if m.strip()]
        log(f"Использую заданные модели из ENV: {models}")
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
    log(f"Доступные модели Gemini: {candidates}")
    return client, candidates


def clean_donor_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"erid:\s*\S+", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def make_text_hash(raw_text: str) -> str:
    cleaned = clean_donor_text(raw_text).lower()
    norm = re.sub(r"\W+", "", cleaned)[:80]
    return f"txt:{norm}" if len(norm) >= 20 else ""


def extract_first_image(soup: BeautifulSoup, base_url: str = "") -> str:
    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        if not src or src.startswith("data:"):
            continue
        if any(skip in src.lower() for skip in ("avatar", "icon", "logo", "emoji", "smile", "pixel", "1x1", "counter")):
            continue
        if base_url and src.startswith("/"):
            src = urllib.parse.urljoin(base_url, src)
        if src.startswith("http"):
            return src
    return ""


def fetch_og_image(page_url: str) -> str:
    """Добирает главную картинку страницы через og:image, если её не было в RSS."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        og = soup.select_one('meta[property="og:image"], meta[name="twitter:image"]')
        if og and og.get("content", "").startswith("http"):
            return og["content"].strip()
    except Exception:
        pass
    return ""
def parse_rss_source(source: dict) -> list:
    name = source["name"]
    feed_url = source["url"]
    strict_kw = source.get("strict_kw", True)
    results = []

    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log(f"[{name}] HTTP {resp.status_code} — пропускаю.")
            return []

        root = ET.fromstring(resp.content)
        items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

        for item in items[:15]:
            title_el = item.find("title")
            if title_el is None:
                title_el = item.find("{http://www.w3.org/2005/Atom}title")
            title = (title_el.text or "").strip() if title_el is not None else ""

            link = ""
            link_el = item.find("link")
            if link_el is not None:
                link = (link_el.text or link_el.get("href") or "").strip()
            if not link:
                atom_link = item.find("{http://www.w3.org/2005/Atom}link")
                if atom_link is not None:
                    link = (atom_link.get("href") or "").strip()

            desc_html = ""
            for tag in ("description", "content:encoded", "{http://purl.org/rss/1.0/modules/content/}encoded",
                        "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"):
                el = item.find(tag)
                if el is not None and el.text:
                    desc_html = el.text
                    break

            soup = BeautifulSoup(desc_html, "html.parser")
            desc_text = soup.get_text(" ", strip=True)
            full_text = f"{title}\n{desc_text}".strip()
            lower_full = full_text.lower()

            if len(lower_full) < 30:
                continue
            if any(sw in lower_full for sw in POST_STOP_WORDS):
                continue
            if strict_kw and not any(kw in lower_full for kw in NEWS_SITE_PRODUCT_KEYWORDS):
                continue

            # Ищем картинку в enclosure / media:content / внутри HTML описания
            img_url = ""
            enclosure = item.find("enclosure")
            if enclosure is not None and "image" in (enclosure.get("type") or "image"):
                img_url = (enclosure.get("url") or "").strip()

            if not img_url:
                for m_tag in ("{http://search.yahoo.com/mrss/}content", "{http://search.yahoo.com/mrss/}thumbnail"):
                    m_el = item.find(m_tag)
                    if m_el is not None and m_el.get("url"):
                        img_url = m_el.get("url").strip()
                        break

            if not img_url:
                img_url = extract_first_image(soup, link)

            item_uid = f"{name}:{abs(hash(link or title))}"
            results.append({
                "id": item_uid,
                "source": name,
                "link": link,
                "txt_hash": make_text_hash(full_text),
                "raw_text": full_text[:900],
                "media_url": img_url,
            })

        log(f"[{name}] отобрано кандидатов: {len(results)}")
    except Exception as e:
        log(f"[{name}] ошибка парсинга RSS: {e}")

    return results


def parse_mysku_html_fallback() -> list:
    """Дополнительный парсер главной страницы mysku.club на случай задержек в RSS."""
    results = []
    try:
        resp = requests.get("https://mysku.club/", headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for topic in soup.select("article.topic, div.topic")[:12]:
            title_el = topic.select_one("h2.topic-title a, .topic-title a")
            body_el = topic.select_one(".topic-content")
            img_el = topic.select_one(".topic-content img")
            if not (title_el and body_el):
                continue
            href = title_el.get("href", "")
            img_url = (img_el.get("src") or img_el.get("data-src") or "") if img_el else ""
            raw_text = f"{title_el.get_text(' ', strip=True)}\n{body_el.get_text(' ', strip=True)[:600]}"
            if any(sw in raw_text.lower() for sw in POST_STOP_WORDS):
                continue
            m_id = re.search(r"/(\d+)\.html", href)
            item_id = f"mysku_html:{m_id.group(1)}" if m_id else f"mysku_html:{abs(hash(href))}"
            results.append({
                "id": item_id,
                "source": "mysku_html",
                "link": href,
                "txt_hash": make_text_hash(raw_text),
                "raw_text": raw_text,
                "media_url": img_url if img_url.startswith("http") else "",
            })
    except Exception as e:
        log(f"[mysku_html] ошибка: {e}")
    return results


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
    if ai_daily_limit_reached():
        log(f"Суточный лимит AI-запросов ({MAX_AI_CALLS_PER_DAY}) исчерпан.")
        raise QuotaExhausted()

    current_date = datetime.now(MSK_TZ).strftime("%d.%m.%Y")
    items_block = []
    for idx, item in enumerate(batch, start=1):
        cleaned = clean_donor_text(item["raw_text"])[:750]
        items_block.append(f'IDX: {idx}\nТЕКСТ:\n"""{cleaned}"""')
    joined_items = "\n\n---\n\n".join(items_block)

    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов» (упор на AliExpress и Яндекс Маркет).
Сегодняшняя дата: {current_date} (учитывай текущий сезон).

Проанализируй каждый материал (по его числовому IDX) и верни строго JSON-массив объектов (без markdown-обёртки).
Для каждого IDX:
- Если это НЕ физический товар (просто новость индустрии, анонс фильма/игры, слухи, реклама казино/ставок/курсов/подписок) или товар строго с Wildberries/Ozon — верни:
  {{"idx": 1, "is_product": false}}
- Если это ОДИН конкретный реальный товар (гаджет, электроника, инструмент, автотовар, вещь для дома/кухни/туризма) — верни:
  {{
    "idx": 1,
    "is_product": true,
    "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале (без HTML-тегов)",
    "description": "Живое описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками и ценой/скидкой, если указана. Без HTML-тегов, без воды, без упоминаний WB/Ozon, без чужих сайтов и ссылок.",
    "search_query": "Точное название товара для поиска на AliExpress и Яндекс Маркете (4-7 слов)"
  }}

Материалы для проверки:
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

            log(f"Батч из {len(batch)} шт. обработан через {m} (ответов: {len(result_by_idx)}, вызов #{STATE['ai_calls']}/{MAX_AI_CALLS_PER_DAY})")
            return result_by_idx
        except json.JSONDecodeError:
            log(f"Модель {m} вернула невалидный JSON.")
            return {}
        except Exception as e:
            s = str(e)
            if "404" in s:
                models.remove(m)
                continue
            if any(code in s for code in ("429", "RESOURCE_EXHAUSTED", "503", "504", "DEADLINE_EXCEEDED")):
                log(f"Модель {m} временно недоступна — кулдаун 15 мин.")
                MODEL_COOLDOWN[m] = time.time() + 15 * 60
                continue
            raise

    raise QuotaExhausted()


def build_safe_caption(title: str, desc: str, search_query: str) -> str:
    raw_title = title.strip()
    raw_desc = desc.strip()
    encoded_q = urllib.parse.quote(search_query.strip())

    if PARTNER_SEARCH_TEMPLATE:
        link = PARTNER_SEARCH_TEMPLATE.replace("{query}", encoded_q)
        footer = f'👉 <a href="{html.escape(link, quote=True)}">Найти на маркетплейсе</a>'
    else:
        ali_link = f"https://aliexpress.ru/wholesale?SearchText={encoded_q}"
        ym_link = f"https://market.yandex.ru/search?text={encoded_q}"
        footer = (
            f'👉 <a href="{html.escape(ali_link, quote=True)}">Найти на AliExpress</a>  |  '
            f'<a href="{html.escape(ym_link, quote=True)}">Яндекс Маркет</a>'
        )

    safe_title = html.escape(raw_title)
    overhead = len(f"<b>{safe_title}</b>\n\n\n\n{footer}")
    max_desc_len = max(120, 900 - overhead)
    if len(raw_desc) > max_desc_len:
        raw_desc = raw_desc[: max_desc_len - 1].rstrip() + "…"

    return f"<b>{safe_title}</b>\n\n{html.escape(raw_desc)}\n\n{footer}"


def send_telegram_photo_post(caption: str, media_url: str) -> bool:
    api_base = f"https://api.telegram.org/bot{BOT_TOKEN}"
    try:
        if media_url:
            img_resp = requests.get(media_url, headers=HEADERS, timeout=15)
            if img_resp.status_code == 200 and 2000 < len(img_resp.content) <= MAX_PHOTO_BYTES:
                ctype = img_resp.headers.get("Content-Type", "")
                if "image" in ctype or media_url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    r = requests.post(
                        f"{api_base}/sendPhoto",
                        data={"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"},
                        files={"photo": ("product.jpg", img_resp.content, "image/jpeg")},
                        timeout=25,
                    )
                    if r.status_code == 200 and r.json().get("ok"):
                        return True
                    log(f"Ошибка sendPhoto: {r.text[:200]}")

        # Резервная отправка текстом, если картинка не скачалась
        r = requests.post(
            f"{api_base}/sendMessage",
            data={
                "chat_id": CHANNEL_ID,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True
        log(f"Ошибка sendMessage: {r.text[:200]}")
    except Exception as e:
        log(f"Ошибка отправки в Telegram: {e}")
    return False


def main():
    global STATE
    history = load_json(HISTORY_FILE, [])
    history_set = set(history)
    STATE = load_json(STATE_FILE, {"last_post_ts": 0.0, "ai_day": "", "ai_calls": 0})

    log("Запуск разового цикла парсинга по cron...")
    sources = list(RSS_SOURCES)
    random.shuffle(sources)

    candidates = []
    for src in sources:
        items = parse_rss_source(src)
        for it in items:
            if it["id"] in history_set or (it["txt_hash"] and it["txt_hash"] in history_set):
                continue
            candidates.append(it)
        if len(candidates) >= 24:
            break

    if len(candidates) < 6:
        for it in parse_mysku_html_fallback():
            if it["id"] not in history_set and (not it["txt_hash"] or it["txt_hash"] not in history_set):
                candidates.append(it)

    if not candidates:
        log("Новых непроверенных материалов на сайтах пока нет. Завершаю работу.")
        save_and_push_state(history)
        return

    random.shuffle(candidates)
    client, models = init_gemini_client_and_models()

    published = False
    ai_calls_this_run = 0

    for i in range(0, len(candidates), BATCH_SIZE):
        if published or ai_calls_this_run >= MAX_AI_BATCH_CALLS_PER_RUN:
            break

        batch = candidates[i:i + BATCH_SIZE]
        ai_calls_this_run += 1

        try:
            ai_results = generate_batch_content(client, models, batch)
        except QuotaExhausted:
            log("Квота Gemini исчерпана — выхожу до следующего запуска.")
            break

        for idx, item in enumerate(batch, start=1):
            res = ai_results.get(idx)
            if not res:
                continue

            # Запоминаем просмотренный материал, чтобы не гонять его повторно
            history.append(item["id"])
            history_set.add(item["id"])
            if item["txt_hash"]:
                history.append(item["txt_hash"])
                history_set.add(item["txt_hash"])

            if not res.get("is_product"):
                continue

            title = (res.get("title") or "").strip()
            desc = (res.get("description") or "").strip()
            search_query = (res.get("search_query") or "").strip()
            if not (title and desc and search_query):
                continue

            media_url = item.get("media_url") or ""
            if not media_url and item.get("link"):
                media_url = fetch_og_image(item["link"])

            caption = build_safe_caption(title, desc, search_query)
            
            now = datetime.now(MSK_TZ)
            if now.second >= 25:
              time.sleep(60 - now.second)
    
            if send_telegram_photo_post(caption, media_url):
                STATE["last_post_ts"] = time.time()
                log(f"Успешно опубликован товар из [{item['source']}]: {title}")
                published = True
                break

    if not published:
        log("В этом запуске подходящих новых товаров не нашлось — засыпаю до следующего крона.")

    save_and_push_state(history)


if __name__ == "__main__":
    main()
    
