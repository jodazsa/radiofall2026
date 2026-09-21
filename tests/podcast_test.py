#!/usr/bin/env python3

import unittest
import urllib.error
from unittest.mock import patch

import radio
import sync_stations


class FakeResponse:
    """Minimal streaming response for mocked urlopen()."""

    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.bytes_read = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size=-1):
        if self.offset >= len(self.data):
            return b""

        if size is None or size < 0:
            end = len(self.data)
        else:
            end = min(
                self.offset + size,
                len(self.data),
            )

        chunk = self.data[self.offset:end]
        self.offset = end
        self.bytes_read += len(chunk)

        return chunk


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

    def test_first_playable_item_wins(self):
        data = rss_bytes(
            """
            <item>
              <title>First In Feed</title>
              <pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/first.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Newer Date But Second</title>
              <pubDate>Wed, 03 Sep 2026 10:00:00 GMT</pubDate>
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

    def test_unplayable_first_item_is_skipped(self):
        data = rss_bytes(
            """
            <item>
              <title>Broken First Item</title>
              <enclosure
                  url="file:///tmp/not-playable.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Playable Second Item</title>
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

    def test_pubdate_is_not_required(self):
        data = rss_bytes(
            """
            <item>
              <title>No Date Needed</title>
              <enclosure
                  url="https://example.com/current.mp3"
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
            "https://example.com/current.mp3",
        )

    def test_resolver_stops_reading_after_first_playable_item(self):
        filler = "x" * (radio.PODCAST_READ_CHUNK * 3)

        data = rss_bytes(
            f"""
            <item>
              <title>First Episode</title>
              <enclosure
                  url="https://example.com/first.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Large Historical Item</title>
              <description>{filler}</description>
              <enclosure
                  url="https://example.com/old.mp3"
                  type="audio/mpeg" />
            </item>
            """
        )

        response = FakeResponse(data)

        with patch(
            "radio.urllib.request.urlopen",
            return_value=response,
        ):
            result = radio.resolve_latest_podcast_episode(
                "https://example.com/feed.xml"
            )

        self.assertEqual(
            result,
            "https://example.com/first.mp3",
        )

        self.assertLess(
            response.bytes_read,
            len(data),
        )

        self.assertLessEqual(
            response.bytes_read,
            radio.PODCAST_READ_CHUNK,
        )

    def test_invalid_enclosure_url_is_rejected(self):
        data = rss_bytes(
            """
            <item>
              <title>Bad URL</title>
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