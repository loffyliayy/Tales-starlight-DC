
"""TalesRunner official news -> Discord. No paid API. Python 3.11+."""

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
    "event": f"{BASE}/notice/notice.php?type=event",
    "patch": f"{BASE}/notice/notice.php?type=patch",
    "news": f"{BASE}/notice/notice.php",
}

STATE = Path("official_news_state.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"
TEST = os.environ.get("TEST_NOTIFICATION", "").lower() == "true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; GabuGuildNews/2.0)"
}

DATE = re.compile(r"20\d\d\s*[/.-]\s*\d{1,2}\s*[/.-]\s*\d{1,2}")

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def get(url):
    response = SESSION.get(url, timeout=35)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return BeautifulSoup(response.text, "html.parser")


def absolute(href):
    url = urljoin(BASE, href or "")
    allowed = {"www.talesrunner.com.hk", "talesrunner.com.hk"}
    return url if urlparse(url).hostname in allowed else ""


def item_container(date_node):
    """Find the smallest useful announcement wrapper near a date."""

    for p in [date_node, *list(date_node.parents)[:7]]:
        if not getattr(p, "get_text", None):
            continue

        text = clean(p.get_text(" ", strip=True))
        dates = DATE.findall(text)

        if (
            len(dates) == 1
            and 12 <= len(text) <= 9000
            and (p.find("a") or p.find("img"))
        ):
            return p

    return date_node.parent


def parse_items(soup, category):
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    items = []
    seen = set()

    for node in soup.find_all(string=DATE):
        raw = clean(str(node))
        match = DATE.search(raw)

        if not match:
            continue

        date = (
            re.sub(r"\s+", "", match.group())
            .replace(".", "/")
            .replace("-", "/")
        )

        container = item_container(node)

        if container is None:
            continue

        lines = [
            clean(x)
            for x in container.get_text("\n", strip=True).splitlines()
        ]

        lines = [
            x for x in lines
            if x and x not in {"◆", "|"}
        ]

        date_positions = [
            i for i, line in enumerate(lines)
            if DATE.search(line)
        ]

        if len(date_positions) != 1:
            continue

        content = [
            x for x in lines[date_positions[0] + 1:]
            if x not in {"Image", "◆"}
        ]

        if not content:
            continue

        title = clean(content[0]).lstrip("|").strip()

        if (
            not title
            or len(title) > 180
            or DATE.fullmatch(title)
        ):
            continue

        anchors = container.find_all("a", href=True)

        best = next(
            (
                a for a in anchors
                if title[:8] in clean(a.get_text(" ", strip=True))
            ),
            None
        )

        if best is None:
            best = next(
                (
                    a for a in anchors
                    if "notice" in a["href"]
                    or "event" in a["href"]
                ),
                None
            )

        url = absolute(best["href"]) if best else ""

        if not url or url == SOURCES[category]:
            url = SOURCES[category]

        images = []

        for img in container.find_all("img"):
            src = absolute(img.get("data-src") or img.get("src"))

            if src and not any(
                k in src.lower()
                for k in ("logo", "icon", "bullet", "btn_", "button")
            ):
                if src not in images:
                    images.append(src)

        body = "\n".join(content[1:])

        key = hashlib.sha256(
            f"{category}|{date}|{title}".encode()
        ).hexdigest()[:20]

        if key in seen:
            continue

        seen.add(key)

        items.append({
            "key": key,
            "category": category,
            "date": date,
            "title": title,
            "url": url,
            "body": body,
            "images": images[:4],
        })

    return items


def digest(item):
    source = (
        item["title"]
        + item["body"]
        + "|".join(item["images"])
    )

    return hashlib.sha256(
        source.encode()
    ).hexdigest()[:16]


def lines_from_section(body, start, end, max_items):
    if not body:
        return []

    match = re.search(start, body, flags=re.I)

    if not match:
        return []

    text = body[match.end():]
    stop = re.search(end, text, flags=re.I) if end else None

    if stop:
        text = text[:stop.start()]

    parts = re.split(
        r"\n|(?=\d+\s*[)）.、])",
        text
    )

    return [
        clean(re.sub(r"^\d+\s*[)）.、]\s*", "", x))
        for x in parts
        if clean(x)
    ][:max_items]


def shorten(s, limit=850):
    if len(s) <= limit:
        return s

    return s[:limit - 1] + "…"


def embed_for(item, changed=False):
    category = item["category"]

    label = {
        "event": "🎉 活動公告",
        "patch": "🎬 更新預告",
        "news": "📢 系統公告",
    }[category]

    color = {
        "event": 0xA855F7,
        "patch": 0x4786ED,
        "news": 0xE8A23C,
    }[category]

    title = (
        "🔄 公告內容更新｜" if changed else ""
    ) + item["title"]

    body = item["body"]
    fields = []

    if category == "patch":
        maintenance = re.search(
            r"維護日期及時間\s*[:：]?\s*([^\n]{1,90})",
            body
        )

        if maintenance:
            fields.append({
                "name": "🕒 維護時間",
                "value": shorten(
                    clean(maintenance.group(1)),
                    180
                ),
                "inline": False,
            })

        main = lines_from_section(
            body,
            r"本次更新主要內容\s*[:：]?",
            r"注意事項\s*[:：]?",
            12
        )

        notes = lines_from_section(
            body,
            r"注意事項\s*[:：]?",
            None,
            12
        )

        if main:
            fields.append({
                "name": "✨ 主要更新",
                "value": shorten(
                    "\n".join("• " + x for x in main),
                    950
                ),
                "inline": False,
            })

        if notes:
            fields.append({
                "name": "⚠️ 注意事項／下架提醒",
                "value": shorten(
                    "\n".join("• " + x for x in notes),
                    950
                ),
                "inline": False,
            })

    elif category == "news" and body:
        body_lines = [
            clean(x)
            for x in body.splitlines()
            if clean(x)
        ]

        body_lines = [
            x for x in body_lines
            if x not in (
                "各位跑者好，",
                "各位跑者好﹐",
                "《跑Online》營運團隊 敬上",
            )
        ]

        if body_lines:
            fields.append({
                "name": "公告重點（節錄）",
                "value": shorten(
                    "\n".join(body_lines[:9]),
                    950
                ),
                "inline": False,
            })

    elif category == "event":
        fields.append({
            "name": "活動詳情",
            "value": (
                "官方公告以圖片為主，"
                "請查看下方圖片及完整活動頁。"
            ),
            "inline": False,
        })

    embed = {
        "title": shorten(
            f"{label}｜{title}",
            250
        ),
        "url": item["url"],
        "color": color,
        "description": f"公告日期：{item['date']}",
        "fields": fields,
        "footer": {
            "text": "加布｜跑 Online 官方情報"
        },
    }

    if category == "event" and item["images"]:
        embed["image"] = {
            "url": item["images"][0]
        }

    return embed


def send(payload):
    if DRY_RUN:
        print(
            "DRY RUN payload:",
            json.dumps(
                payload,
                ensure_ascii=False
            )[:2200]
        )
        return

    if not WEBHOOK:
        raise RuntimeError(
            "Missing DISCORD_WEBHOOK_URL GitHub secret"
        )

    response = SESSION.post(
        WEBHOOK,
        json=payload,
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"Discord HTTP {response.status_code}; "
            "check webhook permissions or IP filtering"
        )

    print("Discord sent:", response.status_code)


def notify(item, changed=False):
    payload = {
        "username": "加布",
        "allowed_mentions": {
            "parse": []
        },
        "embeds": [
            embed_for(item, changed)
        ],
    }

    if (
        item["category"] == "event"
        and len(item["images"]) > 1
    ):
        payload["embeds"].extend(
            {
                "url": item["url"],
                "image": {"url": url},
                "color": 0xA855F7,
            }
            for url in item["images"][1:4]
        )

    send(payload)


def main():
    if TEST:
        send({
            "username": "加布",
            "content": "🧪 加布 2.0 官方公告通知測試成功！",
            "allowed_mentions": {
                "parse": []
            },
        })
        return

    old = (
        json.loads(
            STATE.read_text(encoding="utf-8")
        )
        if STATE.exists()
        else {}
    )

    fresh = dict(old)
    errors = []

    for category, url in SOURCES.items():
        try:
            soup = get(url)
            items = parse_items(soup, category)

            print(
                f"{category}: parsed {len(items)} entries"
            )

            if not items:
                raise RuntimeError(
                    "No entries parsed: "
                    "website markup may have changed; "
                    "no state overwritten"
                )

            prefix = category + ":"

            # First successful run:
            # Record existing announcements silently.
            if not any(
                key.startswith(prefix)
                for key in old
            ):
                for item in items:
                    fresh[prefix + item["key"]] = digest(item)

                print(
                    f"{category}: initialized; "
                    "no historical notifications"
                )
                continue

            # Process oldest first.
            # Limit notifications after a long outage.
            pending = [
                (
                    item,
                    (prefix + item["key"]) in old
                )
                for item in reversed(items)
                if old.get(
                    prefix + item["key"]
                ) != digest(item)
            ]

            for item, changed in pending[-8:]:
                notify(item, changed)

                fresh[
                    prefix + item["key"]
                ] = digest(item)

                STATE.write_text(
                    json.dumps(
                        fresh,
                        ensure_ascii=False,
                        indent=2
                    ) + "\n",
                    encoding="utf-8"
                )

                time.sleep(1)

        except Exception as exc:
            print(
                f"ERROR {category}: "
                f"{type(exc).__name__}: {exc}"
            )
            errors.append(category)

    if not DRY_RUN:
        STATE.write_text(
            json.dumps(
                fresh,
                ensure_ascii=False,
                indent=2
            ) + "\n",
            encoding="utf-8"
        )

    if errors:
        print(
            "Some categories failed:",
            ", ".join(errors)
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
