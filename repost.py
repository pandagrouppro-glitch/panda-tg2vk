"""Репостер: посты из Telegram-группы -> стена сообщества ВК.

Переменные окружения:
  TG_BOT_TOKEN   токен бота (бот должен быть админом группы)
  VK_TOKEN       пользовательский токен админа сообщества (wall, photos, groups, offline)
  VK_GROUP_ID    числовой id сообщества (без минуса)
  TG_CHAT_ID     id группы-источника (например -1001234567890)
  TOPICS         (необязательно) id тем через запятую; пусто = все темы
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

TG_TOKEN = os.environ["TG_BOT_TOKEN"]
VK_TOKEN = os.environ["VK_TOKEN"]
VK_GROUP_ID = int(os.environ["VK_GROUP_ID"])
SOURCE_CHAT = int(os.environ["TG_CHAT_ID"])
TOPICS = {int(t) for t in os.environ.get("TOPICS", "").replace(" ", "").split(",") if t}

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
VK_API = "https://api.vk.ru/method"
VK_VERSION = "5.131"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


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
    return f"photo{saved['owner_id']}_{saved['id']}"


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
    for message in group:
        text = text or message.get("text") or message.get("caption") or ""
        file_id = biggest_photo(message)
        if file_id and not dry_run:
            attachments.append(upload_photo(file_id))
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
