
"""
加布 2.0：跑 Online 官方公告監測

功能：
- 活動公告
- 更新預告
- 系統公告
- Discord Embed 超連結
- 活動圖片
- 首次執行不發送歷史公告
- 防止重複通知
- dry_run 測試模式

不需要付費 AI API。
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


# =====================================
# 基本設定
# =====================================

BASE = "https://www.talesrunner.com.hk"

SOURCES = {
    "event": BASE + "/notice/notice.php?type=event",
    "patch": BASE + "/notice/notice.php?type=patch",
    "news": BASE + "/notice/notice.php",
}

STATE_FILE = Path("official_news_state.json")

WEBHOOK = os.environ.get(
    "DISCORD_WEBHOOK_URL", ""
).strip()

DRY_RUN = (
    os.environ.get("DRY_RUN", "false").lower()
    == "true"
)

TEST = (
    os.environ.get(
        "TEST_NOTIFICATION", "false"
    ).lower()
    == "true"
)

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(compatible; GabuGuildNews/2.0)"
    )
})

DATE_RE = re.compile(
    r"20\d{2}\s*[/.-]\s*"
    r"\d{1,2}\s*[/.-]\s*\d{1,2}"
)

COLORS = {
    "event": 0xA855F7,
    "patch": 0x4786ED,
    "news": 0xE8A23C,
}

LABELS = {
    "event": "🎉 活動公告",
    "patch": "🎬 更新預告",
    "news": "📢 系統公告",
}


# =====================================
# 通用工具
# =====================================

def clean(value):
    return re.sub(
        r"\s+", " ", value or ""
    ).strip()


def normalize_date(value):
    value = re.sub(
        r"\s+", "", value
    )

    return (
        value.replace("-", "/")
        .replace(".", "/")
    )


def shorten(value, limit=900):
    if len(value) <= limit:
        return value

    return value[:limit - 1] + "…"


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
        url,
        timeout=35
    )

    response.raise_for_status()

    if response.apparent_encoding:
        response.encoding = (
            response.apparent_encoding
        )

    return BeautifulSoup(
        response.text,
        "html.parser"
    )


def make_key(category, date, title):
    raw = f"{category}|{date}|{title}"

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def make_digest(item):
    raw = json.dumps(
        {
            "title": item["title"],
            "body": item["body"],
            "images": item["images"],
        },
        ensure_ascii=False,
        sort_keys=True
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:16]


# =====================================
# 尋找公告連結
# =====================================

def find_detail_url(container, title, category):
    """
    優先尋找公告本身的連結。
    不把列表頁誤當成獨立公告網址。
    """

    anchors = container.find_all(
        "a",
        href=True
    )

    title_short = clean(title)[:8]

    candidates = []

    for anchor in anchors:
        url = absolute_url(
            anchor.get("href")
        )

        if not url:
            continue

        if url == SOURCES[category]:
            continue

        anchor_text = clean(
            anchor.get_text(" ", strip=True)
        )

        score = 0

        if (
            title_short
            and title_short in anchor_text
        ):
            score += 10

        if "notice" in url.lower():
            score += 2

        if "news" in url.lower():
            score += 1

        candidates.append(
            (score, url)
        )

    if not candidates:
        return ""

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    return candidates[0][1]


# =====================================
# 解析單則公告
# =====================================

def build_item(
    container,
    category,
    date,
    title,
    url=""
):
    title = clean(title)

    if not title:
        return None

    if len(title) > 180:
        return None

    if DATE_RE.fullmatch(title):
        return None

    if not url:
        url = find_detail_url(
            container,
            title,
            category
        )

    # 讀取公告區塊內的文字
    lines = [
        clean(line)
        for line in container.get_text(
            "\n",
            strip=True
        ).splitlines()
    ]

    lines = [
        line
        for line in lines
        if line
    ]

    # 去除日期及標題
    body_lines = []

    title_found = False

    for line in lines:
        if not title_found:
            if title in line:
                title_found = True

            continue

        if DATE_RE.fullmatch(line):
            break

        body_lines.append(line)

    body = "\n".join(body_lines)

    # 活動圖片
    images = []

    if category == "event":
        for img in container.find_all("img"):
            src = absolute_url(
                img.get("data-src")
                or img.get("src")
            )

            if not src:
                continue

            lower = src.lower()

            if any(
                word in lower
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

    return {
        "key": make_key(
            category,
            date,
            title
        ),
        "category": category,
        "date": date,
        "title": title,
        "url": url,
        "body": body,
        "images": images[:4],
    }


# =====================================
# 解析公告列表
# =====================================

def parse_items(soup, category):
    """
    先尋找包含日期的獨立公告區塊。

    若網站的 HTML 沒有明確區塊，
    則使用日期分段作為後備方法。
    """

    for tag in soup(
        ["script", "style", "nav", "footer"]
    ):
        tag.decompose()

    items = []
    seen = set()

    # 方法一：尋找每個公告的獨立容器
    for node in soup.find_all(
        string=DATE_RE
    ):
        match = DATE_RE.search(
            str(node)
        )

        if not match:
            continue

        date = normalize_date(
            match.group()
        )

        container = None

        for parent in [
            node.parent,
            *list(node.parents)[:7]
        ]:
            if not getattr(
                parent,
                "get_text",
                None
            ):
                continue

            text = clean(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            dates = DATE_RE.findall(text)

            # 避免選到包含整頁公告的大容器
            if len(dates) != 1:
                continue

            if not (
                12 <= len(text) <= 9000
            ):
                continue

            if not parent.find("a"):
                continue

            container = parent
            break

        if container is None:
            continue

        lines = [
            clean(line)
            for line in container.get_text(
                "\n",
                strip=True
            ).splitlines()
        ]

        lines = [
            line for line in lines
            if line and line not in (
                "◆", "|", "Image"
            )
        ]

        date_index = next(
            (
                i
                for i, line in enumerate(lines)
                if DATE_RE.search(line)
            ),
            -1
        )

        if date_index < 0:
            continue

        title_candidates = [
            line.lstrip("|").strip()
            for line in lines[
                date_index + 1:
            ]
        ]

        title_candidates = [
            line
            for line in title_candidates
            if line
            and not DATE_RE.fullmatch(line)
        ]

        if not title_candidates:
            continue

        title = title_candidates[0]

        item = build_item(
            container,
            category,
            date,
            title
        )

        if not item:
            continue

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    # 方法二：按日期分段，補充未成功解析的公告
    #
    # 只把有獨立連結的區塊加入結果，
    # 避免整頁文字被誤認為一則公告。

    for anchor in soup.find_all(
        "a",
        href=True
    ):
        url = absolute_url(
            anchor.get("href")
        )

        if not url:
            continue

        if url == SOURCES[category]:
            continue

        title = clean(
            anchor.get_text(
                " ",
                strip=True
            )
        )

        if not (
            4 <= len(title) <= 180
        ):
            continue

        container = None
        date = ""

        for parent in [
            anchor.parent,
            *list(anchor.parents)[:6]
        ]:
            if not getattr(
                parent,
                "get_text",
                None
            ):
                continue

            text = clean(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            dates = DATE_RE.findall(text)

            if len(dates) == 1:
                container = parent
                date = normalize_date(
                    dates[0]
                )
                break

        if container is None:
            continue

        item = build_item(
            container,
            category,
            date,
            title,
            url
        )

        if not item:
            continue

        if item["key"] in seen:
            continue

        seen.add(item["key"])
        items.append(item)

    return items


# =====================================
# 取得獨立公告的正文及圖片
# =====================================

def enrich_item(item):
    """
    只有找到獨立公告網址才讀取詳情。
    若詳情頁無法讀取，保留列表資料。
    """

    url = item["url"]

    if not url:
        return item

    if url == SOURCES[item["category"]]:
        return item

    try:
        soup = fetch(url)

        for tag in soup(
            ["script", "style", "nav", "footer"]
        ):
            tag.decompose()

        candidates = []

        for selector in (
            ".notice_view",
            ".view_content",
            ".view_cont",
            ".board_view",
            ".content",
            "article",
        ):
            candidates.extend(
                soup.select(selector)
            )

        if not candidates:
            candidates = [
                soup.body or soup
            ]

        content = max(
            candidates,
            key=lambda element: len(
                element.get_text(
                    " ",
                    strip=True
                )
            )
        )

        text = content.get_text(
            "\n",
            strip=True
        )

        if text:
            item["body"] = text

        if item["category"] == "event":
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
                    )
                ):
                    continue

                if src not in images:
                    images.append(src)

            if images:
                item["images"] = images[:4]

    except requests.RequestException as exc:
        print(
            "Detail page unavailable:",
            item["title"],
            type(exc).__name__
        )

    return item


# =====================================
# Discord 訊息格式
# =====================================

def make_embed(item, changed=False):
    category = item["category"]

    prefix = (
        "🔄 公告內容更新｜"
        if changed
        else ""
    )

    title = (
        LABELS[category]
        + "｜"
        + prefix
        + item["title"]
    )

    fields = []

    body = item["body"]

    if category == "event":
        fields.append({
            "name": "📋 活動詳情",
            "value": (
                "請查看官方活動圖片及"
                "完整公告。"
            ),
            "inline": False,
        })

    elif category == "patch":
        if body:
            fields.append({
                "name": "✨ 官方更新內容",
                "value": shorten(
                    body,
                    1000
                ),
                "inline": False,
            })

    elif category == "news":
        if body:
            fields.append({
                "name": "📢 公告內容",
                "value": shorten(
                    body,
                    1000
                ),
                "inline": False,
            })

    embed = {
        "title": shorten(title, 250),
        "color": COLORS[category],
        "description": (
            "公告日期：" + item["date"]
        ),
        "fields": fields,
        "footer": {
            "text": (
                "加布｜跑 Online 官方情報"
            )
        },
    }

    # Discord Embed 標題超連結
    if item["url"]:
        embed["url"] = item["url"]

    if (
        category == "event"
        and item["images"]
    ):
        embed["image"] = {
            "url": item["images"][0]
        }

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
    payload = {
        "username": "加布",
        "allowed_mentions": {
            "parse": []
        },
        "embeds": [
            make_embed(
                item,
                changed
            )
        ],
    }

    # 多張活動圖片
    if (
        item["category"] == "event"
        and len(item["images"]) > 1
    ):
        for url in item["images"][1:4]:
            payload["embeds"].append({
                "color": COLORS["event"],
                "image": {
                    "url": url
                }
            })

    send_discord(payload)


# =====================================
# 公告紀錄
# =====================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    return json.loads(
        STATE_FILE.read_text(
            encoding="utf-8"
        )
    )


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


# =====================================
# 主程式
# =====================================

def main():
    if TEST:
        send_discord({
            "username": "加布",
            "content": (
                "🧪 加布 2.0 "
                "官方公告通知測試成功！"
            ),
            "allowed_mentions": {
                "parse": []
            },
        })
        return

    old = load_state()
    state = dict(old)

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

            items = parse_items(
                soup,
                category
            )

            print(
                category,
                ": parsed",
                len(items),
                "entries"
            )

            if not items:
                raise RuntimeError(
                    "No announcements parsed"
                )

            # 測試時顯示前 5 筆公告，
            # 方便檢查是否抓對標題及網址。
            for item in items[:5]:
                print(
                    "PREVIEW:",
                    item["date"],
                    "|",
                    item["title"]
                )

                print(
                    "URL:",
                    item["url"] or (
                        "NO DETAIL URL"
                    )
                )

            # dry_run 只檢查解析結果。
            # 不下載全部詳情，也不發通知。
            if DRY_RUN:
                continue

            prefix = category + ":"

            # 第一次成功解析某類公告：
            # 只記錄，不發送歷史消息。
            if not any(
                key.startswith(prefix)
                for key in old
            ):
                for item in items:
                    state[
                        prefix + item["key"]
                    ] = make_digest(item)

                save_state(state)

                print(
                    category,
                    ": initialized;"
                    " no historical notifications"
                )

                continue

            # 找出新公告
            pending = []

            for item in reversed(items):
                key = prefix + item["key"]

                if key not in old:
                    pending.append(
                        (item, False)
                    )

            # 避免一次發送大量公告
            for item, changed in pending[-8:]:
                # 正式通知前讀取獨立公告
                item = enrich_item(item)

                notify(
                    item,
                    changed
                )

                state[
                    prefix + item["key"]
                ] = make_digest(item)

                save_state(state)

                time.sleep(1)

            print(
                category,
                ":",
                len(pending),
                "new announcements"
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
