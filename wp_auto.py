#!/usr/bin/env python3
"""
Automated WordPress News Publisher
Fetches RSS feeds, generates articles with Gemini AI,
adds featured images from Pexels, injects Adsterra ads,
and publishes to WordPress automatically.
"""

import os
import time
import json
import logging
import sys
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import feedparser
import requests

# ── Configuration ──────────────────────────────────────────────────────────────
WP_URL          = os.environ["WP_URL"].rstrip("/")
WP_USERNAME     = os.environ["WP_USERNAME"]
WP_APP_PASSWORD = os.environ["WP_APP_PASSWORD"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]
PEXELS_API_KEY  = os.environ["PEXELS_API_KEY"]
ADSTERRA_NATIVE = os.environ.get("ADSTERRA_NATIVE", "")
ADSTERRA_BANNER = os.environ.get("ADSTERRA_BANNER", "")

POSTS_PER_CATEGORY = 3   # 3 posts × 5 categories = 15/day (within free tier)
GEMINI_DELAY       = 15  # seconds between Gemini calls (stay under 5 RPM)
PUBLISH_DELAY      = 5   # seconds between WordPress publishes
STATE_FILE         = "published_state.json"
IST                = timezone(timedelta(hours=5, minutes=30))

# ── Category → WordPress ID ────────────────────────────────────────────────────
CATEGORY_MAP = {
    "Finance":    49,
    "Business":   50,
    "Health":     51,
    "Technology": 52,
    "Jobs":       53,
}

# ── RSS Feeds per Category ─────────────────────────────────────────────────────
RSS_FEEDS = {
    "Finance": [
        "http://www.moneycontrol.com/rss/latestnews.xml",
        "https://economictimes.indiatimes.com/rssfeedsdefault.cms",
        "https://www.financialexpress.com/feed/",
    ],
    "Business": [
        "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
        "https://www.financialexpress.com/business/feed/",
        "https://www.business-standard.com/rss/home_page_top_stories.rss",
    ],
    "Health": [
        "https://www.sciencedaily.com/rss/health_medicine.xml",
        "https://www.sciencedaily.com/rss/mind_brain.xml",
        "https://feeds.feedburner.com/ndtvnews-health",
    ],
    "Technology": [
        "https://www.sciencedaily.com/rss/computers_math.xml",
        "https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms",
        "https://www.thehindu.com/sci-tech/technology/feeder/default.rss",
    ],
    "Jobs": [
        "https://www.freshersworld.com/feed",
        "https://www.sarkarijobfind.com/feed/",
        "https://indiajoblive.com/feed",
    ],
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# ── State Management ───────────────────────────────────────────────────────────
def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"published": []}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"published": []}

def save_state(state: Dict) -> None:
    state["published"] = state["published"][-500:]
    state["last_run"] = datetime.now(IST).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def make_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80]

# ── RSS Fetching ───────────────────────────────────────────────────────────────
def fetch_feed_items(url: str) -> List[Dict]:
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:15]:
            title   = getattr(entry, "title", "").strip()
            summary = getattr(entry, "summary", "").strip()
            link    = getattr(entry, "link", "").strip()
            summary = re.sub(r"<[^>]+>", " ", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            if title and link:
                items.append({
                    "title":   title,
                    "summary": summary[:500] if summary else "",
                    "link":    link,
                })
        return items
    except Exception as e:
        logging.warning("Feed fetch failed %s: %s", url, e)
        return []

def fetch_category_items(category: str) -> List[Dict]:
    all_items = []
    for url in RSS_FEEDS.get(category, []):
        all_items.extend(fetch_feed_items(url))
    # Deduplicate by title slug
    seen, unique = set(), []
    for item in all_items:
        key = make_slug(item["title"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique

# ── Pexels Featured Image ──────────────────────────────────────────────────────
def fetch_pexels_image(query: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (image_url, photographer_name) or (None, None)"""
    try:
        # Extract clean keywords from title
        keywords = re.sub(r"[^a-zA-Z\s]", " ", query)
        keywords = " ".join(keywords.split()[:4])
        response = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": keywords, "per_page": 1, "orientation": "landscape"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("photos"):
            photo = data["photos"][0]
            return photo["src"]["large2x"], photo["photographer"]
    except Exception as e:
        logging.warning("Pexels fetch failed: %s", e)
    return None, None

def upload_image_to_wordpress(image_url: str, title: str) -> Optional[int]:
    """Downloads image and uploads to WordPress media library. Returns media ID."""
    try:
        img_response = requests.get(image_url, timeout=20)
        img_response.raise_for_status()
        filename = f"{make_slug(title)}.jpg"
        media_response = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "image/jpeg",
            },
            data=img_response.content,
            timeout=30,
        )
        media_response.raise_for_status()
        media_id = media_response.json().get("id")
        logging.info("Uploaded featured image → media ID %s", media_id)
        return media_id
    except Exception as e:
        logging.warning("Image upload failed: %s", e)
        return None

# ── Gemini Article Generation ──────────────────────────────────────────────────
def generate_article(title: str, summary: str, category: str) -> Optional[str]:
    prompt = f"""You are a professional news writer for an Indian audience. Write a detailed, engaging, well-structured blog article about the following news story.

Title: {title}
Summary: {summary}
Category: {category}

STRICT REQUIREMENTS:
- Total length: 800-1000 words
- Use proper HTML formatting with these tags only: <h2>, <h3>, <p>, <ul>, <li>, <ol>, <strong>, <em>, <table>, <thead>, <tbody>, <tr>, <th>, <td>
- Do NOT include <html>, <head>, <body>, or the article title as H1
- Do NOT include any CSS styles inline

ARTICLE STRUCTURE (follow exactly):
1. <h2>Overview</h2> — 2 paragraph introduction explaining the story clearly
2. <h2>Key Highlights</h2> — bullet list of 5-6 most important facts using <ul><li>
3. <h2>Detailed Analysis</h2> — 2-3 paragraphs of in-depth explanation
4. <h2>Impact on India</h2> — use a <table> with 2 columns: "Area Affected" and "Expected Impact" with 4-5 rows
5. <h2>What Experts Say</h2> — 1-2 paragraphs on expert opinions and reactions
6. <h2>What to Expect Next</h2> — numbered list <ol><li> of 4-5 upcoming developments to watch
7. <h2>Conclusion</h2> — 1 strong closing paragraph

WRITING STYLE:
- Simple, clear English suitable for Indian readers
- Bold important terms using <strong>
- Be factual, informative, and engaging
- No affiliate links, no promotional content

Write the article now:"""

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": 2000,
                },
            },
            timeout=45,
        )
        response.raise_for_status()
        data = response.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Clean up any markdown code fences if present
        text = re.sub(r"```html|```", "", text).strip()
        return text
    except Exception as e:
        logging.error("Gemini generation failed: %s", e)
        return None

# ── Post Assembly ──────────────────────────────────────────────────────────────
def build_post_html(
    article_html: str,
    title: str,
    category: str,
    source_link: str,
    photographer: Optional[str],
) -> str:

    # Summary box at top
    now_ist = datetime.now(IST).strftime("%d %b %Y")
    summary_box = f"""
<div style="background:#f0f7ff;border-left:4px solid #FF6B2B;padding:16px 20px;margin:20px 0;border-radius:4px;">
  <strong style="color:#1A1A2E;font-size:15px;">&#128203; Quick Summary</strong>
  <ul style="margin:10px 0 0 0;padding-left:20px;color:#333;">
    <li><strong>Category:</strong> {category}</li>
    <li><strong>Published:</strong> {now_ist}</li>
    <li><strong>Source:</strong> <a href="{source_link}" target="_blank" rel="noopener noreferrer">Read Original Story &#8599;</a></li>
  </ul>
</div>
"""

    # Native ad banner
    native_ad = f"""
<div style="margin:24px 0;text-align:center;">
{ADSTERRA_NATIVE}
</div>
""" if ADSTERRA_NATIVE else ""

    # 728x90 banner
    banner_ad = f"""
<div style="margin:24px 0;text-align:center;overflow:hidden;">
{ADSTERRA_BANNER}
</div>
""" if ADSTERRA_BANNER else ""

    # Inject ads between sections
    # Split article at each <h2> tag to insert ads between sections
    sections = re.split(r"(?=<h2)", article_html)
    
    assembled = ""
    for i, section in enumerate(sections):
        assembled += section
        # Insert 728x90 banner after sections 1, 3, 5
        if i in [1, 3, 5] and banner_ad:
            assembled += banner_ad

    # Source card at bottom
    source_card = f"""
<div style="background:#1A1A2E;color:#fff;padding:16px 20px;border-radius:6px;margin:30px 0;">
  <strong style="color:#FF6B2B;">&#128279; Original Source</strong>
  <p style="margin:8px 0 0 0;">
    <a href="{source_link}" target="_blank" rel="noopener noreferrer" 
       style="color:#FFD166;text-decoration:none;">
      Read the full original story &#8599;
    </a>
  </p>
</div>
"""

    # Photo credit
    photo_credit = ""
    if photographer:
        photo_credit = f'<p style="font-size:12px;color:#888;text-align:right;margin-top:-10px;">Photo by {photographer} on Pexels</p>'

    # Final assembly
    final_html = f"""
{summary_box}
{native_ad}
{assembled}
{banner_ad}
{source_card}
{photo_credit}
"""
    return final_html.strip()

# ── WordPress Publishing ───────────────────────────────────────────────────────
def publish_post(
    title: str,
    content: str,
    category_id: int,
    featured_media_id: Optional[int] = None,
) -> bool:
    payload = {
        "title":      title,
        "content":    content,
        "status":     "publish",
        "categories": [category_id],
    }
    if featured_media_id:
        payload["featured_media"] = featured_media_id

    try:
        response = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        post = response.json()
        logging.info("✅ Published: %s", post.get("link", title))
        return True
    except Exception as e:
        logging.error("❌ Publish failed '%s': %s", title, e)
        return False

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    logging.info("=" * 60)
    logging.info("WordPress Automation Started — %s",
                 datetime.now(IST).strftime("%d %b %Y %I:%M %p IST"))
    logging.info("=" * 60)

    state = load_state()
    published_slugs = set(state["published"])
    total_published = 0
    total_skipped   = 0

    for category, category_id in CATEGORY_MAP.items():
        logging.info("── Category: %s (ID: %s) ──", category, category_id)
        items = fetch_category_items(category)
        logging.info("Found %d unique items in %s feeds", len(items), category)
        count = 0

        for item in items:
            if count >= POSTS_PER_CATEGORY:
                break

            title_slug = make_slug(item["title"])
            if title_slug in published_slugs:
                total_skipped += 1
                continue

            logging.info("Generating: %s", item["title"])

            # Step 1: Generate article with Gemini
            time.sleep(GEMINI_DELAY)
            article_html = generate_article(
                item["title"], item["summary"], category
            )
            if not article_html:
                logging.warning("Skipping — Gemini returned nothing")
                continue

            # Step 2: Fetch featured image from Pexels
            image_url, photographer = fetch_pexels_image(item["title"])
            media_id = None
            if image_url:
                media_id = upload_image_to_wordpress(image_url, item["title"])

            # Step 3: Build full post HTML
            full_html = build_post_html(
                article_html,
                item["title"],
                category,
                item["link"],
                photographer,
            )

            # Step 4: Publish to WordPress
            time.sleep(PUBLISH_DELAY)
            success = publish_post(
                item["title"], full_html, category_id, media_id
            )

            if success:
                published_slugs.add(title_slug)
                state["published"].append(title_slug)
                count += 1
                total_published += 1
                save_state(state)  # Save after each post in case of crash

        logging.info("Category %s done — %d/%d posts published",
                     category, count, POSTS_PER_CATEGORY)

    logging.info("=" * 60)
    logging.info("Run complete — Published: %d | Skipped: %d",
                 total_published, total_skipped)
    logging.info("=" * 60)

if __name__ == "__main__":
    main()
