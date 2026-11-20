import os
import re
import json
import time
import urllib.parse
import requests
from bs4 import BeautifulSoup
from google import genai

# --- НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ (GITHUB SECRETS) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@hits_mp")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Список публичных каналов-доноров (через запятую в Secrets или по умолчанию)
DONORS_RAW = os.environ.get("DONOR_CHANNELS", "ali_muzhik,nahodki_wb_ozon")
DONOR_CHANNELS = [c.strip().lstrip("@") for c in DONORS_RAW.split(",") if c.strip()]

# Шаблон твоей партнёрской ссылки (когда одобрят ePN или Яндекс Дистрибуцию).
# Например: "https://alitems.co/g/твой_код/?ulp=https%3A%2F%2Faliexpress.ru%2Fwholesale%3FSearchText%3D{query}"
# Пока пусто — бот ставит текстовый запрос для поиска без чужих ссылок.
PARTNER_SEARCH_TEMPLATE = os.environ.get("PARTNER_SEARCH_TEMPLATE", "").strip()

HISTORY_FILE = "history.json"
MAX_POSTS_PER_RUN = 1  # Сколько постов публиковать за один запуск


def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_history(history: list):
    # Храним последние 500 записей, чтобы файл не разрастался
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history[-500:], f, ensure_ascii=False, indent=2)


def get_working_gemini_client_and_model():
    """Автоматически находит доступную и рабочую модель Gemini для текущего ключа."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    preferred_keywords = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "flash"]

    available_models = []
    try:
        for m in client.models.list():
            name = getattr(m, "name", "")
            methods = getattr(m, "supported_generation_methods", []) or []
            if "gemini" in name.lower() and (not methods or "generateContent" in methods):
                clean_name = name.replace("models/", "")
                available_models.append(clean_name)
    except Exception as e:
        print(f"Ошибка получения списка моделей: {e}")
        available_models = ["gemini-2.5-flash", "gemini-2.0-flash"]

    # Сортируем по приоритету Flash-моделей
    ordered = []
    for kw in preferred_keywords:
        for m in available_models:
            if kw in m and m not in ordered and "lite" not in m and "audio" not in m and "image" not in m:
                ordered.append(m)
    for m in available_models:
        if m not in ordered and "embed" not in m and "imagen" not in m and "veo" not in m:
            ordered.append(m)

    for model_name in ordered:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents="Ответь одним словом: ОК"
            )
            if resp and resp.text:
                print(f"Выбрана рабочая модель Gemini: {model_name}")
                return client, model_name
        except Exception as e:
            print(f"Модель {model_name} недоступна ({e}), пробуем следующую...")
            continue

    raise RuntimeError("Не найдено ни одной доступной модели Gemini для данного API-ключа.")


def fetch_video_posts_from_donor(channel: str) -> list:
    """Парсит публичную веб-страницу канала t.me/s/<channel> и находит посты с видео."""
    url = f"https://t.me/s/{channel}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    messages = soup.select(".tgme_widget_message_wrap")
    results = []

    for msg in messages:
        msg_div = msg.select_one(".tgme_widget_message")
        if not msg_div:
            continue
        post_id = msg_div.get("data-post")  # например, "ali_muzhik/1234"
        if not post_id:
            continue

        # Ищем прямую ссылку на mp4-видео внутри поста
        video_tag = msg.select_one("video.tgme_widget_message_video")
        if not video_tag or not video_tag.get("src"):
            continue

        video_url = video_tag["src"]

        # Извлекаем текст поста
        text_div = msg.select_one(".tgme_widget_message_text")
        raw_text = text_div.get_text("\n").strip() if text_div else ""

        # Отбрасываем рекламные посты других каналов / казино / крипты без товара
        if len(raw_text) < 15:
            continue

        results.append({
            "id": post_id,
            "video_url": video_url,
            "raw_text": raw_text
        })

    return results


def clean_donor_text(text: str) -> str:
    """Удаляет чужие ссылки, токены erid и упоминания чужих каналов."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"Реклама\..*?(erid:\s*\S+)?", "", text, flags=re.IGNORECASE)
    return text.strip()


def generate_post_content(client, model_name: str, raw_text: str) -> dict:
    """Генерирует уникальное описание товара и поисковый запрос через Gemini."""
    cleaned = clean_donor_text(raw_text)
    prompt = f"""
Ты — автор популярного Telegram-канала «Лучшие находки с маркетплейсов».
На основе исходного текста объявления составь описание товара для поста под видео-обзор.

Исходный текст:
\"\"\"{cleaned}\"\"\"

Требования:
1. Если это НЕ физический товар с маркетплейса (а реклама другого канала, ставки, курсы, новости), верни строго JSON: {{"is_product": false}}
2. Если это товар, верни строго JSON без markdown-обёртки:
{{
  "is_product": true,
  "title": "Короткий цепляющий заголовок с 1 подходящим эмодзи в начале",
  "description": "Живое, конкретное описание пользы товара (2-3 предложения) + 3-4 коротких буллита (• ...) с главными фишками. Без воды, без упоминания других каналов, без ссылок и без слова erid.",
  "search_query": "Точный поисковый запрос для поиска этого товара на маркетплейсе (4-7 слов, без лишних знаков)"
}}
"""
    response = client.models.generate_content(
        model=model_name,
        contents=prompt
    )
    raw_resp = response.text.strip()
    # Убираем возможные ```json ... ```
    raw_resp = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_resp, flags=re.DOTALL).strip()
    return json.loads(raw_resp)


def send_video_to_telegram(video_path: str, caption: str):
    """Отправляет видео с подписью в канал через Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
    with open(video_path, "rb") as vf:
        files = {"video": vf}
        data = {
            "chat_id": CHANNEL_ID,
            "caption": caption[:1024],  # Лимит подписи к видео в Telegram — 1024 символа
            "parse_mode": "HTML",
            "supports_streaming": True
        }
        resp = requests.post(url, data=data, files=files, timeout=60)
        resp.raise_for_status()
        res_json = resp.json()
        if not res_json.get("ok"):
            raise RuntimeError(f"Ошибка Telegram API: {res_json}")


def main():
    history = load_history()
    client, model_name = get_working_gemini_client_and_model()

    posted_count = 0

    for channel in DONOR_CHANNELS:
        if posted_count >= MAX_POSTS_PER_RUN:
            break
        print(f"Проверяю донора: @{channel}")
        try:
            posts = fetch_video_posts_from_donor(channel)
        except Exception as e:
            print(f"Не удалось спарсить @{channel}: {e}")
            continue

        # Идём от свежих к более старым
        for post in reversed(posts):
            if posted_count >= MAX_POSTS_PER_RUN:
                break
            if post["id"] in history:
                continue

            print(f"Обрабатываю пост {post['id']}...")
            try:
                ai_data = generate_post_content(client, model_name, post["raw_text"])
            except Exception as e:
                print(f"Ошибка генерации Gemini для {post['id']}: {e}")
                continue

            if not ai_data.get("is_product"):
                print(f"Пост {post['id']} не является товаром, пропускаю.")
                history.append(post["id"])
                save_history(history)
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

            # Скачиваем видео во временный файл
            temp_video = "temp_video.mp4"
            try:
                v_resp = requests.get(post["video_url"], timeout=40)
                v_resp.raise_for_status()
                with open(temp_video, "wb") as f:
                    f.write(v_resp.content)

                send_video_to_telegram(temp_video, caption)
                print(f"Успешно опубликовано: {post['id']}")

                history.append(post["id"])
                save_history(history)
                posted_count += 1
                time.sleep(3)
            except Exception as e:
                print(f"Ошибка при скачивании/отправке видео {post['id']}: {e}")
            finally:
                if os.path.exists(temp_video):
                    os.remove(temp_video)

    if posted_count == 0:
        print("Новых подходящих видео-постов пока не найдено.")


if __name__ == "__main__":
    main()
  
