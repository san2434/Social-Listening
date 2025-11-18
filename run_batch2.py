#!/usr/bin/env python3
import os
import json
import time
from io import BytesIO  # kept if needed later; not strictly required now
from datetime import datetime, timedelta, timezone

import pandas as pd
from googleapiclient.discovery import build
import praw
import prawcore

# ====== CONFIG ======
SWITCH_THRESHOLD = 10          # YouTube quota switch threshold (tune as needed)
COMMENTS_PER_REQUEST = 100
MAX_REDDIT_COMMENTS = 3000

NOW_UTC = datetime.now(timezone.utc)
TODAY_STR = NOW_UTC.strftime("%Y%m%d")

# New clean file names
YOUTUBE_CSV = "YouTube_New.csv"
REDDIT_CSV = "Reddit_New.csv"
COMBINED_CSV = "Combined_New.csv"

print("CP 1")

# ====== LOCAL OUTPUT DIRECTORY ======
LOCAL_DIR = "."
os.makedirs(LOCAL_DIR, exist_ok=True)

# ====== UNIFIED CLEAN SCHEMA ======
COLUMNS = [
    "id",
    "video_id",
    "post_id",
    "title",
    "description",
    "channel_id",
    "channel_title",
    "subreddit",
    "published_at",
    "view_count",
    "like_count",
    "favorite_count",
    "comment_count",
    "thumbnail_medium_url",
    "category_id",
    "tags",
    "topic_categories",
    "keyword",
    "keyword_match_type",
    "url",
    "source",
    "comment_id",
    "comment",
    "comment_author",
    "comment_author_channel_id",
    "comment_like_count",
    "comment_published_at",
    "comment_updated_at",
]


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure dataframe has exactly COLUMNS in this order."""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    # Drop unexpected columns and enforce order
    df = df[COLUMNS]
    return df

print("CP 2")
# ======================================================================
# =========================== LOCAL CSV HELPERS =========================
# ======================================================================

def safe_read_csv(path: str, normalize_output: bool = False) -> pd.DataFrame:
    """
    Safely read a CSV from local filesystem with basic retry and
    EmptyDataError handling. Optionally normalizes to unified schema.
    """
    full_path = os.path.join(LOCAL_DIR, path)
    if not os.path.exists(full_path):
        print(f"📂 CSV not found, starting empty: {full_path}")
        return normalize(pd.DataFrame()) if normalize_output else pd.DataFrame()

    for attempt in range(1, 4):
        try:
            df = pd.read_csv(full_path)
            print(f"📂 Loaded local CSV: {full_path}")
            if normalize_output:
                df = normalize(df)
            return df
        except pd.errors.EmptyDataError:
            print(f"⚠️ Empty CSV at {full_path}, returning empty DataFrame")
            return normalize(pd.DataFrame()) if normalize_output else pd.DataFrame()
        except Exception as e:
            print(f"⚠️ Error reading {full_path}: {e} (attempt {attempt}/3)")
            if attempt == 3:
                print("❌ Giving up on reading, returning empty DataFrame")
                return normalize(pd.DataFrame()) if normalize_output else pd.DataFrame()
            time.sleep(2)

    # Fallback
    return normalize(pd.DataFrame()) if normalize_output else pd.DataFrame()


def safe_write_csv(df: pd.DataFrame, path: str, normalize_input: bool = False):
    """
    Safely write CSV locally with basic retry. Optionally normalizes before save.
    """
    full_path = os.path.join(LOCAL_DIR, path)
    if normalize_input:
        df = normalize(df)

    for attempt in range(1, 4):
        try:
            df.to_csv(full_path, index=False)
            print(f"💾 Local CSV saved: {full_path}")
            return
        except Exception as e:
            print(f"⚠️ Error writing {full_path}: {e} (attempt {attempt}/3)")
            if attempt == 3:
                print("❌ Giving up on writing this file.")
                return
            time.sleep(2)


# ======================================================================
# ============================ REDDIT AUTH ==============================
# ======================================================================

reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"],
)

# ====== REDDIT RATE LIMIT CONFIG ======
REDDIT_TIME_FILTER = "week"
REDDIT_RETRIES = 3
REDDIT_BACKOFF_S = 60
REDDIT_SEARCH_PAUSE_S = 1.0
REDDIT_COMMENT_PAUSE_S = 0.25

print("CP 3")
def safe_reddit_search(reddit_client, query, retries=REDDIT_RETRIES):
    """
    Keyword-based Reddit search with retry and backoff.
    """
    for attempt in range(1, retries + 1):
        try:
            gen = reddit_client.subreddit("all").search(
                f'"{query}"',
                sort="new",
                time_filter=REDDIT_TIME_FILTER,
                limit=None,
            )
            return list(gen)
        except prawcore.exceptions.TooManyRequests as e:
            wait = getattr(e, "sleep_time", REDDIT_BACKOFF_S)
            print(f"⏳ 429 TooManyRequests for '{query}' — sleeping {wait}s")
            time.sleep(wait)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "temporarily" in msg.lower():
                print(f"⏳ Temporary Reddit error for '{query}' — sleeping {REDDIT_BACKOFF_S}s")
                time.sleep(REDDIT_BACKOFF_S)
            else:
                print(f"⚠️ Reddit error for '{query}': {e}")
                return []
    print(f"❌ Reddit search failed after {retries} attempts for '{query}'")
    return []


# ======================================================================
# ======================= YOUTUBE AUTH + KEY ROTATION ===================
# ======================================================================

YOUTUBE_KEYS = json.loads(os.environ["YOUTUBE_KEYS_JSON"])
YT_USAGE = YOUTUBE_KEYS
current_key_index = 0
YOUTUBE_API_KEY = YT_USAGE[current_key_index]["key"]


def get_youtube_service():
    global youtube
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    print(f"🔑 Using YouTube Key: {YOUTUBE_API_KEY[:10]}...")
    return youtube


get_youtube_service()

print("CP 4")
def rotate_key():
    global current_key_index, YOUTUBE_API_KEY
    current_key_index += 1
    if current_key_index >= len(YT_USAGE):
        print("⚠️ All YouTube keys exhausted.")
        raise StopIteration
    YOUTUBE_API_KEY = YT_USAGE[current_key_index]["key"]
    get_youtube_service()


def add_usage(units):
    YT_USAGE[current_key_index]["usage"] += units
    if YT_USAGE[current_key_index]["usage"] >= SWITCH_THRESHOLD:
        rotate_key()


def yt_safe_request(req, cost):
    while True:
        try:
            resp = req.execute()
            add_usage(cost)
            return resp
        except Exception as e:
            if "quotaExceeded" in str(e):
                print("🚨 YouTube quota exceeded — rotating key")
                rotate_key()
            else:
                print(f"⚠️ YouTube error: {e}")
                return None


# ======================================================================
# ======================== SHARED UTILS (MATCH) ========================
# ======================================================================

def get_keyword_match_type(title, body, kw):
    t = (title or "").lower()
    b = (body or "").lower()
    k = (kw or "").lower()
    title_match = k in t if k else False
    body_match = k in b if k else False
    if title_match and body_match:
        return "both"
    if title_match:
        return "title"
    if body_match:
        return "body"
    return "none"

print("CP 5")
# ======================================================================
# ========================== YOUTUBE SCRAPER ============================
# ======================================================================

def youtube_scrape(keywords):
    df_existing = safe_read_csv(YOUTUBE_CSV, normalize_output=True)
    seen_comments = set(df_existing.get("comment_id", []).dropna())
    rows = []

    since_dt = NOW_UTC - timedelta(days=7)
    since = since_dt.isoformat().replace("+00:00", "Z")

    try:
        for kw in keywords:
            print(f"\n🔍 YouTube: {kw}")

            resp = yt_safe_request(
                youtube.search().list(
                    q=kw,
                    part="snippet",
                    type="video",
                    order="date",
                    maxResults=50,
                    publishedAfter=since,
                ),
                100,
            )

            if not resp:
                continue

            for item in resp.get("items", []):
                vid = item["id"]["videoId"]

                meta = yt_safe_request(
                    youtube.videos().list(
                        part="snippet,statistics,contentDetails,topicDetails",
                        id=vid,
                    ),
                    1,
                )
                if not meta or not meta.get("items"):
                    continue

                v = meta["items"][0]
                sn = v.get("snippet", {}) or {}
                st = v.get("statistics", {}) or {}
                topics = v.get("topicDetails", {}) or {}

                title = sn.get("title", "")
                desc = sn.get("description", "")
                match_type = get_keyword_match_type(title, desc, kw)

                thumbnails = sn.get("thumbnails", {}) or {}
                thumb_med = thumbnails.get("medium", {}) or {}
                thumb_url = thumb_med.get("url")

                tags = sn.get("tags") or []
                topic_cats = topics.get("topicCategories") or []

                base = {
                    "id": vid,
                    "video_id": vid,
                    "post_id": None,
                    "title": title,
                    "description": desc,
                    "channel_id": sn.get("channelId"),
                    "channel_title": sn.get("channelTitle", ""),
                    "subreddit": None,
                    "published_at": sn.get("publishedAt", ""),
                    "view_count": st.get("viewCount"),
                    "like_count": st.get("likeCount"),
                    "favorite_count": st.get("favoriteCount"),
                    "comment_count": st.get("commentCount"),
                    "thumbnail_medium_url": thumb_url,
                    "category_id": sn.get("categoryId"),
                    "tags": "|".join(tags) if isinstance(tags, list) else tags,
                    "topic_categories": "|".join(topic_cats)
                    if isinstance(topic_cats, list)
                    else topic_cats,
                    "keyword": kw,
                    "keyword_match_type": match_type,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "source": "YouTube",
                }

                token = None
                while True:
                    c_resp = yt_safe_request(
                        youtube.commentThreads().list(
                            part="snippet",
                            videoId=vid,
                            maxResults=COMMENTS_PER_REQUEST,
                            order="relevance",
                            textFormat="plainText",
                            pageToken=token,
                        ),
                        1,
                    )
                    if not c_resp:
                        break

                    for c in c_resp.get("items", []):
                        cid = c["id"]
                        if cid in seen_comments:
                            continue
                        seen_comments.add(cid)

                        snip = c["snippet"]["topLevelComment"]["snippet"]

                        rows.append(
                            {
                                **base,
                                "comment_id": cid,
                                "comment": snip.get("textDisplay", ""),
                                "comment_author": snip.get("authorDisplayName", ""),
                                "comment_author_channel_id": (
                                    snip.get("authorChannelId") or {}
                                ).get("value"),
                                "comment_like_count": snip.get("likeCount"),
                                "comment_published_at": snip.get("publishedAt"),
                                "comment_updated_at": snip.get("updatedAt"),
                            }
                        )

                    token = c_resp.get("nextPageToken")
                    if not token:
                        break

    except StopIteration:
        pass

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    safe_write_csv(df_all, YOUTUBE_CSV, normalize_input=True)
    return df_all


# ======================================================================
# ============================ REDDIT SCRAPER ===========================
# ======================================================================

def reddit_keyword_scrape(keywords):
    """Phase 1: keyword-based Reddit scraping (last 7 days)."""
    df_existing = safe_read_csv(REDDIT_CSV, normalize_output=True)
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = NOW_UTC - timedelta(days=7)

    for kw in keywords:
        print(f"\n👽 Reddit keyword search: {kw}")
        posts = safe_reddit_search(reddit, kw)

        for post in posts:
            time.sleep(REDDIT_SEARCH_PAUSE_S)

            created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
            if created < cutoff:
                continue

            title = post.title or ""
            body = post.selftext or ""
            match_type = get_keyword_match_type(title, body, kw)

            base = {
                "id": post.id,
                "video_id": None,
                "post_id": post.id,
                "title": title,
                "description": body,
                "channel_id": None,
                "channel_title": str(post.author) if post.author else None,
                "subreddit": str(post.subreddit.display_name),
                "published_at": created.isoformat(),
                "view_count": post.score,
                "like_count": post.ups,
                "favorite_count": None,
                "comment_count": post.num_comments,
                "thumbnail_medium_url": post.thumbnail
                if getattr(post, "thumbnail", "") not in ("", "self", "default")
                else None,
                "category_id": None,
                "tags": None,
                "topic_categories": None,
                "keyword": kw,
                "keyword_match_type": match_type,
                "url": f"https://www.reddit.com{post.permalink}",
                "source": "Reddit",
            }

            try:
                post.comments.replace_more(limit=0)
            except Exception:
                continue

            for c in post.comments.list():
                time.sleep(REDDIT_COMMENT_PAUSE_S)

                key = (post.id, c.id)
                if key in seen:
                    continue
                seen.add(key)

                rows.append(
                    {
                        **base,
                        "comment_id": c.id,
                        "comment": c.body,
                        "comment_author": str(c.author) if c.author else None,
                        "comment_author_channel_id": None,
                        "comment_like_count": getattr(c, "score", None),
                        "comment_published_at": datetime.fromtimestamp(
                            c.created_utc, tz=timezone.utc
                        ).isoformat(),
                        "comment_updated_at": None,
                    }
                )

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    safe_write_csv(df_all, REDDIT_CSV, normalize_input=True)
    return df_all


def subreddit_full_scrape(subreddits):
    """Phase 2: scrape all posts from active subreddits last 7 days."""
    print("\n🔥 Starting full subreddit scrape (Phase 2)...")

    df_existing = safe_read_csv(REDDIT_CSV, normalize_output=True)
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = NOW_UTC - timedelta(days=7)

    for sr in subreddits:
        print(f"\n📡 Scraping subreddit: r/{sr}")

        try:
            # Unlimited fetch, but we exit early when posts get old
            posts = reddit.subreddit(sr).new(limit=None)
        except Exception as e:
            print(f"⚠️ Error accessing subreddit {sr}: {e}")
            continue

        for post in posts:
            created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)

            # EARLY EXIT: stop immediately when posts get older than 7 days
            if created < cutoff:
                print(f"⏹️ Stopped: older posts reached for r/{sr}")
                break

            title = post.title or ""
            body = post.selftext or ""

            base = {
                "id": post.id,
                "video_id": None,
                "post_id": post.id,
                "title": title,
                "description": body,
                "channel_id": None,
                "channel_title": str(post.author) if post.author else None,
                "subreddit": str(post.subreddit.display_name),
                "published_at": created.isoformat(),
                "view_count": post.score,
                "like_count": post.ups,
                "favorite_count": None,
                "comment_count": post.num_comments,
                "thumbnail_medium_url": (
                    post.thumbnail
                    if getattr(post, "thumbnail", "") not in ("", "self", "default")
                    else None
                ),
                "category_id": None,
                "tags": None,
                "topic_categories": None,
                "keyword": None,
                "keyword_match_type": None,
                "url": f"https://www.reddit.com{post.permalink}",
                "source": "Reddit",
            }

            try:
                post.comments.replace_more(limit=0)
            except Exception:
                continue

            for c in post.comments.list():
                time.sleep(REDDIT_COMMENT_PAUSE_S)

                key = (post.id, c.id)
                if key in seen:
                    continue
                seen.add(key)

                rows.append(
                    {
                        **base,
                        "comment_id": c.id,
                        "comment": c.body,
                        "comment_author": str(c.author) if c.author else None,
                        "comment_author_channel_id": None,
                        "comment_like_count": getattr(c, "score", None),
                        "comment_published_at": datetime.fromtimestamp(
                            c.created_utc, tz=timezone.utc
                        ).isoformat(),
                        "comment_updated_at": None,
                    }
                )

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    safe_write_csv(df_all, REDDIT_CSV, normalize_input=True)
    return df_all

print("CP 6")
# ======================================================================
# ================================ MAIN ================================
# ======================================================================

def main():
    # Read keywords.csv from repo (NOT from Drive)
    keywords_df = safe_read_csv("keywords.csv", normalize_output=False)
    if "cluster_keyword" not in keywords_df.columns:
        raise ValueError("keywords.csv must contain a 'cluster_keyword' column")
    print("Keywords read success")
    # Adjust slice as needed (e.g. [:500])
    keywords = keywords_df["cluster_keyword"].dropna().tolist()[:5]
    print("into redit keyword scrape")
    # Phase 1 — Reddit keyword search
    rd_phase1 = reddit_keyword_scrape(keywords)
    print("Reddit keyword scrape success")
    # Discover active subreddits
    active_subreddits = rd_phase1["subreddit"].dropna().unique().tolist()
    print("\n🧭 Active subreddits found:", active_subreddits)
    print("starting phase 2")
    # Phase 2 — Full subreddit scrape
    rd_phase2 = subreddit_full_scrape(active_subreddits) if active_subreddits else rd_phase1

    # YouTube scrape
    yt = youtube_scrape(keywords)

    # Combine everything
    combined = pd.concat([yt, rd_phase2], ignore_index=True)
    safe_write_csv(combined, COMBINED_CSV, normalize_input=True)

    print("\n🎉 Run Completed Successfully!")
    print("\n📊 YouTube API Usage Summary:")
    for k in YT_USAGE:
        print(f"{k['key'][:10]}… → {k['usage']}")


if __name__ == "__main__":
    main()
