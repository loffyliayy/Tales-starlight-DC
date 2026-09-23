
"""
加布 2.1 — 官網結構診斷版

目的：
1. 確認活動公告仍能正常解析。
2. 檢查更新預告及系統公告的 HTML 結構。
3. 印出公告頁的文字、日期、連結及 HTML 類別。

安全設定：
- 本診斷版不會發送 Discord 訊息。
- 不會修改 official_news_state.json。
- 不需要付費 API。
"""

import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ======================================
# 基本設定
# ======================================

BASE = "https://www.talesrunner.com.hk"

SOURCES = {
    "event": (
        BASE + "/notice/notice.php?type=event"
    ),
    "patch": (
        BASE + "/notice/notice.php?type=patch"
    ),
    "news": (
        BASE + "/notice/notice.php"
    ),
}

DATE_RE = re.compile(
    r"20\d{2}\s*[/.-]\s*"
    r"\d{1,2}\s*[/.-]\s*\d{1,2}"
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


# ======================================
# 通用工具
# ======================================

def clean(value):
    return re.sub(
        r"\s+",
        " ",
        value or ""
    ).strip()


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

    if parsed.scheme not in (
        "http",
        "https"
    ):
        return ""

    if parsed.hostname not in (
        "www.talesrunner.com.hk",
        "talesrunner.com.hk"
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

    print(
        "HTTP STATUS:",
        response.status_code
    )

    print(
        "FINAL URL:",
        response.url
    )

    print(
        "HTML SIZE:",
        len(response.content)
    )

    return BeautifulSoup(
        response.text,
        "html.parser"
    )


# ======================================
# 活動公告
# ======================================

def parse_events(soup):
    """
    保留上一版的活動連結解析方式。
    """

    items = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True
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
            *list(anchor.parents)[:7]
        ]:
            if not hasattr(
                parent,
                "get_text"
            ):
                continue

            text = clean(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            dates = DATE_RE.findall(
                text
            )

            if len(dates) == 1:
                container = parent

                date = normalize_date(
                    dates[0]
                )

                break

        if container is None:
            continue

        key = (
            date,
            title,
            url
        )

        if key in seen:
            continue

        seen.add(key)

        items.append({
            "date": date,
            "title": title,
            "url": url
        })

    return items


# ======================================
# 更新預告及系統公告診斷
# ======================================

def debug_page(soup, category):
    print()
    print(
        "========== DEBUG",
        category.upper(),
        "=========="
    )

    if soup.title:
        print(
            "DEBUG TITLE:",
            clean(
                soup.title.get_text(
                    " ",
                    strip=True
                )
            )
        )
    else:
        print(
            "DEBUG TITLE: NO TITLE"
        )

    # 先複製網頁，避免移除標籤後
    # 影響後面的 HTML 診斷。

    copy = BeautifulSoup(
        str(soup),
        "html.parser"
    )

    for tag in copy(
        [
            "script",
            "style",
            "nav",
            "footer"
        ]
    ):
        tag.decompose()

    lines = [
        clean(line)
        for line in copy.get_text(
            "\n",
            strip=True
        ).splitlines()
        if clean(line)
    ]

    print(
        "DEBUG LINE COUNT:",
        len(lines)
    )

    print(
        "DEBUG SEPARATORS:",
        sum(
            "◆" in line
            for line in lines
        )
    )

    print(
        "DEBUG DATES:",
        sum(
            bool(DATE_RE.search(line))
            for line in lines
        )
    )

    # 顯示前 70 行文字，
    # 確認日期、標題和正文排列。

    print()
    print(
        "----- FIRST 70 LINES -----"
    )

    for index, line in enumerate(
        lines[:70]
    ):
        print(
            "DEBUG LINE",
            index,
            repr(line[:200])
        )

    # 額外顯示日期附近的文字，
    # 避免公告藏在頁面較後位置。

    print()
    print(
        "----- DATE CONTEXT -----"
    )

    date_positions = [
        index
        for index, line in enumerate(lines)
        if DATE_RE.search(line)
    ]

    for position in date_positions[:8]:
        start = max(
            0,
            position - 2
        )

        end = min(
            len(lines),
            position + 5
        )

        print(
            "DATE AT LINE:",
            position
        )

        for index in range(
            start,
            end
        ):
            print(
                "CONTEXT",
                index,
                repr(
                    lines[index][:200]
                )
            )

    # 查看公告相關連結，
    # 判斷有沒有獨立公告網址。

    print()
    print(
        "----- ANNOUNCEMENT LINKS -----"
    )

    links = []
    seen_urls = set()

    for anchor in soup.find_all(
        "a",
        href=True
    ):
        href = anchor.get(
            "href",
            ""
        )

        url = absolute_url(href)

        if not url:
            continue

        if url in seen_urls:
            continue

        title = clean(
            anchor.get_text(
                " ",
                strip=True
            )
        )

        # 只顯示可能與公告有關的連結。
        if not (
            DATE_RE.search(title)
            or "notice" in url.lower()
            or "news" in url.lower()
            or "patch" in url.lower()
            or "更新" in title
            or "公告" in title
        ):
            continue

        seen_urls.add(url)

        links.append({
            "text": title[:100],
            "url": url
        })

    print(
        "DEBUG LINK COUNT:",
        len(links)
    )

    for link in links[:25]:
        print(
            "DEBUG LINK:",
            repr(link["text"]),
            "=>",
            link["url"]
        )

    # 查看日期所在的 HTML 標籤
    # 及其父元素的 class。

    print()
    print(
        "----- HTML STRUCTURE -----"
    )

    date_nodes = soup.find_all(
        string=DATE_RE
    )

    print(
        "DEBUG DATE NODES:",
        len(date_nodes)
    )

    for node in date_nodes[:8]:
        print()
        print(
            "HTML DATE:",
            repr(
                clean(str(node))[:120]
            )
        )

        parent = node.parent

        for level in range(4):
            if parent is None:
                break

            if not getattr(
                parent,
                "name",
                None
            ):
                break

            print(
                "HTML PARENT",
                level,
                "TAG:",
                parent.name,
                "CLASS:",
                parent.get(
                    "class",
                    []
                ),
                "ID:",
                parent.get(
                    "id",
                    ""
                ),
                "TEXT:",
                repr(
                    clean(
                        parent.get_text(
                            " ",
                            strip=True
                        )
                    )[:220]
                )
            )

            parent = parent.parent

    print()
    print(
        "========== END DEBUG",
        category.upper(),
        "=========="
    )


# ======================================
# 主程式
# ======================================

def main():
    print(
        "Gabu 2.1 diagnostic mode"
    )

    print(
        "SAFETY: Discord posting disabled"
    )

    print(
        "SAFETY: State writing disabled"
    )

    errors = []

    for category, url in SOURCES.items():
        print()
        print(
            "==========",
            category.upper(),
            "=========="
        )

        try:
            soup = fetch(url)

            if category == "event":
                items = parse_events(
                    soup
                )

                print(
                    "event: parsed",
                    len(items),
                    "entries"
                )

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

            else:
                debug_page(
                    soup,
                    category
                )

        except Exception as exc:
            print(
                "ERROR:",
                category,
                type(exc).__name__,
                str(exc)
            )

            errors.append(
                category
            )

    print()
    print(
        "Diagnostic completed"
    )

    if errors:
        print(
            "Failed categories:",
            ", ".join(errors)
        )

        raise SystemExit(1)


if __name__ == "__main__":
    main()
