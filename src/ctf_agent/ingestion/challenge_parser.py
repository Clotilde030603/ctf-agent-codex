from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin


class ChallengeHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.links: list[str] = []
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag_stack.append(tag)
        attrs_map = dict(attrs)
        if tag == "a" and attrs_map.get("href"):
            href = attrs_map["href"]
            if href:
                self.links.append(urljoin(self.base_url, href))
        if tag in {"br", "p", "div", "li"}:
            self.body_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[index] == tag:
                del self._tag_stack[index:]
                break
        if tag in {"p", "div", "li"}:
            self.body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if "title" in self._tag_stack or "h1" in self._tag_stack:
            self.title_parts.append(data.strip())
        if any(
            tag in self._tag_stack
            for tag in ("main", "article", "section", "body", "p", "div", "li")
        ):
            self.body_parts.append(data)


def parse_challenge_html(html: str, base_url: str) -> dict[str, object]:
    parser = ChallengeHTMLParser(base_url)
    parser.feed(html)
    title = _compact(" ".join(part for part in parser.title_parts if part)) or "Untitled challenge"
    description = _compact("\n".join(parser.body_parts))
    attachments = [link for link in parser.links if _looks_like_attachment(link)]
    flag_format = extract_flag_format(description)
    points = extract_points(description)
    return {
        "title": title,
        "description": description,
        "attachments": attachments,
        "flag_format": flag_format,
        "points": points,
    }


def extract_flag_format(text: str) -> str | None:
    match = re.search(r"([A-Za-z0-9_-]+\{[^{}\s]{0,80}\})", text)
    if match:
        value = match.group(1)
        return value if "*" in value or "..." in value else value.split("{", 1)[0] + "{...}"
    match = re.search(r"flag\s*format\s*[:\-]\s*([^\n\r]+)", text, re.I)
    return match.group(1).strip() if match else None


def extract_points(text: str) -> int | None:
    match = re.search(r"\b(\d{2,5})\s*(?:pts|points)\b", text, re.I)
    return int(match.group(1)) if match else None


def _looks_like_attachment(url: str) -> bool:
    lower = url.lower().split("?", 1)[0]
    return any(
        token in lower
        for token in (
            "/files/",
            "/download",
            "/attachments/",
            ".zip",
            ".tar",
            ".gz",
            ".7z",
            ".pcap",
            ".py",
            ".bin",
        )
    )


def _compact(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()
