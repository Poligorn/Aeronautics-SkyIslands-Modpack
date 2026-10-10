#!/usr/bin/env python3
"""Публикация GitHub-релизов в Discord через channel webhook.

Только стандартная библиотека. См. docs/discord-announcements.md.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

DISCORD_EMBED_DESC_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_EMBEDS_PER_MESSAGE = 10
DISCORD_TOTAL_CHARS = 6000
DISCORD_WEBHOOK_FILE_LIMIT = 8 * 1024 * 1024
DISCORD_MAX_FILES = 10
DISCORD_COLOR = 0x5B9BD5
USER_AGENT = "Aeronautics-SkyIslands-DiscordRelease/1.0"
MORE_LABEL = "полный текст релиза"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/bmp",
}

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_IMG_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)[\"'][^>]*/?>",
    re.IGNORECASE | re.DOTALL,
)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
HR_RE = re.compile(r"^\s*(?:[-=_*]){3,}\s*$", re.MULTILINE)
EXTRA_BLANK_RE = re.compile(r"\n{3,}")


class GithubHttpError(RuntimeError):
    pass


@dataclass
class ImageRef:
    url: str
    filename: str
    source: str
    upload: bool
    asset_id: int | None = None
    content_type: str | None = None
    reason: str = ""


@dataclass
class FileLink:
    name: str
    url: str
    size: int | None = None


@dataclass
class MessagePlan:
    content: str
    embeds: list[dict[str, Any]]
    uploads: list[ImageRef] = field(default_factory=list)
    leftover_images: list[ImageRef] = field(default_factory=list)

    def total_embed_chars(self) -> int:
        return sum(embed_char_count(e) for e in self.embeds)


@dataclass
class ReleasePlan:
    tag: str
    name: str
    html_url: str
    published_at: str | None
    messages: list[MessagePlan]
    files: list[FileLink]
    images: list[ImageRef]
    skipped: str | None = None


def embed_char_count(embed: dict[str, Any]) -> int:
    total = 0
    total += len(embed.get("title") or "")
    total += len(embed.get("description") or "")
    footer = embed.get("footer") or {}
    total += len(footer.get("text") or "")
    author = embed.get("author") or {}
    total += len(author.get("name") or "")
    for item in embed.get("fields") or []:
        total += len(item.get("name") or "") + len(item.get("value") or "")
    return total


def is_image_name(name: str, content_type: str | None = None) -> bool:
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        if ctype in IMAGE_CONTENT_TYPES:
            return True
    ext = os.path.splitext(name)[1].lower()
    return ext in IMAGE_EXTENSIONS


def safe_filename(name: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", name, flags=re.ASCII).strip("._")
    return cleaned or fallback


def filename_from_url(url: str, index: int, content_type: str | None = None) -> str:
    path = urlparse(url).path
    base = os.path.basename(path) or f"image-{index}"
    if "." not in base and content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ""
        if ext == ".jpe":
            ext = ".jpg"
        base = f"{base}{ext}"
    if "." not in os.path.splitext(base)[1]:
        # user-attachments IDs have no extension
        if content_type:
            ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            base = f"{base}{ext}"
        else:
            base = f"{base}.jpg"
    return safe_filename(base, f"image-{index}.jpg")


def should_upload_image(url: str, repo_private: bool) -> tuple[bool, str]:
    """Decide whether Discord can fetch the image URL itself."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path.lower()
    query = parsed.query.lower()

    githubish = any(
        token in host
        for token in (
            "github.com",
            "githubusercontent.com",
            "github-production-user-asset",
            "amazonaws.com",
        )
    )
    user_attachment = "user-attachments" in path or "private-user-images" in host
    signed = "x-amz-signature" in query or "token=" in query

    if repo_private and githubish:
        return True, "private-repo-github-url"
    if user_attachment:
        # Публичный репозиторий: Discordbot GET проходит по user-attachments.
        # Приватный уже отсечён выше. Оставляем URL в эмбеде.
        return False, "public-user-attachment"
    if signed:
        return True, "expiring-signed-url"
    if repo_private and host.endswith("githubusercontent.com"):
        return True, "private-githubusercontent"
    return False, "public-url"


def extract_body_images(body: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def _html(match: re.Match[str]) -> str:
        urls.append(match.group(1).strip())
        return ""

    def _md(match: re.Match[str]) -> str:
        urls.append(match.group(2).strip())
        return ""

    text = HTML_IMG_RE.sub(_html, body)
    text = MD_IMAGE_RE.sub(_md, text)
    # unique, keep order
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            unique.append(url)
    return text, unique


def markdown_to_discord(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = HTML_COMMENT_RE.sub("", text)
    text = HEADING_RE.sub(lambda m: f"**{m.group(2).strip()}**", text)
    text = HR_RE.sub("────────", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&nbsp;", " ")
    text = EXTRA_BLANK_RE.sub("\n\n", text)
    return text.strip()


def extract_and_convert(body: str | None) -> tuple[str, list[str]]:
    raw = body or ""
    without_images, image_urls = extract_body_images(raw)
    return markdown_to_discord(without_images), image_urls


def format_size(size: int | None) -> str:
    if not size or size < 0:
        return ""
    units = ["Б", "КБ", "МБ", "ГБ"]
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} {units[unit]}"
    return f"{value:.1f} {units[unit]}"


def human_date(iso_value: str | None) -> str:
    if not iso_value:
        return ""
    try:
        dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_value


def truncate_with_link(text: str, limit: int, html_url: str) -> str:
    suffix = f"\n\n[{MORE_LABEL}]({html_url})"
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(suffix) - 1)
    cut = text[:budget]
    for sep in ("\n", " "):
        if sep in cut:
            maybe = cut.rsplit(sep, 1)[0]
            if maybe:
                cut = maybe
                break
    return cut.rstrip() + "…" + suffix


def split_text(text: str, first_limit: int, next_limit: int) -> list[str]:
    if not text:
        return [""]
    if len(text) <= first_limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    limit = first_limit
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining[:limit]
        split_at = cut.rfind("\n")
        if split_at < limit // 3:
            split_at = cut.rfind(" ")
        if split_at < limit // 3:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
        limit = next_limit
    return chunks or [""]


def classify_assets(
    assets: list[dict[str, Any]], repo_private: bool
) -> tuple[list[ImageRef], list[FileLink]]:
    images: list[ImageRef] = []
    files: list[FileLink] = []
    for asset in assets:
        name = asset.get("name") or "asset"
        url = asset.get("browser_download_url") or ""
        content_type = asset.get("content_type")
        asset_id = asset.get("id")
        size = asset.get("size")
        if is_image_name(name, content_type):
            upload, reason = should_upload_image(url, repo_private)
            if repo_private:
                upload, reason = True, "private-release-asset"
            images.append(
                ImageRef(
                    url=url,
                    filename=safe_filename(name, f"asset-{asset_id or len(images)}.png"),
                    source="asset",
                    upload=upload,
                    asset_id=asset_id,
                    content_type=content_type,
                    reason=reason,
                )
            )
        elif url:
            files.append(FileLink(name=name, url=url, size=size))
    return images, files


def collect_images(
    body_urls: list[str], asset_images: list[ImageRef], repo_private: bool
) -> list[ImageRef]:
    images: list[ImageRef] = []
    seen: set[str] = set()
    for index, url in enumerate(body_urls, start=1):
        if url in seen:
            continue
        seen.add(url)
        upload, reason = should_upload_image(url, repo_private)
        images.append(
            ImageRef(
                url=url,
                filename=filename_from_url(url, index),
                source="body",
                upload=upload,
                reason=reason,
            )
        )
    for image in asset_images:
        if image.url in seen:
            continue
        seen.add(image.url)
        images.append(image)
    return images


def file_field_value(files: list[FileLink]) -> str:
    lines = []
    for item in files:
        size = format_size(item.size)
        label = f"{item.name} ({size})" if size else item.name
        lines.append(f"[{label}]({item.url})")
    text = "\n".join(lines)
    if len(text) <= DISCORD_EMBED_FIELD_VALUE_LIMIT:
        return text
    kept: list[str] = []
    used = 0
    more = "\n…"
    for line in lines:
        extra = (1 if kept else 0) + len(line)
        if used + extra + len(more) > DISCORD_EMBED_FIELD_VALUE_LIMIT:
            break
        kept.append(line)
        used += extra
    return "\n".join(kept) + more


def build_release_plan(
    release: dict[str, Any],
    *,
    repo_private: bool = False,
) -> ReleasePlan:
    tag = release.get("tag_name") or "unknown"
    name = (release.get("name") or tag).strip() or tag
    html_url = release.get("html_url") or ""
    published_at = release.get("published_at") or release.get("created_at")

    if release.get("draft"):
        return ReleasePlan(
            tag=tag,
            name=name,
            html_url=html_url,
            published_at=published_at,
            messages=[],
            files=[],
            images=[],
            skipped="draft",
        )

    body_text, body_images = extract_and_convert(release.get("body"))
    asset_images, files = classify_assets(release.get("assets") or [], repo_private)
    images = collect_images(body_images, asset_images, repo_private)

    title = name
    if release.get("prerelease"):
        title = f"{title} (pre-release)"
    title = title[:DISCORD_EMBED_TITLE_LIMIT]

    date_label = human_date(published_at)
    footer_parts = [tag]
    if date_label:
        footer_parts.append(date_label)
    footer_text = " · ".join(footer_parts)

    # Reserve room in the first embed for title/footer/fields.
    reserved = len(title) + len(footer_text) + 80
    if files:
        reserved += len("Файлы") + min(len(file_field_value(files)), DISCORD_EMBED_FIELD_VALUE_LIMIT)
    first_desc_limit = max(500, min(DISCORD_EMBED_DESC_LIMIT, DISCORD_TOTAL_CHARS - reserved))

    more = f"\n\n[{MORE_LABEL}]({html_url})" if html_url else ""
    if body_text and len(body_text) > first_desc_limit:
        chunks = split_text(body_text, first_desc_limit - len(more) - 1, DISCORD_EMBED_DESC_LIMIT)
        if chunks:
            chunks[-1] = truncate_with_link(chunks[-1], DISCORD_EMBED_DESC_LIMIT, html_url)
    else:
        chunks = [body_text]

    # One extra embed for each leftover text chunk after the first.
    text_extra = max(0, len(chunks) - 1)
    image_slots_first = max(0, DISCORD_EMBEDS_PER_MESSAGE - 1 - text_extra)
    first_images = images[:image_slots_first]
    remaining_images = images[image_slots_first:]

    first_embed: dict[str, Any] = {
        "title": title,
        "description": chunks[0] if chunks else "",
        "color": DISCORD_COLOR,
        "footer": {"text": footer_text},
    }
    if html_url:
        first_embed["url"] = html_url
    if published_at:
        first_embed["timestamp"] = published_at
    if files:
        first_embed["fields"] = [
            {"name": "Файлы", "value": file_field_value(files), "inline": False}
        ]

    first_uploads: list[ImageRef] = []
    leftover: list[ImageRef] = []
    if first_images:
        _img, extra_uploads, extra_leftover = _apply_image(first_images[0], first_embed)
        first_uploads.extend(extra_uploads)
        leftover.extend(extra_leftover)
        extra_embeds: list[dict[str, Any]] = []
        for image in first_images[1:]:
            embed = {"color": DISCORD_COLOR}
            _img, uploads, left = _apply_image(image, embed)
            extra_embeds.append(embed)
            first_uploads.extend(uploads)
            leftover.extend(left)
        extra_text_embeds = []
        for chunk in chunks[1:]:
            extra_text_embeds.append({"description": chunk, "color": DISCORD_COLOR})
        first_embeds = [first_embed, *extra_text_embeds, *extra_embeds]
    else:
        extra_text_embeds = [{"description": chunk, "color": DISCORD_COLOR} for chunk in chunks[1:]]
        first_embeds = [first_embed, *extra_text_embeds]

    # Enforce 6000-char total on the first message: shrink description if needed.
    _fit_message_embeds(first_embeds, html_url)

    content = f"🚀 **Новый релиз: {name}**"
    if tag and tag != name:
        content += f" `{tag}`"
    if html_url:
        content += f"\n{html_url}"

    messages = [
        MessagePlan(content=content, embeds=first_embeds, uploads=first_uploads, leftover_images=leftover)
    ]

    # Remaining images as follow-up messages (image-only embeds).
    queue = remaining_images
    while queue:
        batch = queue[: min(DISCORD_EMBEDS_PER_MESSAGE, DISCORD_MAX_FILES)]
        queue = queue[len(batch) :]
        embeds: list[dict[str, Any]] = []
        uploads: list[ImageRef] = []
        extra_left: list[ImageRef] = []
        for image in batch:
            embed = {"color": DISCORD_COLOR}
            _img, up, left = _apply_image(image, embed)
            embeds.append(embed)
            uploads.extend(up)
            extra_left.extend(left)
        messages.append(
            MessagePlan(
                content="",
                embeds=embeds,
                uploads=uploads,
                leftover_images=extra_left,
            )
        )

    # If leftover images could not be attached, mention them on the first embed.
    all_leftover = [img for msg in messages for img in msg.leftover_images]
    if all_leftover and messages:
        links = "\n".join(f"[изображение]({img.url})" for img in all_leftover if img.url)
        if links:
            desc = messages[0].embeds[0].get("description") or ""
            addition = "\n\n**Ещё изображения:**\n" + links
            messages[0].embeds[0]["description"] = truncate_with_link(
                desc + addition, DISCORD_EMBED_DESC_LIMIT, html_url
            )
            _fit_message_embeds(messages[0].embeds, html_url)

    return ReleasePlan(
        tag=tag,
        name=name,
        html_url=html_url,
        published_at=published_at,
        messages=messages,
        files=files,
        images=images,
    )


def _apply_image(
    image: ImageRef, embed: dict[str, Any]
) -> tuple[ImageRef, list[ImageRef], list[ImageRef]]:
    if image.upload:
        embed["image"] = {"url": f"attachment://{image.filename}"}
        return image, [image], []
    embed["image"] = {"url": image.url}
    return image, [], []


def _fit_message_embeds(embeds: list[dict[str, Any]], html_url: str) -> None:
    total = sum(embed_char_count(e) for e in embeds)
    if total <= DISCORD_TOTAL_CHARS:
        return
    overflow = total - DISCORD_TOTAL_CHARS
    desc = embeds[0].get("description") or ""
    new_limit = max(0, len(desc) - overflow)
    embeds[0]["description"] = truncate_with_link(desc, new_limit, html_url)


def plan_to_dict(plan: ReleasePlan) -> dict[str, Any]:
    return {
        "tag": plan.tag,
        "name": plan.name,
        "html_url": plan.html_url,
        "published_at": plan.published_at,
        "skipped": plan.skipped,
        "files": [{"name": f.name, "url": f.url, "size": f.size} for f in plan.files],
        "images": [
            {
                "url": img.url,
                "filename": img.filename,
                "source": img.source,
                "upload": img.upload,
                "reason": img.reason,
            }
            for img in plan.images
        ],
        "messages": [
            {
                "content": msg.content,
                "embeds": msg.embeds,
                "uploads": [img.filename for img in msg.uploads],
                "leftover_images": [img.url for img in msg.leftover_images],
                "embed_chars": msg.total_embed_chars(),
            }
            for msg in plan.messages
        ],
    }


def format_plan_summary(plan: ReleasePlan, index: int, total: int) -> str:
    lines = [
        f"=== Релиз {index}/{total}: {plan.tag} — {plan.name} ===",
        f"Ссылка: {plan.html_url}",
        f"Дата: {human_date(plan.published_at) or '—'}",
    ]
    if plan.skipped:
        lines.append(f"Пропущен: {plan.skipped}")
        return "\n".join(lines)
    lines.append(f"Сообщений Discord: {len(plan.messages)}")
    for i, msg in enumerate(plan.messages, start=1):
        desc = (msg.embeds[0].get("description") if msg.embeds else "") or ""
        preview = desc if len(desc) <= 400 else desc[:400] + "…"
        lines.append(f"— сообщение {i}: {len(msg.embeds)} эмбед(ов), {msg.total_embed_chars()} символов")
        if msg.content:
            lines.append(f"  content: {msg.content.replace(chr(10), ' / ')}")
        if preview:
            lines.append("  описание:")
            for row in preview.splitlines():
                lines.append(f"    {row}")
        images = [e.get("image", {}).get("url") for e in msg.embeds if e.get("image")]
        if images:
            lines.append("  картинки в эмбедах:")
            for url in images:
                lines.append(f"    {url}")
        if msg.uploads:
            lines.append("  будут загружены вложениями:")
            for img in msg.uploads:
                lines.append(f"    {img.filename} ← {img.url} ({img.reason})")
    if plan.files:
        lines.append("Файлы (ссылки):")
        for item in plan.files:
            size = format_size(item.size)
            extra = f" ({size})" if size else ""
            lines.append(f"  - {item.name}{extra}: {item.url}")
    if plan.images:
        lines.append("Картинки:")
        for img in plan.images:
            mode = "upload" if img.upload else "embed-url"
            lines.append(f"  - [{img.source}/{mode}] {img.url} ({img.reason})")
    return "\n".join(lines)


# --- GitHub / HTTP ---------------------------------------------------------


def _request(
    url: str,
    *,
    token: str | None = None,
    accept: str = "application/vnd.github+json",
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[bytes, dict[str, str], int]:
    req_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    if headers:
        req_headers.update(headers)
    request = Request(url, data=data, headers=req_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            return body, resp_headers, response.status
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise GithubHttpError(f"HTTP {exc.code} for {url}: {detail}") from exc
    except URLError as exc:
        raise GithubHttpError(f"Network error for {url}: {exc}") from exc


def download_url(
    url: str,
    *,
    token: str | None = None,
    max_bytes: int = DISCORD_WEBHOOK_FILE_LIMIT,
    asset_id: int | None = None,
    repo: str | None = None,
) -> tuple[bytes, str | None]:
    """Download a URL, stripping Authorization when redirecting off github.com."""
    if asset_id and repo and token:
        api_url = f"https://api.github.com/repos/{repo}/releases/assets/{asset_id}"
        return _download_follow(
            api_url,
            token=token,
            max_bytes=max_bytes,
            accept="application/octet-stream",
        )
    return _download_follow(url, token=token, max_bytes=max_bytes, accept="*/*")


def _download_follow(
    url: str,
    *,
    token: str | None,
    max_bytes: int,
    accept: str,
) -> tuple[bytes, str | None]:
    current = url
    for _ in range(8):
        host = (urlparse(current).hostname or "").lower()
        use_token = bool(token and ("github.com" in host or host.endswith("githubusercontent.com")))
        headers = {"User-Agent": USER_AGENT, "Accept": accept}
        if use_token and token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(current, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=60) as response:
                content_type = response.headers.get("Content-Type")
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise GithubHttpError(f"File too large for Discord webhook (> {max_bytes} bytes): {url}")
                return data, content_type
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    raise GithubHttpError(f"Redirect without Location from {current}") from exc
                current = urljoin(current, location)
                continue
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise GithubHttpError(f"HTTP {exc.code} downloading {current}: {detail}") from exc
    raise GithubHttpError(f"Too many redirects for {url}")


def github_api_json(url: str, token: str | None) -> tuple[Any, dict[str, str]]:
    body, headers, _status = _request(url, token=token)
    return json.loads(body.decode("utf-8")), headers


def fetch_repo_meta(repo: str, token: str | None) -> dict[str, Any]:
    data, _ = github_api_json(f"https://api.github.com/repos/{repo}", token)
    return data


def fetch_releases(repo: str, token: str | None) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    url: str | None = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    while url:
        data, headers = github_api_json(url, token)
        if not isinstance(data, list):
            raise GithubHttpError(f"Unexpected releases payload: {type(data)}")
        releases.extend(data)
        url = _next_link(headers.get("link", ""))
    releases.sort(key=lambda item: item.get("published_at") or item.get("created_at") or "")
    return releases


def fetch_release_by_tag(repo: str, tag: str, token: str | None) -> dict[str, Any]:
    data, _ = github_api_json(
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}", token
    )
    return data


def _next_link(link_header: str) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            start = section.find("<")
            end = section.find(">", start)
            if start != -1 and end != -1:
                return section[start + 1 : end]
    return None


# --- Discord webhook -------------------------------------------------------


def post_discord_message(
    webhook_url: str,
    message: MessagePlan,
    *,
    token: str | None,
    repo: str | None,
    dry_run: bool,
) -> None:
    payload = {"content": message.content, "embeds": message.embeds, "allowed_mentions": {"parse": []}}
    files: list[tuple[str, bytes, str]] = []
    if not dry_run:
        for image in message.uploads:
            try:
                data, content_type = download_url(
                    image.url,
                    token=token,
                    asset_id=image.asset_id,
                    repo=repo,
                )
            except GithubHttpError as exc:
                print(f"warn: не удалось скачать {image.url}: {exc}", file=sys.stderr)
                _replace_attachment_with_link(message.embeds, image)
                continue
            ctype = content_type or image.content_type or "application/octet-stream"
            files.append((image.filename, data, ctype.split(";")[0].strip()))

    if dry_run:
        return

    _webhook_post(webhook_url, payload, files)


def _replace_attachment_with_link(embeds: list[dict[str, Any]], image: ImageRef) -> None:
    marker = f"attachment://{image.filename}"
    for embed in embeds:
        img = embed.get("image") or {}
        if img.get("url") == marker:
            if image.url:
                embed["image"] = {"url": image.url}
            else:
                embed.pop("image", None)


def _webhook_post(
    webhook_url: str,
    payload: dict[str, Any],
    files: list[tuple[str, bytes, str]],
    retries: int = 4,
) -> None:
    url = webhook_url
    if "wait=" not in url:
        url += ("&" if "?" in url else "?") + "wait=true"

    for attempt in range(retries):
        try:
            if files:
                body, headers = _encode_multipart(payload, files)
                _request(
                    url,
                    method="POST",
                    data=body,
                    headers=headers,
                    accept="application/json",
                    timeout=90,
                )
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                _request(
                    url,
                    method="POST",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    accept="application/json",
                    timeout=60,
                )
            return
        except GithubHttpError as exc:
            text = str(exc)
            retry_after = _parse_retry_after(text)
            if retry_after is not None and attempt < retries - 1:
                time.sleep(retry_after + 0.2)
                continue
            raise


def _parse_retry_after(text: str) -> float | None:
    if "HTTP 429" not in text:
        return None
    match = re.search(r'"retry_after"\s*:\s*([0-9.]+)', text)
    if match:
        return float(match.group(1))
    return 2.0


def _encode_multipart(
    payload: dict[str, Any], files: list[tuple[str, bytes, str]]
) -> tuple[bytes, dict[str, str]]:
    boundary = f"----DiscordForm{uuid.uuid4().hex}"
    crlf = b"\r\n"
    chunks: list[bytes] = []

    def add_field(name: str, value: bytes, filename: str | None, content_type: str) -> None:
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            disposition += f'; filename="{filename}"'
        chunks.append(disposition.encode("utf-8") + crlf)
        chunks.append(f"Content-Type: {content_type}\r\n".encode("ascii"))
        chunks.append(crlf)
        chunks.append(value)
        chunks.append(crlf)

    add_field(
        "payload_json",
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        None,
        "application/json",
    )
    for index, (filename, data, content_type) in enumerate(files):
        add_field(f"files[{index}]", data, filename, content_type)
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    body = b"".join(chunks)
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# --- CLI -------------------------------------------------------------------


def load_event_release(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        event = json.load(handle)
    if "release" in event and isinstance(event["release"], dict):
        return event["release"]
    if event.get("tag_name"):
        return event
    raise SystemExit(f"No release object in event payload: {path}")


def resolve_repo(cli_repo: str | None) -> str:
    repo = cli_repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise SystemExit("Укажите --repo owner/name или переменную GITHUB_REPOSITORY")
    return repo


def resolve_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def run_backfill(
    repo: str,
    token: str | None,
    *,
    tag: str | None,
    dry_run: bool,
    delay: float,
    webhook_url: str | None,
) -> list[ReleasePlan]:
    meta = fetch_repo_meta(repo, token)
    repo_private = bool(meta.get("private"))
    if tag:
        releases = [fetch_release_by_tag(repo, tag, token)]
    else:
        releases = fetch_releases(repo, token)

    plans: list[ReleasePlan] = []
    to_post = [rel for rel in releases if not rel.get("draft")]
    total = len(to_post)
    print(f"Найдено релизов: {total} (черновики пропущены). private={repo_private}")
    if not dry_run and not webhook_url:
        raise SystemExit("Нет DISCORD_WEBHOOK_URL — некуда постить (для проверки используйте --dry-run)")

    for index, release in enumerate(to_post, start=1):
        plan = build_release_plan(release, repo_private=repo_private)
        plans.append(plan)
        print(format_plan_summary(plan, index, total))
        print()
        if plan.skipped:
            continue
        if dry_run:
            continue
        assert webhook_url is not None
        for message in plan.messages:
            post_discord_message(
                webhook_url, message, token=token, repo=repo, dry_run=False
            )
        if index < total and delay > 0:
            time.sleep(delay)
    return plans


def run_event(
    release: dict[str, Any],
    repo: str,
    token: str | None,
    *,
    dry_run: bool,
    webhook_url: str | None,
) -> ReleasePlan:
    repo_private = False
    try:
        repo_private = bool(fetch_repo_meta(repo, token).get("private"))
    except GithubHttpError as exc:
        print(f"warn: не удалось прочитать видимость репозитория: {exc}", file=sys.stderr)
    plan = build_release_plan(release, repo_private=repo_private)
    print(format_plan_summary(plan, 1, 1))
    if plan.skipped:
        return plan
    if dry_run:
        return plan
    if not webhook_url:
        raise SystemExit("Нет DISCORD_WEBHOOK_URL")
    for message in plan.messages:
        post_discord_message(webhook_url, message, token=token, repo=repo, dry_run=False)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Пост GitHub-релизов в Discord")
    parser.add_argument("--mode", choices=("backfill", "event"), default="backfill")
    parser.add_argument("--repo", help="owner/name, по умолчанию GITHUB_REPOSITORY")
    parser.add_argument("--tag", help="Один тег (только backfill)")
    parser.add_argument("--dry-run", action="store_true", help="Ничего не постить в Discord")
    parser.add_argument("--delay", type=float, default=3.0, help="Пауза между постами, сек")
    parser.add_argument("--event-path", help="Путь к GitHub event JSON (mode=event)")
    parser.add_argument("--release-json", help="Путь к JSON одного релиза")
    parser.add_argument("--json-out", help="Записать машиночитаемый план в файл")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = resolve_repo(args.repo)
    token = resolve_token()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    plans: list[ReleasePlan]

    if args.mode == "event":
        path = args.release_json or args.event_path or os.environ.get("GITHUB_EVENT_PATH")
        if not path:
            raise SystemExit("mode=event требует --event-path / --release-json / GITHUB_EVENT_PATH")
        release = load_event_release(path)
        plans = [
            run_event(release, repo, token, dry_run=args.dry_run, webhook_url=webhook_url)
        ]
    else:
        plans = run_backfill(
            repo,
            token,
            tag=args.tag,
            dry_run=args.dry_run,
            delay=args.delay,
            webhook_url=webhook_url,
        )

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump([plan_to_dict(p) for p in plans], handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
