
"""Gabu 2.5 — TalesRunner HK official announcements.

Fix: Resolve event image URLs relative to each event page.
Images are downloaded temporarily and uploaded to Discord.
Existing v4 announcement state remains compatible.
"""

import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE = "https://www.talesrunner.com.hk"

SOURCES = {
    "event": BASE + "/notice/notice.php?type=event",
    "patch": BASE + "/notice/notice.php?type=patch",
    "news": BASE + "/notice/notice.php?type=system",
}

LABELS = {
    "event": "🎉 活動公告",
    "patch": "🎬 更新預告",
    "news": "📢 系統公告",
}

COLORS = {
    "event": 0xA855F7,
    "patch": 0x4786ED,
    "news": 0xE8A23C,
}

STATE_FILE = Path("official_news_state.json")
STATE_VERSION = 4

DRY_RUN = os.getenv(
    "DRY_RUN", "true"
).lower() == "true"

TEST = os.getenv(
    "TEST_NOTIFICATION", "false"
).lower() == "true"

WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK_URL", ""
).strip()

DATE_RE = re.compile(
    r"20\d{2}\s*[/.-]\s*\d{1,2}"
    r"\s*[/.-]\s*\d{1,2}"
)

NUMBER_RE = re.compile(
    r"^\d+\s*[)）.、]\s*"
)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 3

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/120.0 Safari/537.36"
    )
})


# =====================================
# 通用工具
# =====================================

def clean(value):
    return re.sub(
        r"\s+", " ", value or ""
    ).strip()


def shorten(value, limit):
    if len(value) <= limit:
        return value

    return value[:limit - 1] + "…"


def normalize_date(value):
    return (
        re.sub(r"\s+", "", value)
        .replace("-", "/")
        .replace(".", "/")
    )


def absolute_url(value, base_url=BASE):
    """
    Resolve relative URLs against the correct page.

    Example:
    images/index_01.png
    +
    https://www.talesrunner.com.hk/event/ABC/
    =
    https://www.talesrunner.com.hk/event/ABC/images/index_01.png
    """

    if not value:
        return ""

    if value.startswith((
        "data:",
        "blob:",
        "javascript:",
    )):
        return ""

    url = urljoin(base_url, value)

    parsed = urlparse(url)

    if parsed.scheme not in (
        "http",
        "https",
    ):
        return ""

    if parsed.hostname not in (
        "www.talesrunner.com.hk",
        "talesrunner.com.hk",
    ):
        return ""

    return url


def fetch(url):
    response = SESSION.get(
        url,
        timeout=35,
    )

    response.raise_for_status()

    if response.apparent_encoding:
        response.encoding = (
            response.apparent_encoding
        )

    return BeautifulSoup(
        response.text,
        "html.parser",
    )


def item_key(category, date, title):
    raw = f"{category}|{date}|{title}"

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def item_digest(item):
    """
    Keep the existing digest format.

    Image changes do not trigger duplicate
    notifications for historical events.
    """

    raw = json.dumps(
        {
            "title": item["title"],
            "body": item["body"],
            "url": item["url"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


def make_item(
    category,
    date,
    title,
    body="",
    url="",
):
    return {
        "category": category,
        "date": date,
        "title": clean(title),
        "body": body,
        "url": url or SOURCES[category],
        "key": item_key(
            category,
            date,
            title,
        ),
    }


# =====================================
# 活動公告
# =====================================

def parse_events(soup):
    items = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        url = absolute_url(
            anchor.get("href")
        )

        if not url:
            continue

        if "/event/" not in urlparse(url).path:
            continue

        title = clean(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        if not 4 <= len(title) <= 180:
            continue

        date = ""

        for parent in [
            anchor.parent,
            *list(anchor.parents)[:7],
        ]:
            if not hasattr(
                parent,
                "get_text",
            ):
                continue

            text = clean(
                parent.get_text(
                    " ",
                    strip=True,
                )
            )

            dates = DATE_RE.findall(text)

            if len(dates) == 1:
                date = normalize_date(
                    dates[0]
                )
                break

        if not date:
            continue

        item = make_item(
            "event",
            date,
            title,
            url=url,
        )

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


# =====================================
# 更新預告及系統公告
# =====================================

def extract_lines(soup):
    copy = BeautifulSoup(
        str(soup),
        "html.parser",
    )

    for tag in copy([
        "script",
        "style",
        "nav",
        "footer",
    ]):
        tag.decompose()

    return [
        clean(line)
        for line in copy.get_text(
            "\n",
            strip=True,
        ).splitlines()
        if clean(line)
    ]


def split_announcements(
    soup,
    category,
):
    blocks = []
    current = None

    for line in extract_lines(soup):
        if line == "◆":
            if current is not None:
                blocks.append(current)

            current = []

        elif current is not None:
            current.append(line)

    if current is not None:
        blocks.append(current)

    items = []
    seen = set()

    for block in blocks:
        date_index = next(
            (
                index
                for index, line
                in enumerate(block[:3])
                if DATE_RE.fullmatch(line)
            ),
            -1,
        )

        if date_index < 0:
            continue

        date = normalize_date(
            block[date_index]
        )

        remaining = (
            block[date_index + 1:]
        )

        while (
            remaining
            and remaining[0] in (
                "|",
                "｜",
                "-",
            )
        ):
            remaining.pop(0)

        if not remaining:
            continue

        title = clean(
            remaining[0].lstrip(
                "|｜"
            )
        )

        if not 3 <= len(title) <= 180:
            continue

        body_lines = [
            line
            for line in remaining[1:]
            if line not in (
                "上一頁",
                "下一頁",
                "頁數：",
                "◆",
                "|",
            )
        ]

        item = make_item(
            category,
            date,
            title,
            body="\n".join(
                body_lines
            ),
        )

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


# =====================================
# 更新預告整理
# =====================================

def combine_numbered_lines(lines):
    result = []
    current = ""

    for raw in lines:
        line = clean(raw)

        if not line or line == "-":
            continue

        if NUMBER_RE.match(line):
            if current:
                result.append(current)

            current = re.sub(
                r"^(\d+)\s*[)）.、]\s*",
                r"\1) ",
                line,
            )

        elif current:
            if (
                line.startswith((
                    "「",
                    "」",
                    "『",
                    "』",
                ))
                or current.endswith((
                    "「",
                    "」",
                    "『",
                    "』",
                ))
            ):
                current += line

            else:
                current += " " + line

        else:
            current = line

    if current:
        result.append(current)

    return result


def section_lines(
    body,
    start,
    end=None,
):
    lines = [
        clean(line)
        for line in body.splitlines()
        if clean(line)
    ]

    result = []
    active = False

    for line in lines:
        if re.search(
            start,
            line,
            re.I,
        ):
            active = True
            continue

        if (
            active
            and end
            and re.search(
                end,
                line,
                re.I,
            )
        ):
            break

        if active:
            result.append(line)

    return result


def patch_fields(body):
    fields = []

    maintenance = re.search(
        r"維護日期及時間\s*[:：]\s*([^\n]+)",
        body,
    )

    if maintenance:
        fields.append({
            "name": "🕒 維護時間",
            "value": shorten(
                clean(
                    maintenance.group(1)
                ),
                180,
            ),
            "inline": False,
        })

    main_items = (
        combine_numbered_lines(
            section_lines(
                body,
                r"本次更新主要內容",
                r"注意事項",
            )
        )
    )

    note_items = (
        combine_numbered_lines(
            section_lines(
                body,
                r"注意事項",
            )
        )
    )

    if main_items:
        fields.append({
            "name": "✨ 主要更新",
            "value": shorten(
                "\n".join(main_items),
                1000,
            ),
            "inline": False,
        })

    if note_items:
        fields.append({
            "name": (
                "⚠️ 注意事項／下架提醒"
            ),
            "value": shorten(
                "\n".join(note_items),
                1000,
            ),
            "inline": False,
        })

    if not fields:
        fields.append({
            "name": "更新內容",
            "value": shorten(
                body or "請查看官方公告",
                1000,
            ),
            "inline": False,
        })

    return fields


# =====================================
# Discord Embed
# =====================================

def make_embed(
    item,
    changed=False,
):
    category = item["category"]

    prefix = (
        "🔄 公告內容更新｜"
        if changed
        else ""
    )

    embed = {
        "title": shorten(
            prefix
            + LABELS[category]
            + "｜"
            + item["title"],
            250,
        ),
        "url": item["url"],
        "description": (
            "公告日期："
            + item["date"]
        ),
        "color": COLORS[category],
        "footer": {
            "text": (
                "加布｜跑 Online 官方情報"
            )
        },
    }

    if category == "event":
        embed["fields"] = [{
            "name": "📋 活動詳情",
            "value": (
                "點擊上方標題查看"
                "官方完整活動。"
            ),
            "inline": False,
        }]

    elif category == "patch":
        embed["fields"] = (
            patch_fields(
                item["body"]
            )
        )

    else:
        lines = [
            clean(line)
            for line in item[
                "body"
            ].splitlines()
            if clean(line)
        ]

        embed["fields"] = [{
            "name": (
                "📢 公告內容（節錄）"
            ),
            "value": shorten(
                "\n".join(
                    lines[:12]
                )
                or "請查看官方公告",
                1000,
            ),
            "inline": False,
        }]

    return embed


# =====================================
# 活動圖片：2.5 修正版
# =====================================

def discover_event_images(item):
    """
    Resolve images against the event page,
    not the TalesRunner homepage.
    """

    page_url = item["url"]
    soup = fetch(page_url)

    content = None

    for selector in (
        ".event_content",
        ".view_content",
        ".view_cont",
        ".board_view",
        "article",
        "main",
    ):
        content = soup.select_one(
            selector
        )

        if content:
            break

    if content is None:
        content = (
            soup.body or soup
        )

    candidates = []
    seen = set()

    for img in content.find_all(
        "img"
    ):
        try:
            width = int(
                img.get("width") or 0
            )
            height = int(
                img.get("height") or 0
            )

        except (
            ValueError,
            TypeError,
        ):
            width = 0
            height = 0

        if (
            (width and width < 150)
            or (
                height
                and height < 90
            )
        ):
            continue

        sources = [
            img.get("data-original"),
            img.get("data-src"),
            img.get("data-lazy-src"),
            img.get("src"),
        ]

        srcset = (
            img.get("srcset")
            or img.get("data-srcset")
        )

        if srcset:
            sources.append(
                srcset.split(",")[-1]
                .strip()
                .split(" ")[0]
            )

        for source in sources:
            # Important fix:
            # use page_url as the base.
            url = absolute_url(
                source,
                base_url=page_url,
            )

            if not url:
                continue

            if url in seen:
                continue

            if any(
                word in url.lower()
                for word in (
                    "logo",
                    "icon",
                    "button",
                    "btn_",
                    "bullet",
                    "footer",
                    "header",
                    "menu",
                    "arrow",
                )
            ):
                continue

            seen.add(url)
            candidates.append(url)

    if not candidates:
        og = soup.find(
            "meta",
            attrs={
                "property": "og:image"
            },
        )

        if og:
            url = absolute_url(
                og.get("content"),
                base_url=page_url,
            )

            if url:
                candidates.append(
                    url
                )

    return candidates[:18]


def image_extension(data):
    if data.startswith(
        b"\xff\xd8\xff"
    ):
        return (
            "jpg",
            "image/jpeg",
        )

    if data.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        return (
            "png",
            "image/png",
        )

    if data.startswith((
        b"GIF87a",
        b"GIF89a",
    )):
        return (
            "gif",
            "image/gif",
        )

    if (
        data.startswith(b"RIFF")
        and data[8:12]
        == b"WEBP"
    ):
        return (
            "webp",
            "image/webp",
        )

    return None


def download_event_images(item):
    """
    Download up to 3 images into memory.
    Each image must be under 5 MB.
    """

    files = []

    try:
        candidates = (
            discover_event_images(
                item
            )
        )

    except requests.RequestException as exc:
        print(
            "WARNING: Event page "
            "unavailable:",
            type(exc).__name__,
        )

        return files

    print(
        "Event image candidates:",
        len(candidates),
    )

    for url in candidates:
        if len(files) >= MAX_IMAGES:
            break

        try:
            with SESSION.get(
                url,
                timeout=25,
                stream=True,
                headers={
                    "Referer": (
                        item["url"]
                    )
                },
            ) as response:
                response.raise_for_status()

                length = (
                    response.headers.get(
                        "Content-Length"
                    )
                )

                if (
                    length
                    and int(length)
                    > MAX_IMAGE_BYTES
                ):
                    print(
                        "Skipping oversized image"
                    )
                    continue

                data = bytearray()

                for chunk in (
                    response.iter_content(
                        chunk_size=65536
                    )
                ):
                    data.extend(chunk)

                    if (
                        len(data)
                        > MAX_IMAGE_BYTES
                    ):
                        break

                if (
                    len(data)
                    > MAX_IMAGE_BYTES
                ):
                    print(
                        "Skipping oversized image"
                    )
                    continue

            kind = image_extension(
                data
            )

            if (
                not kind
                or len(data) < 1024
            ):
                print(
                    "Skipping unsupported "
                    "or tiny image"
                )
                continue

            ext, mime = kind

            filename = (
                f"event_"
                f"{len(files) + 1}"
                f".{ext}"
            )

            files.append((
                filename,
                bytes(data),
                mime,
            ))

            print(
                "Image ready:",
                filename,
                len(data),
                "bytes",
            )

        except (
            requests.RequestException,
            ValueError,
        ) as exc:
            print(
                "Skipping unavailable image:",
                type(exc).__name__,
            )

    return files


# =====================================
# 發送 Discord
# =====================================

def send_discord(
    payload,
    images=None,
):
    if DRY_RUN:
        print(
            "DRY RUN:",
            json.dumps(
                payload,
                ensure_ascii=False,
            )[:2500],
        )
        return

    if not WEBHOOK:
        raise RuntimeError(
            "Missing DISCORD_WEBHOOK_URL"
        )

    if images:
        multipart = [
            (
                f"files[{index}]",
                (
                    name,
                    io.BytesIO(data),
                    mime,
                ),
            )
            for index, (
                name,
                data,
                mime,
            ) in enumerate(images)
        ]

        response = SESSION.post(
            WEBHOOK,
            data={
                "payload_json": (
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                    )
                )
            },
            files=multipart,
            timeout=60,
        )

    else:
        response = SESSION.post(
            WEBHOOK,
            json=payload,
            timeout=30,
        )

    if not response.ok:
        raise RuntimeError(
            "Discord HTTP "
            + str(
                response.status_code
            )
        )

    print(
        "Discord sent:",
        response.status_code,
    )


def notify(
    item,
    changed=False,
):
    embeds = [
        make_embed(
            item,
            changed,
        )
    ]

    images = []

    if item["category"] == "event":
        images = (
            download_event_images(
                item
            )
        )

        if images:
            embeds[0]["image"] = {
                "url": (
                    "attachment://"
                    + images[0][0]
                )
            }

            for (
                filename,
                _,
                _,
            ) in images[1:]:
                embeds.append({
                    "color": (
                        COLORS["event"]
                    ),
                    "image": {
                        "url": (
                            "attachment://"
                            + filename
                        )
                    },
                })

        else:
            print(
                "No suitable event images; "
                "sending text and official "
                "link only"
            )

    payload = {
        "username": "加布",
        "allowed_mentions": {
            "parse": []
        },
        "embeds": embeds,
    }

    send_discord(
        payload,
        images,
    )


# =====================================
# 公告紀錄
# =====================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except (
        ValueError,
        OSError,
    ) as exc:
        raise RuntimeError(
            "Cannot read "
            "official_news_state.json"
        ) from exc

    if not isinstance(
        data,
        dict,
    ):
        raise RuntimeError(
            "Invalid announcement state"
        )

    return data


def save_state(state):
    if DRY_RUN:
        return

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


# =====================================
# 主程式
# =====================================

def main():
    print(
        "Gabu 2.5 starting"
    )

    print(
        "DRY_RUN:",
        DRY_RUN,
    )

    if TEST:
        send_discord({
            "username": "加布",
            "content": (
                "🧪 加布 2.5 "
                "測試成功！"
            ),
            "allowed_mentions": {
                "parse": []
            },
        })

        return

    all_items = {}
    errors = []

    for category, url in (
        SOURCES.items()
    ):
        print()
        print(
            "==========",
            category.upper(),
            "==========",
        )

        try:
            soup = fetch(url)

            if category == "event":
                items = (
                    parse_events(
                        soup
                    )
                )

            else:
                items = (
                    split_announcements(
                        soup,
                        category,
                    )
                )

            print(
                f"{category}: "
                f"parsed "
                f"{len(items)} "
                f"entries"
            )

            if not items:
                raise RuntimeError(
                    "No announcements parsed"
                )

            for item in items[:5]:
                print(
                    "PREVIEW:",
                    item["date"],
                    "|",
                    item["title"],
                )

                print(
                    "URL:",
                    item["url"],
                )

                if category == "patch":
                    for field in (
                        patch_fields(
                            item["body"]
                        )
                    ):
                        print(
                            "FIELD:",
                            field["name"],
                            field["value"][:500],
                        )

                elif category == "news":
                    print(
                        "BODY:",
                        item["body"][:300],
                    )

            all_items[
                category
            ] = items

        except Exception as exc:
            print(
                "ERROR:",
                category,
                type(exc).__name__,
                str(exc),
            )

            errors.append(
                category
            )

    if errors:
        print(
            "Failed categories:",
            ", ".join(errors),
        )

        sys.exit(1)

    if DRY_RUN:
        print(
            "DRY RUN completed: "
            "no Discord messages "
            "or state changes"
        )

        return

    old = load_state()

    # Retain v4 state compatibility.
    if (
        old.get(
            "_schema_version"
        )
        != STATE_VERSION
    ):
        state = {
            "_schema_version": (
                STATE_VERSION
            )
        }

        for category, items in (
            all_items.items()
        ):
            for item in items:
                state[
                    category
                    + ":"
                    + item["key"]
                ] = item_digest(
                    item
                )

            print(
                f"{category}: "
                f"initialized "
                f"{len(items)} "
                "entries; no "
                "historical notifications"
            )

        save_state(
            state
        )

        print(
            "Initial baseline "
            "saved successfully"
        )

        return

    state = dict(
        old
    )

    for category, items in (
        all_items.items()
    ):
        prefix = (
            category + ":"
        )

        existing = any(
            key.startswith(
                prefix
            )
            for key in state
        )

        if not existing:
            for item in items:
                state[
                    prefix
                    + item["key"]
                ] = item_digest(
                    item
                )

            save_state(
                state
            )

            print(
                f"{category}: "
                "baseline restored; "
                "no historical "
                "notifications"
            )

            continue

        pending = []

        for item in reversed(
            items
        ):
            key = (
                prefix
                + item["key"]
            )

            digest = (
                item_digest(
                    item
                )
            )

            if key not in state:
                pending.append((
                    item,
                    False,
                ))

            elif (
                state[key]
                != digest
            ):
                pending.append((
                    item,
                    True,
                ))

        if not pending:
            print(
                f"{category}: "
                "no changes"
            )

            continue

        for (
            item,
            changed,
        ) in pending[:8]:
            notify(
                item,
                changed,
            )

            state[
                prefix
                + item["key"]
            ] = item_digest(
                item
            )

            save_state(
                state
            )

            time.sleep(1)

        print(
            f"{category}: "
            f"sent "
            f"{min(8, len(pending))} "
            "notifications"
        )

        if len(pending) > 8:
            print(
                f"{category}: "
                f"{len(pending) - 8} "
                "changes remaining"
            )

    print()
    print(
        "Gabu 2.5 "
        "completed successfully"
    )


if __name__ == "__main__":
    main()
