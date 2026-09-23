
"""
加布 2.1：跑 Online 官方公告監測
Python 3.11+
免費使用，無需 AI API。
"""

import hashlib
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
    "news": BASE + "/notice/notice.php",
}

STATE_FILE = Path("official_news_state.json")
STATE_VERSION = 2

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
TEST = os.getenv(
    "TEST_NOTIFICATION", "false"
).lower() == "true"

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

DATE_RE = re.compile(
    r"20\d{2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{1,2}"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0"
})


# -------------------------------------
# 通用工具
# -------------------------------------

def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def short(value, limit=900):
    if len(value) <= limit:
        return value
    return value[:limit - 1] + "…"


def normal_date(value):
    return re.sub(
        r"\s+", "", value
    ).replace("-", "/").replace(".", "/")


def absolute_url(value):
    if not value:
        return ""

    url = urljoin(BASE, value)
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    if parsed.hostname not in (
        "www.talesrunner.com.hk",
        "talesrunner.com.hk",
    ):
        return ""

    return url


def fetch(url):
    response = SESSION.get(url, timeout=35)
    response.raise_for_status()

    if response.apparent_encoding:
        response.encoding = response.apparent_encoding

    return BeautifulSoup(
        response.text, "html.parser"
    )


def make_key(category, date, title):
    raw = f"{category}|{date}|{title}"
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def make_digest(item):
    data = {
        "title": item["title"],
        "body": item["body"],
        "images": item["images"],
    }

    return hashlib.sha256(
        json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True
        ).encode("utf-8")
    ).hexdigest()[:16]


def new_item(
    category, date, title,
    url="", body="", images=None
):
    return {
        "key": make_key(category, date, title),
        "category": category,
        "date": date,
        "title": clean(title),
        "url": url or SOURCES[category],
        "body": body,
        "images": images or [],
    }


# -------------------------------------
# 活動公告解析
# -------------------------------------

def parse_events(soup):
    """
    活動公告使用獨立活動連結。
    只收集有日期和活動標題的項目。
    """

    items = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        url = absolute_url(anchor.get("href"))

        if not url:
            continue

        # 排除列表頁及其他公告類別
        if "/event/" not in urlparse(url).path:
            continue

        title = clean(
            anchor.get_text(" ", strip=True)
        )

        if not (4 <= len(title) <= 180):
            continue

        container = None
        date = ""

        for parent in [
            anchor.parent,
            *list(anchor.parents)[:7]
        ]:
            if not hasattr(parent, "get_text"):
                continue

            text = clean(
                parent.get_text(" ", strip=True)
            )
            dates = DATE_RE.findall(text)

            if len(dates) == 1:
                container = parent
                date = normal_date(dates[0])
                break

        if not container:
            continue

        key = make_key("event", date, title)

        if key in seen:
            continue

        seen.add(key)

        items.append(
            new_item(
                "event", date, title, url
            )
        )

    return items


def enrich_event(item):
    """
    從活動獨立頁面取得原始圖片。
    失敗時仍保留標題及連結。
    """

    try:
        soup = fetch(item["url"])

        content = None

        for selector in (
            ".event_content",
            ".view_content",
            ".view_cont",
            ".board_view",
            "article",
        ):
            content = soup.select_one(selector)

            if content:
                break

        if content is None:
            content = soup.body or soup

        images = []

        for img in content.find_all("img"):
            src = absolute_url(
                img.get("data-src")
                or img.get("src")
            )

            if not src:
                continue

            if any(
                word in src.lower()
                for word in (
                    "logo",
                    "icon",
                    "button",
                    "btn_",
                    "bullet",
                )
            ):
                continue

            if src not in images:
                images.append(src)

        item["images"] = images[:4]

    except requests.RequestException as exc:
        print(
            "Event image unavailable:",
            item["title"],
            type(exc).__name__
        )

    return item


# -------------------------------------
# 更新預告及系統公告解析
# -------------------------------------

def extract_lines(soup):
    """
    保留網頁文字的換行，
    同時保留公告之間的 ◆ 分隔符。
    """

    for tag in soup(
        ["script", "style", "nav", "footer"]
    ):
        tag.decompose()

    text = soup.get_text(
        "\n", strip=True
    )

    return [
        clean(line)
        for line in text.splitlines()
        if clean(line)
    ]


def split_announcements(soup, category):
    """
    官網格式：

    ◆
    2026/09/15
    | 09.16更新預告
    維護日期及時間...
    本次更新主要內容...
    注意事項...
    ◆

    以 ◆ 為分隔，避免正文內的日期
    被錯誤當成另一則公告。
    """

    lines = extract_lines(soup)

    blocks = []
    current = []

    for line in lines:
        if line == "◆":
            if current:
                blocks.append(current)
            current = []
        else:
            current.append(line)

    if current:
        blocks.append(current)

    items = []
    seen = set()

    for block in blocks:
        if not block:
            continue

        # 只接受開頭幾行包含發布日期的區塊
        date_index = -1

        for index, line in enumerate(block[:3]):
            if DATE_RE.fullmatch(line):
                date_index = index
                break

        if date_index < 0:
            continue

        date = normal_date(
            block[date_index]
        )

        remaining = block[date_index + 1:]

        if not remaining:
            continue

        title = remaining[0].lstrip("|").strip()

        if not (3 <= len(title) <= 180):
            continue

        body_lines = remaining[1:]

        # 去除列表底部的頁碼及年份導航
        body_lines = [
            line for line in body_lines
            if line not in (
                "年公告記錄",
                "上一頁",
                "下一頁",
            )
        ]

        body = "\n".join(body_lines)

        item = new_item(
            category,
            date,
            title,
            SOURCES[category],
            body
        )

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


# -------------------------------------
# 更新預告重點整理
# -------------------------------------

def section_lines(body, start, end=None):
    lines = [
        clean(line)
        for line in body.splitlines()
        if clean(line)
    ]

    result = []
    active = False

    for line in lines:
        if re.search(start, line, re.I):
            active = True
            continue

        if active and end and re.search(end, line, re.I):
            break

        if active:
            # 去除列表序號，但保留原文
            line = re.sub(
                r"^\d+\s*[)）.、]\s*",
                "",
                line
            )

            if line and line != "-":
                result.append(line)

    return result


def patch_fields(body):
    fields = []

    maintenance = re.search(
        r"維護日期及時間\s*[:：]\s*([^\n]+)",
        body
    )

    if maintenance:
        fields.append({
            "name": "🕒 維護時間",
            "value": short(
                clean(maintenance.group(1)),
                180
            ),
            "inline": False,
        })

    main = section_lines(
        body,
        r"本次更新主要內容",
        r"注意事項"
    )

    notes = section_lines(
        body,
        r"注意事項"
    )

    if main:
        fields.append({
            "name": "✨ 主要更新",
            "value": short(
                "\n".join("• " + x for x in main),
                1000
            ),
            "inline": False,
        })

    if notes:
        fields.append({
            "name": "⚠️ 注意事項／下架提醒",
            "value": short(
                "\n".join("• " + x for x in notes),
                1000
            ),
            "inline": False,
        })

    if not fields:
        fields.append({
            "name": "更新內容",
            "value": short(body or "請查看官方公告"),
            "inline": False,
        })

    return fields


# -------------------------------------
# Discord Embed
# -------------------------------------

def make_embed(item, changed=False):
    category = item["category"]

    prefix = (
        "🔄 公告更新｜"
        if changed else ""
    )

    embed = {
        "title": short(
            LABELS[category]
            + "｜"
            + prefix
            + item["title"],
            250
        ),
        "url": item["url"],
        "description": (
            "公告日期：" + item["date"]
        ),
        "color": COLORS[category],
        "fields": [],
        "footer": {
            "text": "加布｜跑 Online 官方情報"
        },
    }

    if category == "event":
        embed["fields"] = [{
            "name": "📋 活動詳情",
            "value": (
                "點擊公告標題查看完整活動，"
                "並參考下方官方圖片。"
            ),
            "inline": False,
        }]

        if item["images"]:
            embed["image"] = {
                "url": item["images"][0]
            }

    elif category == "patch":
        embed["fields"] = patch_fields(
            item["body"]
        )

    elif category == "news":
        lines = [
            line for line in item["body"].splitlines()
            if clean(line)
        ]

        # 長篇公告只節錄正文，
        # 不會將全文當作 AI 摘要。
        excerpt = "\n".join(lines[:10])

        embed["fields"] = [{
            "name": "📢 公告內容（節錄）",
            "value": short(
                excerpt or "請查看官方公告",
                1000
            ),
            "inline": False,
        }]

    return embed


def send_discord(payload):
    if DRY_RUN:
        print(
            "DRY RUN:",
            json.dumps(
                payload,
                ensure_ascii=False
            )[:2500]
        )
        return

    if not WEBHOOK:
        raise RuntimeError(
            "Missing DISCORD_WEBHOOK_URL"
        )

    response = SESSION.post(
        WEBHOOK,
        json=payload,
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            "Discord HTTP "
            + str(response.status_code)
        )

    print(
        "Discord sent:",
        response.status_code
    )


def notify(item, changed=False):
    embeds = [
        make_embed(item, changed)
    ]

    if item["category"] == "event":
        for image_url in item["images"][1:4]:
            embeds.append({
                "color": COLORS["event"],
                "image": {
                    "url": image_url
                },
            })

    send_discord({
        "username": "加布",
        "allowed_mentions": {
            "parse": []
        },
        "embeds": embeds,
    })


# -------------------------------------
# 公告紀錄
# -------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {}

    data = json.loads(
        STATE_FILE.read_text(
            encoding="utf-8"
        )
    )

    return data if isinstance(data, dict) else {}


def save_state(state):
    if DRY_RUN:
        return

    STATE_FILE.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )


# -------------------------------------
# 主程式
# -------------------------------------

def main():
    if TEST:
        send_discord({
            "username": "加布",
            "content": (
                "🧪 加布 2.1 "
                "官方公告通知測試成功！"
            ),
            "allowed_mentions": {
                "parse": []
            },
        })
        return

    old = load_state()

    # 解析方式升級後重新建立基準，
    # 防止舊版本的錯誤 ID 造成洗版。
    migrating = (
        old.get("_schema_version")
        != STATE_VERSION
    )

    state = (
        {"_schema_version": STATE_VERSION}
        if migrating
        else dict(old)
    )

    errors = []

    for category, url in SOURCES.items():
        print()
        print(
            "==========",
            category,
            "=========="
        )

        try:
            soup = fetch(url)

            if category == "event":
                items = parse_events(soup)
            else:
                items = split_announcements(
                    soup, category
                )

            print(
                f"{category}: parsed "
                f"{len(items)} entries"
            )

            if not items:
                raise RuntimeError(
                    "No announcements parsed"
                )

            # 測試時顯示前五筆。
            for item in items[:5]:
                print(
                    "PREVIEW:",
                    item["date"],
                    "|",
                    item["title"]
                )

                print(
                    "URL:",
                    item["url"]
                )

                if category == "patch":
                    fields = patch_fields(
                        item["body"]
                    )

                    for field in fields:
                        print(
                            "FIELD:",
                            field["name"],
                            field["value"][:250]
                        )

                if category == "news":
                    print(
                        "BODY:",
                        item["body"][:250]
                    )

            if DRY_RUN:
                print(
                    "Dry run: no messages sent"
                )
                continue

            prefix = category + ":"

            existing_keys = [
                key for key in old
                if key.startswith(prefix)
            ]

            # 升級或首次啟動：
            # 記錄目前公告，不發送舊消息。
            if migrating or not existing_keys:
                for item in items:
                    state[
                        prefix + item["key"]
                    ] = make_digest(item)

                save_state(state)

                print(
                    f"{category}: initialized; "
                    "no historical notifications"
                )
                continue

            pending = []

            # 官網通常按最新至最舊排序。
            for item in reversed(items):
                key = prefix + item["key"]
                digest = make_digest(item)

                if key not in old:
                    pending.append(
                        (item, False)
                    )

                elif old[key] != digest:
                    pending.append(
                        (item, True)
                    )

            # 每次最多發送八則，
            # 避免長時間停機後洗版。
            for item, changed in pending[-8:]:
                if category == "event":
                    item = enrich_event(item)

                notify(item, changed)

                state[
                    prefix + item["key"]
                ] = make_digest(item)

                save_state(state)
                time.sleep(1)

            print(
                f"{category}: "
                f"{len(pending)} pending changes"
            )

        except Exception as exc:
            print(
                "ERROR:",
                category,
                type(exc).__name__,
                str(exc)
            )
            errors.append(category)

    if errors:
        print(
            "Failed categories:",
            ", ".join(errors)
        )
        sys.exit(1)

    print()
    print(
        "Gabu official news check completed"
    )


if __name__ == "__main__":
    main()
