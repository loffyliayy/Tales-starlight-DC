
"""
加布 2.3 — 跑 Online 官方公告監測

首次啟動：只建立基準，不發送歷史公告。
日常執行：只發送新增或內容有變動的公告。
測試模式：不發送訊息，也不修改公告紀錄。
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

DRY_RUN = (
    os.getenv("DRY_RUN", "true").lower() == "true"
)

TEST = (
    os.getenv("TEST_NOTIFICATION", "false").lower()
    == "true"
)

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
    response = SESSION.get(
        url, timeout=35
    )
    response.raise_for_status()

    if response.apparent_encoding:
        response.encoding = (
            response.apparent_encoding
        )

    return BeautifulSoup(
        response.text, "html.parser"
    )


def item_key(category, date, title):
    raw = f"{category}|{date}|{title}"

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def item_digest(item):
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
    category, date, title,
    body="", url="", images=None
):
    return {
        "category": category,
        "date": date,
        "title": clean(title),
        "body": body,
        "url": url or SOURCES[category],
        "images": images or [],
        "key": item_key(
            category, date, title
        ),
    }


# =====================================
# 活動公告
# =====================================

def parse_events(soup):
    items = []
    seen = set()

    for anchor in soup.find_all(
        "a", href=True
    ):
        url = absolute_url(
            anchor.get("href")
        )

        if not url:
            continue

        if "/event/" not in urlparse(url).path:
            continue

        title = clean(
            anchor.get_text(" ", strip=True)
        )

        if not 4 <= len(title) <= 180:
            continue

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
                date = normalize_date(dates[0])
                break

        if not date:
            continue

        item = make_item(
            "event", date, title, url=url
        )

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


def enrich_event(item):
    """只在準備發送活動時讀取活動圖片。"""

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

    except requests.RequestException:
        print(
            "WARNING: Could not load event images"
        )

    return item


# =====================================
# 更新預告及系統公告
# =====================================

def extract_lines(soup):
    copy = BeautifulSoup(
        str(soup), "html.parser"
    )

    for tag in copy(
        ["script", "style", "nav", "footer"]
    ):
        tag.decompose()

    return [
        clean(line)
        for line in copy.get_text(
            "\n", strip=True
        ).splitlines()
        if clean(line)
    ]


def split_announcements(soup, category):
    """
    官網公告結構：

    ◆
    2026/09/15
    |
    09.16更新預告
    正文...
    ◆
    """

    lines = extract_lines(soup)

    blocks = []
    current = None

    for line in lines:
        if line == "◆":
            if current is not None:
                blocks.append(current)

            current = []
            continue

        if current is not None:
            current.append(line)

    if current is not None:
        blocks.append(current)

    items = []
    seen = set()

    for block in blocks:
        date_index = -1

        for index, line in enumerate(block[:3]):
            if DATE_RE.fullmatch(line):
                date_index = index
                break

        if date_index < 0:
            continue

        date = normalize_date(
            block[date_index]
        )

        remaining = block[date_index + 1:]

        while remaining and remaining[0] in (
            "|", "｜", "-"
        ):
            remaining.pop(0)

        if not remaining:
            continue

        title = clean(
            remaining[0].lstrip("|｜")
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
            body="\n".join(body_lines),
        )

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


# =====================================
# 更新預告內容整理
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
                line.startswith(("「", "」", "『", "』"))
                or current.endswith(
                    ("「", "」", "『", "』")
                )
            ):
                current += line
            else:
                current += " " + line

        else:
            current = line

    if current:
        result.append(current)

    return result


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

        if active and end:
            if re.search(end, line, re.I):
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
                clean(maintenance.group(1)),
                180,
            ),
            "inline": False,
        })

    main_items = combine_numbered_lines(
        section_lines(
            body,
            r"本次更新主要內容",
            r"注意事項",
        )
    )

    note_items = combine_numbered_lines(
        section_lines(
            body,
            r"注意事項",
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
            "name": "⚠️ 注意事項／下架提醒",
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
# Discord 訊息
# =====================================

def make_embed(item, changed=False):
    category = item["category"]

    prefix = (
        "🔄 公告內容更新｜"
        if changed else ""
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
            "公告日期：" + item["date"]
        ),
        "color": COLORS[category],
        "footer": {
            "text": "加布｜跑 Online 官方情報"
        },
    }

    if category == "event":
        embed["fields"] = [{
            "name": "📋 活動詳情",
            "value": (
                "點擊上方標題查看官方完整活動。"
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
            clean(line)
            for line in item["body"].splitlines()
            if clean(line)
        ]

        excerpt = "\n".join(lines[:12])

        embed["fields"] = [{
            "name": "📢 公告內容（節錄）",
            "value": shorten(
                excerpt or "請查看官方公告",
                1000,
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
                ensure_ascii=False,
            )[:2500],
        )
        return

    if not WEBHOOK:
        raise RuntimeError(
            "Missing DISCORD_WEBHOOK_URL"
        )

    response = SESSION.post(
        WEBHOOK,
        json=payload,
        timeout=30,
    )

    if not response.ok:
        # 不輸出 Webhook URL 或 Secret。
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

    except (ValueError, OSError) as exc:
        raise RuntimeError(
            "Cannot read official_news_state.json"
        ) from exc

    if not isinstance(data, dict):
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
    print("Gabu 2.3 starting")
    print("DRY_RUN:", DRY_RUN)

    if TEST:
        send_discord({
            "username": "加布",
            "content": (
                "🧪 加布 2.3 測試成功！"
                "\n官方公告通知功能已準備就緒。"
            ),
            "allowed_mentions": {
                "parse": []
            },
        })
        return

    # 先讀取三類公告。
    # 只要有任何一類解析失敗，
    # 就不發送、不初始化、不覆蓋紀錄。
    all_items = {}
    errors = []

    for category, url in SOURCES.items():
        print()
        print(
            "==========",
            category.upper(),
            "==========",
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

            for item in items[:5]:
                print(
                    "PREVIEW:",
                    item["date"],
                    "|",
                    item["title"],
                )

                print(
                    "URL:",
                    item["url"]
                )

                if category == "patch":
                    for field in patch_fields(
                        item["body"]
                    ):
                        print(
                            "FIELD:",
                            field["name"],
                            field["value"][:500],
                        )

                elif category == "news":
                    print(
                        "BODY:",
                        item["body"][:300]
                    )

            all_items[category] = items

        except Exception as exc:
            print(
                "ERROR:",
                category,
                type(exc).__name__,
                str(exc),
            )

            errors.append(category)

    if errors:
        print(
            "Failed categories:",
            ", ".join(errors),
        )
        sys.exit(1)

    if DRY_RUN:
        print()
        print(
            "DRY RUN completed: "
            "no Discord messages or state changes"
        )
        return

    old = load_state()

    # 升級到 v4 時，所有現有公告
    # 都只會建立基準，不會發送。
    if old.get("_schema_version") != STATE_VERSION:
        state = {
            "_schema_version": STATE_VERSION
        }

        for category, items in all_items.items():
            for item in items:
                key = category + ":" + item["key"]
                state[key] = item_digest(item)

            print(
                f"{category}: initialized "
                f"{len(items)} entries; "
                "no historical notifications"
            )

        save_state(state)

        print(
            "Initial baseline saved successfully"
        )
        return

    state = dict(old)

    for category, items in all_items.items():
        prefix = category + ":"

        existing = [
            key for key in state
            if key.startswith(prefix)
        ]

        # 防止某類公告的紀錄意外遺失
        # 時，把所有歷史公告重新發送。
        if not existing:
            for item in items:
                key = prefix + item["key"]
                state[key] = item_digest(item)

            save_state(state)

            print(
                f"{category}: baseline restored; "
                "no historical notifications"
            )
            continue

        pending = []

        # 網站最新公告排在最前。
        # 發送時按舊至新的次序。
        for item in reversed(items):
            key = prefix + item["key"]
            digest = item_digest(item)

            if key not in state:
                pending.append(
                    (item, False)
                )

            elif state[key] != digest:
                pending.append(
                    (item, True)
                )

        if not pending:
            print(
                f"{category}: no changes"
            )
            continue

        # 如果一次發現超過 8 則，
        # 先保留較早的待發公告，
        # 每次最多發送 8 則。
        batch = pending[:8]

        for item, changed in batch:
            if category == "event":
                item = enrich_event(item)

            # 只有 Discord 成功接收，
            # 才更新這一則公告的紀錄。
            notify(item, changed)

            key = prefix + item["key"]
            state[key] = item_digest(item)

            save_state(state)
            time.sleep(1)

        print(
            f"{category}: sent "
            f"{len(batch)} notifications"
        )

        if len(pending) > len(batch):
            print(
                f"{category}: "
                f"{len(pending) - len(batch)} "
                "changes remaining"
            )

    print()
    print(
        "Gabu 2.3 completed successfully"
    )


if __name__ == "__main__":
    main()
