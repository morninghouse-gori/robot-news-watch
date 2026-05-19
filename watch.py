import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


ROOT = Path(__file__).resolve().parent
TARGETS_FILE = ROOT / "targets.json"
SEEN_FILE = ROOT / "seen.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; RobotNewsWatch/1.0; "
    "+https://github.com/robot-news-watch)"
)

MAX_ITEMS_PER_TARGET = 20
MAX_NEW_ITEMS_IN_EMAIL = 30


@dataclass
class Article:
    source: str
    title: str
    url: str
    published: str = ""
    summary: str = ""


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data):
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    # RSSや記事URLに計測パラメータが混じる場合があるため、最低限の正規化だけ行う。
    return parsed._replace(fragment="").geturl()


def format_date(value: str) -> str:
    if not value:
        return "不明"
    try:
        dt = date_parser.parse(value)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return normalize_text(value)


def keyword_match(article: Article, include_keywords: List[str], exclude_keywords: List[str]) -> bool:
    haystack = f"{article.title} {article.summary}".lower()

    for kw in exclude_keywords or []:
        if kw and kw.lower() in haystack:
            return False

    if include_keywords:
        return any(kw.lower() in haystack for kw in include_keywords if kw)

    return True


def fetch_url(url: str) -> str:
    res = requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    res.raise_for_status()
    # 文字化け対策
    if not res.encoding or res.encoding.lower() == "iso-8859-1":
        res.encoding = res.apparent_encoding
    return res.text


def fetch_rss(target: Dict) -> List[Article]:
    feed = feedparser.parse(target["url"])
    articles: List[Article] = []

    for entry in feed.entries[:MAX_ITEMS_PER_TARGET]:
        title = normalize_text(getattr(entry, "title", ""))
        link = normalize_url(getattr(entry, "link", ""))

        published = ""
        if getattr(entry, "published", None):
            published = entry.published
        elif getattr(entry, "updated", None):
            published = entry.updated

        summary = normalize_text(getattr(entry, "summary", ""))

        if title and link:
            articles.append(
                Article(
                    source=target["name"],
                    title=title,
                    url=link,
                    published=format_date(published),
                    summary=summary,
                )
            )

    return articles


def extract_date_near(element) -> str:
    # 記事リンクの親要素周辺から日付らしい文字列を探す
    parent_text = ""
    for parent in [element.parent, element.parent.parent if element.parent else None]:
        if parent:
            parent_text += " " + parent.get_text(" ", strip=True)

    patterns = [
        r"\d{4}[./-]\d{1,2}[./-]\d{1,2}",
        r"\d{4}年\d{1,2}月\d{1,2}日",
        r"\d{1,2}月\d{1,2}日",
    ]
    for pat in patterns:
        m = re.search(pat, parent_text)
        if m:
            return m.group(0)
    return "不明"


def looks_like_article_url(base_url: str, href: str) -> bool:
    if not href:
        return False

    href = href.strip()
    if href.startswith("#") or href.startswith("javascript:"):
        return False

    full = urljoin(base_url, href)
    parsed_base = urlparse(base_url)
    parsed = urlparse(full)

    if parsed.netloc and parsed.netloc != parsed_base.netloc:
        return False

    path = parsed.path

    # よくある記事URLパターン
    article_patterns = [
        r"/articles?/",
        r"/\d{4}/\d{2}/",
        r"/news/",
        r"/newarticle/",
        r"/article/",
        r"/\d+/?$",
    ]
    return any(re.search(pat, path) for pat in article_patterns)


def fetch_html_generic(target: Dict) -> List[Article]:
    html = fetch_url(target["url"])
    soup = BeautifulSoup(html, "html.parser")
    articles: List[Article] = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        title = normalize_text(a.get_text(" ", strip=True))
        if len(title) < 8:
            continue

        href = a.get("href", "")
        if not looks_like_article_url(target["url"], href):
            continue

        full_url = normalize_url(urljoin(target["url"], href))
        if full_url in seen_urls:
            continue

        seen_urls.add(full_url)
        articles.append(
            Article(
                source=target["name"],
                title=title,
                url=full_url,
                published=format_date(extract_date_near(a)),
            )
        )

        if len(articles) >= MAX_ITEMS_PER_TARGET:
            break

    return articles


def fetch_html(target: Dict) -> List[Article]:
    # 最初は汎用抽出で十分。HTML構造が変わるサイトが出たら、ここにサイト別抽出を追加する。
    return fetch_html_generic(target)


def fetch_articles(target: Dict) -> List[Article]:
    method = target.get("method", "html")
    if method == "rss":
        return fetch_rss(target)
    if method == "html":
        return fetch_html(target)
    raise ValueError(f"Unknown method: {method}")


def make_article_id(article: Article) -> str:
    # URLを主キーにする。タイトル変更があっても同じ記事として扱う。
    return article.url


def build_email_body(new_articles: List[Article]) -> str:
    lines = []
    lines.append("産業用ロボット・FA・自動化関連の新着ニュースがありました。")
    lines.append("")
    for i, article in enumerate(new_articles[:MAX_NEW_ITEMS_IN_EMAIL], start=1):
        lines.append(f"{i}. {article.title}")
        lines.append(f"媒体：{article.source}")
        lines.append(f"掲載日：{article.published or '不明'}")
        lines.append(f"URL：{article.url}")
        lines.append("")

    if len(new_articles) > MAX_NEW_ITEMS_IN_EMAIL:
        lines.append(f"※他 {len(new_articles) - MAX_NEW_ITEMS_IN_EMAIL} 件あります。")

    return "\n".join(lines)


def send_email(new_articles: List[Article]):
    mail_from = os.environ.get("MAIL_FROM")
    mail_to = os.environ.get("MAIL_TO")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not mail_from or not mail_to or not app_password:
        print("メール送信に必要な環境変数が不足しています。")
        print("MAIL_FROM, MAIL_TO, GMAIL_APP_PASSWORD をGitHub Secretsに設定してください。")
        return

    subject = f"【産業用ロボットニュース】新着{len(new_articles)}件"
    body = build_email_body(new_articles)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(mail_from, app_password)
        smtp.send_message(msg)

    print(f"メールを送信しました: {subject}")


def main():
    targets = load_json(TARGETS_FILE, [])
    seen = load_json(SEEN_FILE, {})

    if not isinstance(seen, dict):
        seen = {}

    new_articles: List[Article] = []
    now = datetime.now(timezone.utc).isoformat()

    for target in targets:
        if not target.get("enabled", True):
            continue

        print(f"Checking: {target.get('name')}")

        try:
            articles = fetch_articles(target)
        except Exception as e:
            print(f"取得に失敗しました: {target.get('name')} / {e}", file=sys.stderr)
            continue

        include_keywords = target.get("include_keywords", [])
        exclude_keywords = target.get("exclude_keywords", [])

        for article in articles:
            if not keyword_match(article, include_keywords, exclude_keywords):
                continue

            article_id = make_article_id(article)
            if article_id in seen:
                continue

            new_articles.append(article)
            seen[article_id] = {
                "title": article.title,
                "source": article.source,
                "published": article.published,
                "first_seen_at": now,
            }

    if new_articles:
        print(f"新着記事: {len(new_articles)}件")
        send_email(new_articles)
    else:
        print("新着記事はありません。")

    save_json(SEEN_FILE, seen)


if __name__ == "__main__":
    main()
