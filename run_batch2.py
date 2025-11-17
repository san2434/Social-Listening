#!/usr/bin/env python3
import os
import json
import pandas as pd
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import praw
import prawcore
from io import BytesIO
import time

# ====== CONFIG ======
DRIVE_FOLDER_ID = "1Lp6rU11WMfmvEwZoIaTKyg8ex52EwAc0"
SWITCH_THRESHOLD = 10
COMMENTS_PER_REQUEST = 100
MAX_REDDIT_COMMENTS = 3000
TODAY_STR = datetime.utcnow().strftime("%Y%m%d")

# New clean file names
YOUTUBE_CSV = "YouTube_New.csv"
REDDIT_CSV = "Reddit_New.csv"
COMBINED_CSV = "Combined_New.csv"

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


# ====== BUILD GOOGLE DRIVE CLIENT WITH REBUILD OPTION ======
def build_drive():
    creds_json = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(creds_json)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


drive = build_drive()


def safe_drive_call(func, retries=3, wait=5):
    """Retry wrapper to prevent SSL/token refresh crash."""
    global drive
    for attempt in range(1, retries + 1):
        try:
            return func()
        except Exception as e:
            print(f"⚠️ Drive API error: {e} (attempt {attempt}/{retries})")
            if attempt == retries:
                raise
            print(f"⏳ Rebuilding Drive client and retrying in {wait} seconds...")
            time.sleep(wait)
            drive = build_drive()


# ====== DRIVE FUNCTIONS ======


def drive_find(name):
    q = f"'{DRIVE_FOLDER_ID}' in parents and name='{name}'"
    result = safe_drive_call(lambda: drive.files().list(q=q).execute())
    files = result.get("files", [])
    return files[0]["id"] if files else None


def drive_read_csv(filename):
    file_id = drive_find(filename)
    if not file_id:
        # Load local copy if present
        path = os.path.join(LOCAL_DIR, filename)
        if os.path.exists(path):
            print(f"📂 Loaded local CSV: {path}")
            return pd.read_csv(path)
        return pd.DataFrame()

    request = drive.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False

    while not done:
        status, done = safe_drive_call(lambda: downloader.next_chunk())

    fh.seek(0)
    df = pd.read_csv(fh)

    # Save local mirror
    df.to_csv(os.path.join(LOCAL_DIR, filename), index=False)
    print(f"💾 Saved local copy: {filename}")

    return df


def drive_write_csv(df, filename):
    df = normalize(df)
    # Save locally first
    local_path = os.path.join(LOCAL_DIR, filename)
    df.to_csv(local_path, index=False)
    print(f"💾 Local copy saved: {local_path}")

    # Now upload to Drive
    fh = BytesIO()
    df.to_csv(fh, index=False)
    fh.seek(0)
    media = MediaIoBaseUpload(fh, mimetype="text/csv")

    file_id = drive_find(filename)
    if file_id:
        safe_drive_call(
            lambda: drive.files().update(fileId=file_id, media_body=media).execute()
        )
        print(f"☁️ Updated Drive file: {filename}")
    else:
        safe_drive_call(
            lambda: drive.files()
            .create(body={"name": filename, "parents": [DRIVE_FOLDER_ID]}, media_body=media)
            .execute()
        )
        print(f"☁️ Created new Drive file: {filename}")


# ====== REDDIT AUTH ======
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


def safe_reddit_search(reddit, query, retries=REDDIT_RETRIES):
    for attempt in range(1, retries + 1):
        try:
            gen = reddit.subreddit("all").search(
                f'"{query}"', sort="new", time_filter=REDDIT_TIME_FILTER, limit=None
            )
            return list(gen)
        except prawcore.exceptions.TooManyRequests as e:
            wait = getattr(e, "sleep_time", REDDIT_BACKOFF_S)
            print(f"⏳ 429 TooManyRequests — sleeping {wait}s")
            time.sleep(wait)
        except Exception as e:
            if "429" in str(e) or "temporarily" in str(e).lower():
                print(f"⏳ Temporary Reddit error — sleeping {REDDIT_BACKOFF_S}s")
                time.sleep(REDDIT_BACKOFF_S)
            else:
                print(f"⚠️ Reddit error: {e}")
                return []
    return []


# ====== YOUTUBE AUTH + KEY ROTATION ======
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


# ======================================================================
# ========================== YOUTUBE SCRAPER ============================
# ======================================================================


def youtube_scrape(keywords):
    df_existing = normalize(drive_read_csv(YOUTUBE_CSV))
    seen_comments = set(df_existing.get("comment_id", []).dropna())
    rows = []
    since = (datetime.utcnow() - timedelta(days=7)).isoformat("T") + "Z"

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
                        part="snippet,statistics,contentDetails,topicDetails", id=vid
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
    df_all = normalize(df_all)
    drive_write_csv(df_all, YOUTUBE_CSV)
    drive_write_csv(df_all, "Youtube_Daily_Backup_New.csv")
    return df_all


# ======================================================================
# ============================ REDDIT SCRAPER ===========================
# ======================================================================


def reddit_keyword_scrape(keywords):
    """Phase 1: keyword-based Reddit scraping."""
    df_existing = normalize(drive_read_csv(REDDIT_CSV))
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

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
    df_all = normalize(df_all)
    drive_write_csv(df_all, REDDIT_CSV)
    drive_write_csv(df_all, "Reddit_Daily_Backup_New.csv")
    return df_all


def subreddit_full_scrape(subreddits):
    """Phase 2: scrape all posts from active subreddits last 7 days."""
    print("\n🔥 Starting full subreddit scrape (Phase 2)...")

    df_existing = normalize(drive_read_csv(REDDIT_CSV))
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for sr in subreddits:
        print(f"\n📡 Scraping subreddit: r/{sr}")

        try:
            # NEW: unlimited fetch, but we exit early when posts get old
            posts = reddit.subreddit(sr).new(limit=None)
        except Exception as e:
            print(f"⚠️ Error accessing subreddit {sr}: {e}")
            continue

        for post in posts:
            # Get created date
            created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)

            # NEW: stop immediately when posts get older than 7 days
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

            # Expand comments safely
            try:
                post.comments.replace_more(limit=0)
            except Exception:
                continue

            for c in post.comments.list():
                # Keep your sleep to avoid rate limit
                time.sleep(REDDIT_COMMENT_PAUSE_S)

                key = (post.id, c.id)
                if key in seen:
                    continue
                seen.add(key)

                rows.append({
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
                })

    # Combine, normalize and save
    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    df_all = normalize(df_all)
    drive_write_csv(df_all, REDDIT_CSV)
    drive_write_csv(df_all, "Reddit_Daily_Backup_New.csv")

    return df_all


# ======================================================================
# ================================ MAIN ================================
# ======================================================================


def main():
    keywords_df = drive_read_csv("keywords.csv")
    if "cluster_keyword" not in keywords_df.columns:
        raise ValueError("keywords.csv must contain a 'cluster_keyword' column")
    keywords = keywords_df["cluster_keyword"].dropna().tolist()[:5]

    # Phase 1 — Reddit keyword search
    rd_phase1 = reddit_keyword_scrape(keywords)

    # Discover active subreddits
    active_subreddits = rd_phase1["subreddit"].dropna().unique().tolist()
    print("\n🧭 Active subreddits found:", active_subreddits)

    # Phase 2 — Full subreddit scrape
    rd_phase2 = subreddit_full_scrape(active_subreddits)

    # YouTube scrape
    yt = youtube_scrape(keywords)

    # Combine everything
    combined = pd.concat([yt, rd_phase2], ignore_index=True)
    combined = normalize(combined)
    drive_write_csv(combined, COMBINED_CSV)

    print("\n🎉 Run Completed Successfully!")
    print("\n📊 YouTube API Usage Summary:")
    for k in YT_USAGE:
        print(f"{k['key'][:10]}… → {k['usage']}")


if __name__ == "__main__":
    main()
