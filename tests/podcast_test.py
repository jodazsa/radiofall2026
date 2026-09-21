#!/usr/bin/env python3

import unittest
import urllib.error
from unittest.mock import patch

import radio
import sync_stations


class FakeResponse:
    """Minimal context-manager response for mocked urlopen()."""

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, _size=-1):
        return self.data


def rss_bytes(items):
    """Build a small RSS 2.0 document from supplied item XML."""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Podcast</title>
    {items}
  </channel>
</rss>
"""
    return xml.encode("utf-8")


class PodcastResolverTests(unittest.TestCase):

    def test_newest_pubdate_wins_even_when_feed_order_differs(self):
        data = rss_bytes(
            """
            <item>
              <title>Older First</title>
              <pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/older.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Newest Second</title>
              <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/newest.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Middle Third</title>
              <pubDate>Tue, 02 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/middle.mp3"
                  type="audio/mpeg" />
            </item>
            """
        )

        with patch(
            "radio.urllib.request.urlopen",
            return_value=FakeResponse(data),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertEqual(
            result,
            "https://example.com/newest.mp3",
        )

    def test_newest_unplayable_item_is_skipped(self):
        data = rss_bytes(
            """
            <item>
              <title>Newest But Broken</title>
              <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate>
            </item>

            <item>
              <title>Newest Playable</title>
              <pubDate>Tue, 02 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/playable.mp3"
                  type="audio/mpeg" />
            </item>
            """
        )

        with patch(
            "radio.urllib.request.urlopen",
            return_value=FakeResponse(data),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertEqual(
            result,
            "https://example.com/playable.mp3",
        )

    def test_missing_dates_fall_back_to_first_playable_item(self):
        data = rss_bytes(
            """
            <item>
              <title>First Playable</title>
              <enclosure
                  url="https://example.com/first.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Second Playable</title>
              <enclosure
                  url="https://example.com/second.mp3"
                  type="audio/mpeg" />
            </item>
            """
        )

        with patch(
            "radio.urllib.request.urlopen",
            return_value=FakeResponse(data),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertEqual(
            result,
            "https://example.com/first.mp3",
        )

    def test_invalid_enclosure_url_is_rejected(self):
        data = rss_bytes(
            """
            <item>
              <title>Bad URL</title>
              <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="file:///tmp/not-a-podcast.mp3"
                  type="audio/mpeg" />
            </item>
            """
        )

        with patch(
            "radio.urllib.request.urlopen",
            return_value=FakeResponse(data),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertIsNone(result)

    def test_invalid_xml_fails_cleanly(self):
        data = b"<rss><channel><item>"

        with patch(
            "radio.urllib.request.urlopen",
            return_value=FakeResponse(data),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertIsNone(result)

    def test_network_failure_fails_cleanly(self):
        with patch(
            "radio.urllib.request.urlopen",
            side_effect=urllib.error.URLError("test failure"),
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertIsNone(result)


class PodcastPlaybackTests(unittest.TestCase):

    def test_play_podcast_stops_old_audio_then_starts_episode(self):
        calls = []

        def fake_mpc(*args):
            calls.append(args)
            return ""

        with (
            patch(
                "radio.resolve_latest_podcast_episode",
                return_value="https://example.com/latest.mp3",
            ),
            patch(
                "radio.mpc",
                side_effect=fake_mpc,
            ),
            patch(
                "radio.play_stream",
            ) as play_stream,
            patch(
                "radio._wait_for_playing",
                return_value=True,
            ),
        ):
            result = radio.play_podcast(
                "https://example.com/feed.xml"
            )

        self.assertTrue(result)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0], ("stop",))

        play_stream.assert_called_once_with(
            "https://example.com/latest.mp3"
        )

    def test_failed_resolution_leaves_radio_stopped(self):
        calls = []

        def fake_mpc(*args):
            calls.append(args)
            return ""

        with (
            patch(
                "radio.resolve_latest_podcast_episode",
                return_value=None,
            ),
            patch(
                "radio.mpc",
                side_effect=fake_mpc,
            ),
            patch(
                "radio.play_stream",
            ) as play_stream,
        ):
            result = radio.play_podcast(
                "https://example.com/feed.xml"
            )

        self.assertFalse(result)
        self.assertEqual(calls, [("stop",)])
        play_stream.assert_not_called()


class StationSyncPodcastValidationTests(unittest.TestCase):

    def test_sync_validator_accepts_podcast(self):
        station = {
            "name": "Test Podcast",
            "type": "podcast",
            "url": "https://example.com/feed.xml",
        }

        sync_stations.validate_station(0, 0, station)

    def test_sync_validator_rejects_missing_podcast_url(self):
        station = {
            "name": "Test Podcast",
            "type": "podcast",
            "url": "",
        }

        with self.assertRaises(ValueError):
            sync_stations.validate_station(0, 0, station)

    def test_sync_validator_rejects_non_http_podcast_url(self):
        station = {
            "name": "Test Podcast",
            "type": "podcast",
            "url": "file:///tmp/feed.xml",
        }

        with self.assertRaises(ValueError):
            sync_stations.validate_station(0, 0, station)


if __name__ == "__main__":
    unittest.main(verbosity=2)