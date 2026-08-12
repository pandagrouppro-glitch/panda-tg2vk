"""Репостер: посты из Telegram-группы -> стена сообщества ВК.

Переменные окружения:
  TG_BOT_TOKEN   токен бота (бот должен быть админом группы)
  VK_TOKEN       пользовательский токен админа сообщества (wall, photos, groups, offline)
  VK_GROUP_ID    числовой id сообщества (без минуса)
  TG_CHAT_ID     id группы-источника (например -1001234567890)
  TOPICS         (необязательно) id тем через запятую; пусто = все темы
"""

import email.utils
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.sax.saxutils

TG_TOKEN = os.environ["TG_BOT_TOKEN"]
VK_TOKEN = os.environ["VK_TOKEN"]
VK_GROUP_ID = int(os.environ["VK_GROUP_ID"])
SOURCE_CHAT = int(os.environ["TG_CHAT_ID"])
TOPICS = {int(t) for t in os.environ.get("TOPICS", "").replace(" ", "").split(",") if t}

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
VK_API = "https://api.vk.ru/method"
VK_VERSION = "5.131"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE_DIR, "state.json")
FEED_ITEMS = os.path.join(BASE_DIR, "feed_items.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
FEED_XML = os.path.join(DOCS_DIR, "feed.xml")
PAGES_DIR = os.path.join(DOCS_DIR, "p")
SITE = "https://pandagrouppro-glitch.github.io"
TG_LINK = "https://t.me/panda_bandapro"
FEED_LIMIT = 30


def http(url, data=None, raw=False):
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=60) as r:
        payload = r.read()
    return payload if raw else json.loads(payload)


def vk(method, **params):
    params.update(access_token=VK_TOKEN, v=VK_VERSION)
    result = http(f"{VK_API}/{method}", params)
    if "error" in result:
        raise RuntimeError(f"VK {method}: {result['error'].get('error_msg')}")
    return result["response"]


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"offset": 0, "posted": []}


def save_state(state):
    state["posted"] = state["posted"][-500:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def tg_file_url(file_id):
    info = http(f"{TG_API}/getFile", {"file_id": file_id})["result"]
    return f"https://api.telegram.org/file/bot{TG_TOKEN}/{info['file_path']}"


def biggest_photo(message):
    photos = message.get("photo") or []
    return max(photos, key=lambda p: p.get("file_size", 0))["file_id"] if photos else None


def upload_photo(file_id):
    """Загружает фото из Telegram на стену сообщества и возвращает attachment."""
    server = vk("photos.getWallUploadServer", group_id=VK_GROUP_ID)
    image = http(tg_file_url(file_id), raw=True)
    print(f"фото из Telegram: {len(image)} байт")
    boundary = "----panda" + str(int(time.time() * 1000))
    parts = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="photo"; filename="photo.jpg"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        image,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        server["upload_url"],
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        uploaded = json.loads(response.read())
    if not uploaded.get("photo") or uploaded["photo"] == "[]":
        raise RuntimeError(f"VK upload: пустой ответ {uploaded}")
    saved = vk(
        "photos.saveWallPhoto",
        group_id=VK_GROUP_ID,
        photo=uploaded["photo"],
        server=uploaded["server"],
        hash=uploaded["hash"],
    )[0]
    largest = max(saved["sizes"], key=lambda s: s.get("width", 0))
    return f"photo{saved['owner_id']}_{saved['id']}", largest["url"]


def feed_add(text, image_urls, post_id):
    """Добавляет пост в RSS-ленту для Дзена."""
    items = []
    if os.path.exists(FEED_ITEMS):
        with open(FEED_ITEMS, encoding="utf-8") as f:
            items = json.load(f)
    title = (text.strip().split("\n", 1)[0] or "Panda Group")[:120]
    items.insert(
        0,
        {
            "id": str(post_id),
            "title": title,
            "link": f"{SITE}/p/{post_id}.html",
            "source": f"{TG_LINK}/{post_id}",
            "text": text,
            "images": image_urls,
            "date": email.utils.formatdate(usegmt=True),
        },
    )
    items = items[:FEED_LIMIT]
    with open(FEED_ITEMS, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    feed_write(items)


def item_body(item):
    def esc(value):
        return xml.sax.saxutils.escape(value)

    body = "".join(f'<img src="{esc(url)}"/>' for url in item["images"])
    body += "".join(f"<p>{esc(line)}</p>" for line in item["text"].split("\n") if line.strip())
    return body


def page_write(item):
    """Пишет HTML-страницу поста: Дзен импортирует только ссылки с подтверждённого домена."""
    def esc(value):
        return xml.sax.saxutils.escape(value)

    html = "\n".join([
        "<!doctype html>",
        '<html lang="ru"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(item['title'])}</title>",
        f'<link rel="canonical" href="{esc(item["link"])}">',
        (
            "<style>body{font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px}"
            "img{max-width:100%;height:auto;border-radius:8px;margin:8px 0}</style>"
        ),
        "</head><body>",
        f"<h1>{esc(item['title'])}</h1>",
        item_body(item),
        f'<p><a href="{esc(item.get("source", TG_LINK))}">Источник: Telegram Panda Group</a></p>',
        "</body></html>",
    ])
    os.makedirs(PAGES_DIR, exist_ok=True)
    with open(os.path.join(PAGES_DIR, f"{item['id']}.html"), "w", encoding="utf-8") as f:
        f.write(html)


def feed_write(items):
    def esc(value):
        return xml.sax.saxutils.escape(value)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"'
            ' xmlns:atom="http://www.w3.org/2005/Atom">'
        ),
        "<channel>",
        "<title>Panda Group</title>",
        f"<link>{SITE}/</link>",
        f'<atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>',
        "<description>Бизнес с Китаем под ключ: логистика, авто, туры, обучение</description>",
        "<language>ru</language>",
        f"<lastBuildDate>{email.utils.formatdate(usegmt=True)}</lastBuildDate>",
    ]
    for item in items:
        page_write(item)
        parts += [
            "<item>",
            f"<title>{esc(item['title'])}</title>",
            f"<link>{esc(item['link'])}</link>",
            f'<guid isPermaLink="true">{esc(item["link"])}</guid>',
            f"<pubDate>{item['date']}</pubDate>",
            f"<description>{esc(item['text'][:400])}</description>",
            f"<content:encoded><![CDATA[{item_body(item)}]]></content:encoded>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(FEED_XML, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def collect(messages):
    """Группирует медиагруппы (альбомы) в один пост."""
    groups = {}
    order = []
    for message in messages:
        key = message.get("media_group_id") or f"single:{message['message_id']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(message)
    return [groups[key] for key in order]


def suitable(message):
    if message.get("chat", {}).get("id") != SOURCE_CHAT:
        return False
    if TOPICS and message.get("message_thread_id") not in TOPICS:
        return False
    if message.get("video") or message.get("video_note") or message.get("poll"):
        return False
    return bool(message.get("text") or message.get("caption") or message.get("photo"))


def publish(group, dry_run):
    text = ""
    attachments = []
    image_urls = []
    for message in group:
        text = text or message.get("text") or message.get("caption") or ""
        file_id = biggest_photo(message)
        if file_id and not dry_run:
            attachment, url = upload_photo(file_id)
            attachments.append(attachment)
            image_urls.append(url)
    if dry_run:
        print("DRY RUN:", text[:200].replace("\n", " | "), f"[фото: {len(group)}]")
        return
    vk(
        "wall.post",
        owner_id=-VK_GROUP_ID,
        from_group=1,
        message=text,
        attachments=",".join(attachments),
    )
    feed_add(text, image_urls, group[0]["message_id"])


def main():
    dry_run = "--dry-run" in sys.argv
    state = load_state()
    updates = http(
        f"{TG_API}/getUpdates",
        {"offset": state["offset"], "timeout": 0, "allowed_updates": json.dumps(["message"])},
    )["result"]
    if not updates:
        print("новых постов нет")
        return

    messages = []
    for update in updates:
        state["offset"] = update["update_id"] + 1
        message = update.get("message")
        if message and suitable(message):
            messages.append(message)

    failed = False
    for group in collect(messages):
        key = group[0].get("media_group_id") or str(group[0]["message_id"])
        if key in state["posted"]:
            continue
        try:
            publish(group, dry_run)
            state["posted"].append(key)
            print("опубликовано:", key)
            time.sleep(2)
        except Exception as error:  # noqa: BLE001 - не терять остальные посты из-за одного сбоя
            print("ошибка публикации", key, error, file=sys.stderr)
            failed = True

    if not dry_run and not failed:
        save_state(state)


if __name__ == "__main__":
    main()
