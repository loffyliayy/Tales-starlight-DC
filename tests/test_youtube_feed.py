import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import youtube_feed as bot

NOW = bot.timestamp("2026-09-26T12:00:00Z")


def video(vid="new_video01", date="2026-09-26T11:30:00Z"):
    return {"id": vid, "title": "Test @everyone", "published": date}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.state = {"version": 1, "not_before": "2026-09-26T11:25:15Z",
                      "videos": {"1n7mZp2Q_HI": {}, "wjVluunpPPw": {}}}

    def test_alternating_old_ids_never_send(self):
        for vid in ["1n7mZp2Q_HI", "wjVluunpPPw"] * 3:
            self.assertEqual(bot.candidates([video(vid)], self.state, NOW), [])

    def test_unseen_historical_future_and_equal_cutoff_skipped(self):
        for date in ["2026-09-20T12:00:00Z", "2026-09-26T13:00:00Z", self.state["not_before"]]:
            self.assertEqual(bot.candidates([video(date=date)], self.state, NOW), [])

    def test_new_video_once_and_reserve_before_send(self):
        events = []
        save = lambda s: events.append("save")
        send = lambda v: events.append("send")
        updated = bot.process([video()], self.state, NOW, False, save, send)
        bot.process([video()], updated, NOW, False, save, send)
        self.assertEqual(events, ["save", "send"])

    def test_push_failure_prevents_discord(self):
        send = Mock()
        with self.assertRaises(RuntimeError):
            bot.process([video()], self.state, NOW, False, Mock(side_effect=RuntimeError()), send)
        send.assert_not_called()

    def test_uncertain_post_retains_reservation(self):
        saved = []
        with self.assertRaises(RuntimeError):
            bot.process([video()], self.state, NOW, False,
                        lambda s: saved.append(copy.deepcopy(s)), Mock(side_effect=RuntimeError()))
        self.assertEqual(bot.candidates([video()], saved[0], NOW), [])

    def test_dry_run_has_no_side_effects(self):
        before = copy.deepcopy(self.state)
        save, send = Mock(), Mock()
        bot.process([video()], self.state, NOW, True, save, send)
        save.assert_not_called()
        send.assert_not_called()
        self.assertEqual(before, self.state)

    def test_plain_url_embed_and_no_mentions(self):
        payload = bot.payload(video())
        self.assertTrue(payload["content"].endswith("\n\nhttps://www.youtube.com/watch?v=new_video01"))
        self.assertNotIn("flags", payload)
        self.assertNotIn("embeds", payload)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_missing_or_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            with self.assertRaises(FileNotFoundError):
                bot.load_state(path)
            for contents in ["bad json", '{"version": 2}', '{"version": 1, "videos": {}, "not_before": "bad"}']:
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    bot.load_state(path)

    def test_feed_order_duplicates_and_timezone(self):
        def entry(vid, date):
            return f"<entry><yt:videoId>{vid}</yt:videoId><title>Test</title><published>{date}</published></entry>"
        xml = (f'<feed xmlns="{bot.NS["a"]}" xmlns:yt="{bot.NS["yt"]}">'
               f'<yt:channelId>{bot.CHANNEL_ID}</yt:channelId>'
               + entry("new_video02", "2026-09-26T19:45:00+08:00")
               + entry("new_video01", "2026-09-26T11:30:00Z") * 2 + '</feed>')
        self.assertEqual([v["id"] for v in bot.parse_feed(xml)], ["new_video01", "new_video02"])
        for invalid in [xml.replace(bot.CHANNEL_ID, "other"), xml.replace("2026-09-26T11:30:00Z", "bad")]:
            with self.assertRaises(ValueError):
                bot.parse_feed(invalid)

    def test_rss_outage_does_not_mutate_or_post(self):
        with patch.object(bot, "load_state", return_value=self.state), \
             patch("sys.argv", ["youtube_feed.py"]), \
             patch.object(bot.urllib.request, "urlopen", side_effect=bot.urllib.error.URLError("offline")), \
             patch.object(bot, "process") as process:
            with self.assertRaises(RuntimeError):
                bot.main()
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
