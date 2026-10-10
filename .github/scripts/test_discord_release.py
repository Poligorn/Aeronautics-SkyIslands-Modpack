#!/usr/bin/env python3
"""Unit-тесты для discord_release.py (без сети и без Discord)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_release as dr  # noqa: E402


SAMPLE_V030 = {
    "tag_name": "v0.3.0",
    "name": "v.0.3.0 - Adventures, Social Interactions, and Decor",
    "html_url": "https://github.com/Poligorn/Aeronautics-SkyIslands-Modpack/releases/tag/v0.3.0",
    "draft": False,
    "prerelease": False,
    "published_at": "2026-05-24T20:19:01Z",
    "body": (
        '<img width="2752" height="1536" alt="banner" '
        'src="https://github.com/user-attachments/assets/79978b7d-38c5-4d6c-ad62-9402ba460645" />\n\n'
        "# Build v0.3.0 - Adventures, Social Interactions, and Decor\n\n"
        "____\n\n"
        "A major update focusing on decor.\n\n"
        "## This update adds:\n\n"
        "• More Create creations.\n\n"
        "See [Create](https://modrinth.com/mod/create) for details.\n"
    ),
    "assets": [
        {
            "id": 1,
            "name": "Aeronautics-Sky-Island-v0.3.0.zip",
            "content_type": "application/zip",
            "size": 264877017,
            "browser_download_url": (
                "https://github.com/Poligorn/Aeronautics-SkyIslands-Modpack/"
                "releases/download/v0.3.0/Aeronautics-Sky-Island-v0.3.0.zip"
            ),
        }
    ],
}

SAMPLE_V040 = {
    "tag_name": "v.0.4.0",
    "name": "v.0.4.0",
    "html_url": "https://github.com/Poligorn/Aeronautics-SkyIslands-Modpack/releases/tag/v.0.4.0",
    "draft": False,
    "prerelease": False,
    "published_at": "2026-06-10T11:27:36Z",
    "body": (
        "![shot](https://github.com/user-attachments/assets/66ba78f4-e430-4639-9e5f-2a16a5e0841e)\n\n"
        "# An update with teleports\n\n"
        "• Waystones added.\n"
    ),
    "assets": [
        {
            "id": 2,
            "name": "Aeronautics.Sky.Islands.0.4.0.mrpack",
            "content_type": "application/octet-stream",
            "size": 282898,
            "browser_download_url": (
                "https://github.com/Poligorn/Aeronautics-SkyIslands-Modpack/"
                "releases/download/v.0.4.0/Aeronautics.Sky.Islands.0.4.0.mrpack"
            ),
        },
        {
            "id": 3,
            "name": "screenshot.png",
            "content_type": "image/png",
            "size": 12000,
            "browser_download_url": (
                "https://github.com/Poligorn/Aeronautics-SkyIslands-Modpack/"
                "releases/download/v.0.4.0/screenshot.png"
            ),
        },
    ],
}


class MarkdownTests(unittest.TestCase):
    def test_extracts_html_and_markdown_images(self) -> None:
        body = (
            '<img src="https://example.com/a.png" alt="a" />\n'
            "hello\n"
            "![b](https://example.com/b.jpg)\n"
            '<img width="10" src="https://example.com/a.png">'
        )
        text, urls = dr.extract_and_convert(body)
        self.assertEqual(
            urls,
            ["https://example.com/a.png", "https://example.com/b.jpg"],
        )
        self.assertIn("hello", text)
        self.assertNotIn("<img", text)
        self.assertNotIn("![", text)

    def test_headings_lists_links_and_hr(self) -> None:
        raw = "# Title\n\n____\n\n## Section\n\n- item\n\n[link](https://ex.test)\n"
        converted = dr.markdown_to_discord(raw)
        self.assertIn("**Title**", converted)
        self.assertIn("**Section**", converted)
        self.assertIn("────────", converted)
        self.assertIn("- item", converted)
        self.assertIn("[link](https://ex.test)", converted)
        self.assertNotIn("# Title", converted)


class LimitTests(unittest.TestCase):
    def test_truncate_keeps_link_and_limit(self) -> None:
        url = "https://github.com/ex/repo/releases/tag/v1"
        text = "word " * 2000
        out = dr.truncate_with_link(text, 200, url)
        self.assertLessEqual(len(out), 200)
        self.assertIn(dr.MORE_LABEL, out)
        self.assertIn(url, out)
        self.assertTrue(out.startswith("word"))

    def test_split_text_oldest_chunks(self) -> None:
        text = "alpha\n\n" + ("b" * 80) + "\n\nomega"
        chunks = dr.split_text(text, 20, 50)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("alpha"))
        self.assertEqual("".join(c.replace("\n", "") for c in chunks).count("b"), 80)


class ImagePolicyTests(unittest.TestCase):
    def test_public_user_attachment_uses_embed_url(self) -> None:
        upload, reason = dr.should_upload_image(
            "https://github.com/user-attachments/assets/abc", False
        )
        self.assertFalse(upload)
        self.assertEqual(reason, "public-user-attachment")

    def test_private_user_attachment_uploads(self) -> None:
        upload, reason = dr.should_upload_image(
            "https://github.com/user-attachments/assets/abc", True
        )
        self.assertTrue(upload)
        self.assertEqual(reason, "private-repo-github-url")

    def test_signed_url_uploads(self) -> None:
        upload, reason = dr.should_upload_image(
            "https://github-production-user-asset-6210df.s3.amazonaws.com/x.jpg?X-Amz-Signature=abc",
            False,
        )
        self.assertTrue(upload)
        self.assertEqual(reason, "expiring-signed-url")


class PlanTests(unittest.TestCase):
    def test_v030_public_plan(self) -> None:
        plan = dr.build_release_plan(SAMPLE_V030, repo_private=False)
        self.assertIsNone(plan.skipped)
        self.assertEqual(len(plan.messages), 1)
        msg = plan.messages[0]
        embed = msg.embeds[0]
        self.assertEqual(embed["title"], SAMPLE_V030["name"])
        self.assertEqual(embed["url"], SAMPLE_V030["html_url"])
        self.assertEqual(embed["timestamp"], SAMPLE_V030["published_at"])
        self.assertIn("**Build v0.3.0", embed["description"])
        self.assertIn("────────", embed["description"])
        self.assertIn("[Create](https://modrinth.com/mod/create)", embed["description"])
        self.assertNotIn("<img", embed["description"])
        self.assertTrue(embed["image"]["url"].startswith("https://github.com/user-attachments/"))
        self.assertEqual(msg.uploads, [])
        self.assertEqual(len(plan.files), 1)
        self.assertIn(".zip", embed["fields"][0]["value"])
        self.assertLessEqual(len(embed["description"]), dr.DISCORD_EMBED_DESC_LIMIT)
        self.assertLessEqual(msg.total_embed_chars(), dr.DISCORD_TOTAL_CHARS)
        self.assertLessEqual(len(msg.embeds), dr.DISCORD_EMBEDS_PER_MESSAGE)
        self.assertIn("v0.3.0", msg.content)

    def test_v040_asset_image_and_mrpack(self) -> None:
        plan = dr.build_release_plan(SAMPLE_V040, repo_private=False)
        sources = {img.source for img in plan.images}
        self.assertEqual(sources, {"body", "asset"})
        self.assertEqual(len(plan.files), 1)
        self.assertTrue(plan.files[0].name.endswith(".mrpack"))
        image_urls = [e["image"]["url"] for e in plan.messages[0].embeds if "image" in e]
        self.assertEqual(len(image_urls), 2)
        self.assertTrue(any("user-attachments" in u for u in image_urls))
        self.assertTrue(any(u.endswith("screenshot.png") for u in image_urls))

    def test_private_repo_marks_uploads(self) -> None:
        plan = dr.build_release_plan(SAMPLE_V030, repo_private=True)
        self.assertTrue(plan.images[0].upload)
        self.assertEqual(
            plan.messages[0].embeds[0]["image"]["url"],
            f"attachment://{plan.images[0].filename}",
        )
        self.assertEqual(len(plan.messages[0].uploads), 1)

    def test_draft_skipped(self) -> None:
        release = dict(SAMPLE_V030, draft=True)
        plan = dr.build_release_plan(release)
        self.assertEqual(plan.skipped, "draft")
        self.assertEqual(plan.messages, [])

    def test_prerelease_suffix(self) -> None:
        release = dict(SAMPLE_V030, prerelease=True)
        plan = dr.build_release_plan(release)
        self.assertTrue(plan.messages[0].embeds[0]["title"].endswith("(pre-release)"))

    def test_long_notes_split_or_truncate(self) -> None:
        body = "# Huge\n\n" + ("paragraph\n\n" * 800)
        release = dict(SAMPLE_V030, body=body, assets=[])
        plan = dr.build_release_plan(release, repo_private=False)
        for msg in plan.messages:
            self.assertLessEqual(len(msg.embeds), dr.DISCORD_EMBEDS_PER_MESSAGE)
            self.assertLessEqual(msg.total_embed_chars(), dr.DISCORD_TOTAL_CHARS)
            for embed in msg.embeds:
                self.assertLessEqual(len(embed.get("description") or ""), dr.DISCORD_EMBED_DESC_LIMIT)
        joined = "\n".join(e.get("description") or "" for m in plan.messages for e in m.embeds)
        self.assertIn(dr.MORE_LABEL, joined)

    def test_many_images_use_followup_messages(self) -> None:
        images_md = "\n".join(f"![n](https://cdn.example/img{i}.png)" for i in range(15))
        release = {
            "tag_name": "v9",
            "name": "Many pics",
            "html_url": "https://example.test/r",
            "published_at": "2026-01-01T00:00:00Z",
            "body": images_md,
            "assets": [],
        }
        plan = dr.build_release_plan(release)
        self.assertGreaterEqual(len(plan.messages), 2)
        total_embed_images = sum(
            1 for m in plan.messages for e in m.embeds if e.get("image")
        )
        self.assertEqual(total_embed_images, 15)
        for msg in plan.messages:
            self.assertLessEqual(len(msg.embeds), dr.DISCORD_EMBEDS_PER_MESSAGE)

    def test_event_loader_accepts_github_event_and_raw_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            raw_path = Path(tmp) / "release.json"
            event_path.write_text(json.dumps({"release": SAMPLE_V030}), encoding="utf-8")
            raw_path.write_text(json.dumps(SAMPLE_V030), encoding="utf-8")
            self.assertEqual(dr.load_event_release(str(event_path))["tag_name"], "v0.3.0")
            self.assertEqual(dr.load_event_release(str(raw_path))["tag_name"], "v0.3.0")


if __name__ == "__main__":
    unittest.main()
