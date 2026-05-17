#!/usr/bin/env python3
import os, json, pandas as pd
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import praw
import prawcore
from io import BytesIO
import time

# ====== CONFIG ======
DRIVE_FOLDER_ID = "1mzU-6K9Vhcvcs-MiXgO0dGUsBpU-zkVf"
SWITCH_THRESHOLD = 10
COMMENTS_PER_REQUEST = 100
MAX_REDDIT_COMMENTS = 300
TODAY_STR = datetime.utcnow().strftime("%Y%m%d")

# ====== LOCAL OUTPUT DIRECTORY ======
LOCAL_DIR = "."
os.makedirs(LOCAL_DIR, exist_ok=True)

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
        safe_drive_call(lambda: drive.files().update(fileId=file_id, media_body=media).execute())
        print(f"☁️ Updated Drive file: {filename}")
    else:
        safe_drive_call(lambda: drive.files().create(
            body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
            media_body=media
        ).execute())
        print(f"☁️ Created new Drive file: {filename}")

# ====== REDDIT AUTH ======
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"]
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
                f'"{query}"',
                sort="new",
                time_filter=REDDIT_TIME_FILTER,
                limit=None
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
# ========================== YOUTUBE SCRAPER ============================
# ======================================================================

def youtube_scrape(keywords):
    df_existing = drive_read_csv("youtube_data_comments.csv")
    seen = set(df_existing.get("comment_id", []).dropna())
    rows = []
    since = (datetime.utcnow() - timedelta(days=7)).isoformat("T") + "Z"

    try:
        for kw in keywords:
            print(f"\n🔍 YouTube: {kw}")

            resp = yt_safe_request(
                youtube.search().list(
                    q=kw, part="snippet", type="video",
                    order="date", maxResults=50, publishedAfter=since
                ), 100)

            if not resp:
                continue

            for item in resp.get("items", []):
                vid = item["id"]["videoId"]

                meta = yt_safe_request(
                    youtube.videos().list(
                        part="snippet,statistics,contentDetails",
                        id=vid
                    ),
                    1
                )
                if not meta or not meta.get("items"):
                    continue

                v = meta["items"][0]
                sn = v["snippet"]
                st = v.get("statistics", {})

                base = {
                    "id": vid,
                    "title": sn.get("title", ""),
                    "description": sn.get("description", ""),
                    "channel_title": sn.get("channelTitle", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "view_count": st.get("viewCount"),
                    "like_count": st.get("likeCount"),
                    "comment_count": st.get("commentCount"),
                    "keyword": kw,
                    "source": "YouTube"
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
                            pageToken=token
                        ),
                        1
                    )
                    if not c_resp:
                        break

                    for c in c_resp.get("items", []):
                        cid = c["id"]
                        if cid in seen:
                            continue

                        seen.add(cid)
                        snip = c["snippet"]["topLevelComment"]["snippet"]

                        rows.append({
                            **base,
                            "comment_id": cid,
                            "comment": snip.get("textDisplay", ""),
                            "comment_author": snip.get("authorDisplayName", ""),
                            "comment_published_at": snip.get("publishedAt", "")
                        })

                    token = c_resp.get("nextPageToken")
                    if not token:
                        break

    except StopIteration:
        pass

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    drive_write_csv(df_all, "youtube_data_comments.csv")
    drive_write_csv(df_all, "Youtube_Daily_Backup.csv")
    return df_all

# ======================================================================
# ============================ REDDIT SCRAPER ===========================
# ======================================================================

def reddit_scrape(keywords):
    df_existing = drive_read_csv("Reddit_Data.csv")
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for kw in keywords:
        print(f"\n👽 Reddit: {kw}")
        posts = safe_reddit_search(reddit, kw)

        for post in posts:
            time.sleep(REDDIT_SEARCH_PAUSE_S)

            created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
            if created < cutoff:
                continue

            base = {
                "id": post.id,
                "title": post.title,
                "description": post.selftext or None,
                "channel_title": str(post.author) if post.author else None,
                "published_at": created.isoformat(),
                "view_count": post.score,
                "like_count": post.ups,
                "comment_count": post.num_comments,
                "keyword": kw,
                "source": "Reddit"
            }

            try:
                post.comments.replace_more(limit=0)
            except Exception:
                continue

            extracted = 0
            for c in post.comments.list():
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
                    "comment_published_at": datetime.fromtimestamp(
                        c.created_utc, tz=timezone.utc
                    ).isoformat()
                })

                extracted += 1
                if extracted >= MAX_REDDIT_COMMENTS:
                    break

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    drive_write_csv(df_all, "Reddit_Data.csv")
    drive_write_csv(df_all, "Reddit_Daily_Backup.csv")
    return df_all

# ======================================================================
# ================================ MAIN ================================
# ======================================================================

def main():
    
    df = drive_read_csv("keywords_hiking.csv")["cluster_keyword"].dropna().tolist()[:55]
    #print("CSV columns:", df.columns.tolist())
    #print(df.head())

    df.columns = df.columns.astype(str).str.strip().str.replace('\ufeff', '', regex=False)

    keywords = df["cluster_keyword"].dropna().tolist()[:55]
    

    yt = youtube_scrape(keywords)
    rd = reddit_scrape(keywords)

    combined = pd.concat([yt, rd], ignore_index=True)
    drive_write_csv(combined, "Combined_Social_Data.csv")

    print("\n🎉 Run Completed Successfully!")
    print("\n📊 YouTube API Usage Summary:")
    for k in YT_USAGE:
        print(f"{k['key'][:10]}… → {k['usage']}")

if __name__ == "__main__":
    main()
