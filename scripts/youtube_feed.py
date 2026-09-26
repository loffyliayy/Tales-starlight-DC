"""RSS notifications with durable, at-most-once delivery reservations."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

CHANNEL_ID = "UCRG7LdWB_-_fP7VF7nCPjcg"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
STATE_FILE = Path("youtube_state.json")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp requires a timezone")
    return result.astimezone(timezone.utc)


def parse_feed(data):
    root = ET.fromstring(data)
    if root.findtext("yt:channelId", namespaces=NS) != CHANNEL_ID:
        raise ValueError("Unexpected RSS channel")
    videos = {}
    for entry in root.findall("a:entry", NS):
        video_id = entry.findtext("yt:videoId", "", NS)
        published = entry.findtext("a:published", "", NS)
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Invalid video ID")
        timestamp(published)  # Fail closed; never infer freshness from list order.
        videos[video_id] = {"id": video_id, "published": published,
                            "title": entry.findtext("a:title", "HKfuntown 新影片", NS)}
    if not videos:
        raise ValueError("RSS contains no videos")
    return sorted(videos.values(), key=lambda v: (timestamp(v["published"]), v["id"]))


def load_state(path=STATE_FILE):
    # Missing/corrupt state is an error, never permission to replay the feed.
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != 1 or not isinstance(state.get("videos"), dict):
        raise ValueError("Invalid YouTube state")
    timestamp(state["not_before"])
    return state


def candidates(videos, state, now):
    cutoff = timestamp(state["not_before"])
    return [v for v in videos if v["id"] not in state["videos"]
            and cutoff < timestamp(v["published"]) <= now]


def payload(video):
    # A bare URL allows Discord's native YouTube embed/player.
    return {"username": "加布", "content":
            f"🎬 **HKfuntown 發布新影片！**\n**{video['title']}**\n\n"
            f"https://www.youtube.com/watch?v={video['id']}",
            "allowed_mentions": {"parse": []}}


def persist(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    subprocess.run(["git", "add", str(STATE_FILE)], check=True)
    subprocess.run(["git", "commit", "-m", "Reserve HKfuntown video notifications"], check=True)
    # No rebase/retry on conflict: do not deliver unless this reservation is durable.
    subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)


def send(video):
    request = urllib.request.Request(os.environ["DISCORD_WEBHOOK_URL"],
        data=json.dumps(payload(video), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "GabuYouTubeFeed/3"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in (200, 204):
                raise RuntimeError("Unexpected Discord response")
    except Exception:
        # Never expose the webhook URL and never retry an ambiguous POST.
        raise RuntimeError("Discord delivery failed or is uncertain; reservation retained. Inspect before manual recovery.") from None


def process(videos, state, now, dry_run=True, save=persist, deliver=send):
    pending = candidates(videos, state, now)
    print(f"RSS videos: {len(videos)}; eligible new videos: {len(pending)}")
    for video in pending:
        if dry_run:
            print("DRY RUN:", json.dumps(payload(video), ensure_ascii=False))
            continue
        updated = copy.deepcopy(state)
        updated["videos"][video["id"]] = {"status": "reserved", "published": video["published"],
                                                "reserved_at": now.isoformat()}
        save(updated)  # Must succeed remotely before Discord is contacted.
        state = updated
        deliver(video)
        print("Discord accepted:", video["id"])
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Reserve in Git before delivering to Discord")
    args = parser.parse_args()
    state = load_state()
    if args.send and not os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")
    request = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            videos = parse_feed(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError):
        raise RuntimeError("YouTube RSS unavailable or invalid; no notification or state change. Retry next run.") from None
    process(videos, state, datetime.now(timezone.utc), dry_run=not args.send)


if __name__ == "__main__":
    main()
