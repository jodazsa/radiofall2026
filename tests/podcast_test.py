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
              <guid>episode-first</guid>
              <pubDate>Mon, 01 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/first.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Newer Date But Second</title>
              <guid>episode-second</guid>
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
            {
                "episode_id": "episode-first",
                "title": "First In Feed",
                "audio_url": "https://example.com/first.mp3",
            },
        )

    def test_unplayable_first_item_is_skipped(self):
        data = rss_bytes(
            """
            <item>
              <title>Broken First Item</title>
              <guid>broken-first</guid>
              <enclosure
                  url="file:///tmp/not-playable.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Playable Second Item</title>
              <guid>playable-second</guid>
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
            {
                "episode_id": "playable-second",
                "title": "Playable Second Item",
                "audio_url": "https://example.com/playable.mp3",
            },
        )

    def test_pubdate_is_not_required(self):
        data = rss_bytes(
            """
            <item>
              <title>No Date Needed</title>
              <guid>no-date-episode</guid>
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
            {
                "episode_id": "no-date-episode",
                "title": "No Date Needed",
                "audio_url": "https://example.com/current.mp3",
            },
        )

    def test_episode_id_falls_back_when_guid_missing(self):
        data = rss_bytes(
            """
            <item>
              <title>Episode Without GUID</title>
              <pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate>
              <enclosure
                  url="https://example.com/fallback.mp3"
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
            result["episode_id"],
            "Episode Without GUID|Tue, 22 Sep 2026 10:00:00 GMT",
        )

        self.assertEqual(
            result["title"],
            "Episode Without GUID",
        )

        self.assertEqual(
            result["audio_url"],
            "https://example.com/fallback.mp3",
        )

    def test_episode_id_falls_back_to_audio_url_without_guid_or_date(self):
        data = rss_bytes(
            """
            <item>
              <title>Minimal Episode</title>
              <enclosure
                  url="https://example.com/minimal.mp3"
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
            result["episode_id"],
            "https://example.com/minimal.mp3",
        )

        self.assertEqual(
            result["title"],
            "Minimal Episode",
        )

        self.assertEqual(
            result["audio_url"],
            "https://example.com/minimal.mp3",
        )

    def test_resolver_stops_reading_after_first_playable_item(self):
        filler = "x" * (radio.PODCAST_READ_CHUNK * 3)

        data = rss_bytes(
            f"""
            <item>
              <title>First Episode</title>
              <guid>first-episode</guid>
              <enclosure
                  url="https://example.com/first.mp3"
                  type="audio/mpeg" />
            </item>

            <item>
              <title>Large Historical Item</title>
              <guid>old-episode</guid>
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
            {
                "episode_id": "first-episode",
                "title": "First Episode",
                "audio_url": "https://example.com/first.mp3",
            },
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
              <guid>bad-url</guid>
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

class PodcastStateValidationTests(unittest.TestCase):

    def test_old_state_without_podcasts_is_backward_compatible(self):
        result = radio._validate_state(
            {
                "volume": 25,
                "timestamp": 123456,
            }
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["volume"], 25)
        self.assertEqual(result["podcasts"], {})

    def test_multiple_podcast_resume_states_are_preserved(self):
        result = radio._validate_state(
            {
                "volume": 40,
                "podcasts": {
                    "https://example.com/feed-a.xml": {
                        "episode_id": "episode-a",
                        "position": 321,
                        "title": "Episode A",
                    },
                    "https://example.com/feed-b.xml": {
                        "episode_id": "episode-b",
                        "position": 654,
                        "title": "Episode B",
                    },
                },
            }
        )

        self.assertEqual(
            result["podcasts"],
            {
                "https://example.com/feed-a.xml": {
                    "episode_id": "episode-a",
                    "position": 321,
                    "title": "Episode A",
                },
                "https://example.com/feed-b.xml": {
                    "episode_id": "episode-b",
                    "position": 654,
                    "title": "Episode B",
                },
            },
        )

    def test_invalid_podcast_entry_is_ignored(self):
        result = radio._validate_state(
            {
                "volume": 25,
                "podcasts": {
                    "https://example.com/good.xml": {
                        "episode_id": "good-episode",
                        "position": 120,
                        "title": "Good Episode",
                    },
                    "https://example.com/bad.xml": {
                        "episode_id": "",
                        "position": -10,
                    },
                },
            }
        )

        self.assertEqual(
            result["podcasts"],
            {
                "https://example.com/good.xml": {
                    "episode_id": "good-episode",
                    "position": 120,
                    "title": "Good Episode",
                }
            },
        )

    def test_save_state_includes_all_podcast_resume_states(self):
        original = radio._podcast_resume_states

        radio._podcast_resume_states = {
            "https://example.com/feed.xml": {
                "episode_id": "episode-123",
                "position": 456,
                "title": "Test Episode",
            }
        }

        try:
            with patch(
                "radio._atomic_write_json"
            ) as atomic_write:
                radio.save_state(35)

            self.assertEqual(
                atomic_write.call_count,
                2,
            )

            payload = atomic_write.call_args_list[0].args[1]

            self.assertEqual(
                payload["volume"],
                35,
            )

            self.assertEqual(
                payload["podcasts"],
                {
                    "https://example.com/feed.xml": {
                        "episode_id": "episode-123",
                        "position": 456,
                        "title": "Test Episode",
                    }
                },
            )

        finally:
            radio._podcast_resume_states = original
            


class PodcastPlaybackTests(unittest.TestCase):

    def setUp(self):
        self.original_resume_states = (
            radio._podcast_resume_states
        )
        self.original_active_podcast = (
            radio._active_podcast
        )

        radio._podcast_resume_states = {}
        radio._active_podcast = None

    def tearDown(self):
        radio._podcast_resume_states = (
            self.original_resume_states
        )
        radio._active_podcast = (
            self.original_active_podcast
        )

    def test_new_podcast_episode_starts_from_beginning(self):
        feed_url = "https://example.com/feed.xml"

        episode = {
            "episode_id": "episode-1",
            "title": "Episode One",
            "audio_url": "https://example.com/episode-1.mp3",
        }

        calls = []

        def fake_mpc(*args):
            calls.append(args)
            return ""

        with (
            patch(
                "radio.resolve_latest_podcast_episode",
                return_value=episode,
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
            patch(
                "radio.save_state",
            ) as save_state,
        ):
            result = radio.play_podcast(
                feed_url,
                35,
            )

        self.assertTrue(result)

        self.assertEqual(
            calls[0],
            ("stop",),
        )

        play_stream.assert_called_once_with(
            "https://example.com/episode-1.mp3"
        )

        self.assertNotIn(
            ("seek", "0"),
            calls,
        )

        self.assertEqual(
            radio._active_podcast,
            {
                "feed_url": feed_url,
                "episode_id": "episode-1",
                "title": "Episode One",
                "audio_url": (
                    "https://example.com/episode-1.mp3"
                ),
            },
        )

        self.assertEqual(
            radio._podcast_resume_states[feed_url],
            {
                "episode_id": "episode-1",
                "position": 0,
                "title": "Episode One",
            },
        )

        save_state.assert_called_once_with(35)

    def test_same_episode_resumes_saved_position(self):
        feed_url = "https://example.com/feed.xml"

        radio._podcast_resume_states[feed_url] = {
            "episode_id": "episode-1",
            "position": 754,
            "title": "Episode One",
        }

        episode = {
            "episode_id": "episode-1",
            "title": "Episode One",
            "audio_url": "https://example.com/fresh-url.mp3",
        }

        calls = []

        def fake_mpc(*args):
            calls.append(args)
            return ""

        with (
            patch(
                "radio.resolve_latest_podcast_episode",
                return_value=episode,
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
            patch(
                "radio.save_state",
            ),
        ):
            result = radio.play_podcast(
                feed_url,
                40,
            )

        self.assertTrue(result)

        play_stream.assert_called_once_with(
            "https://example.com/fresh-url.mp3"
        )

        self.assertIn(
            ("seek", "754"),
            calls,
        )

        self.assertEqual(
            radio._podcast_resume_states[feed_url],
            {
                "episode_id": "episode-1",
                "position": 0,
                "title": "Episode One",
            },
        )

    def test_newer_episode_discards_old_resume_position(self):
        feed_url = "https://example.com/feed.xml"

        radio._podcast_resume_states[feed_url] = {
            "episode_id": "old-episode",
            "position": 900,
            "title": "Old Episode",
        }

        episode = {
            "episode_id": "new-episode",
            "title": "New Episode",
            "audio_url": "https://example.com/new.mp3",
        }

        calls = []

        def fake_mpc(*args):
            calls.append(args)
            return ""

        with (
            patch(
                "radio.resolve_latest_podcast_episode",
                return_value=episode,
            ),
            patch(
                "radio.mpc",
                side_effect=fake_mpc,
            ),
            patch(
                "radio.play_stream",
            ),
            patch(
                "radio._wait_for_playing",
                return_value=True,
            ),
            patch(
                "radio.save_state",
            ),
        ):
            result = radio.play_podcast(
                feed_url,
                30,
            )

        self.assertTrue(result)

        self.assertNotIn(
            ("seek", "900"),
            calls,
        )

        self.assertEqual(
            radio._podcast_resume_states[feed_url],
            {
                "episode_id": "new-episode",
                "position": 0,
                "title": "New Episode",
            },
        )

    def test_pause_active_podcast_saves_position(self):
        feed_url = "https://example.com/feed.xml"

        radio._active_podcast = {
            "feed_url": feed_url,
            "episode_id": "episode-1",
            "title": "Episode One",
            "audio_url": "https://example.com/episode.mp3",
        }

        radio._podcast_resume_states[
            "https://example.com/other.xml"
        ] = {
            "episode_id": "other-episode",
            "position": 222,
            "title": "Other Episode",
        }

        calls = []

        def fake_mpc(*args):
            calls.append(args)

            if args == ("status",):
                return (
                    "Episode One\n"
                    "[playing] #1/1   "
                    "12:34/25:00 (50%)\n"
                    "volume: 35%"
                )

            return ""

        with (
            patch(
                "radio.mpc",
                side_effect=fake_mpc,
            ),
            patch(
                "radio.save_state",
            ) as save_state,
        ):
            result = radio.pause_active_podcast(35)

        self.assertTrue(result)

        self.assertEqual(
            radio._podcast_resume_states[feed_url],
            {
                "episode_id": "episode-1",
                "position": 754,
                "title": "Episode One",
            },
        )

        # A second podcast's saved state must survive untouched.
        self.assertEqual(
            radio._podcast_resume_states[
                "https://example.com/other.xml"
            ]["position"],
            222,
        )

        self.assertIn(
            ("stop",),
            calls,
        )

        self.assertIsNone(
            radio._active_podcast
        )

        save_state.assert_called_once_with(35)

    def test_natural_end_does_not_create_resume_position(self):
        feed_url = "https://example.com/feed.xml"

        radio._active_podcast = {
            "feed_url": feed_url,
            "episode_id": "episode-1",
            "title": "Episode One",
            "audio_url": "https://example.com/episode.mp3",
        }

        radio._podcast_resume_states[feed_url] = {
            "episode_id": "episode-1",
            "position": 0,
            "title": "Episode One",
        }

        def fake_mpc(*args):
            if args == ("status",):
                return "volume: 35%"

            return ""

        with (
            patch(
                "radio.mpc",
                side_effect=fake_mpc,
            ),
            patch(
                "radio.save_state",
            ) as save_state,
        ):
            result = radio.pause_active_podcast(35)

        self.assertTrue(result)

        self.assertEqual(
            radio._podcast_resume_states[feed_url][
                "position"
            ],
            0,
        )

        save_state.assert_not_called()

        self.assertIsNone(
            radio._active_podcast
        )

    def test_pause_when_no_podcast_is_active_does_nothing(self):
        radio._active_podcast = None

        with (
            patch(
                "radio.mpc",
            ) as mpc,
            patch(
                "radio.save_state",
            ) as save_state,
        ):
            result = radio.pause_active_podcast(25)

        self.assertFalse(result)
        mpc.assert_not_called()
        save_state.assert_not_called()

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
                "https://example.com/feed.xml",
                25,
            )

        self.assertFalse(result)

        self.assertEqual(
            calls,
            [("stop",)],
        )

        play_stream.assert_not_called()

        self.assertIsNone(
            radio._active_podcast
        )

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
