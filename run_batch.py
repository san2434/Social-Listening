#!/usr/bin/env python3
import os, json, pandas as pd
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import praw
from io import BytesIO

# ====== CONFIG ======
DRIVE_FOLDER_ID = "1Lp6rU11WMfmvEwZoIaTKyg8ex52EwAc0"
SWITCH_THRESHOLD = 8300
COMMENTS_PER_REQUEST = 100
MAX_REDDIT_COMMENTS = 3000

# DAILY BACKUP DATE STAMP
TODAY_STR = datetime.utcnow().strftime("%Y%m%d")

# ====== GOOGLE DRIVE AUTH ======
creds_json = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
creds = Credentials.from_service_account_info(creds_json)
drive = build("drive", "v3", credentials=creds)

def drive_find(name):
    q = f"'{DRIVE_FOLDER_ID}' in parents and name='{name}'"
    results = drive.files().list(q=q).execute().get("files", [])
    return results[0]["id"] if results else None

def drive_read_csv(filename):
    file_id = drive_find(filename)
    if not file_id:
        return pd.DataFrame()

    request = drive.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    return pd.read_csv(fh)

def drive_write_csv(df, filename):
    fh = BytesIO()
    df.to_csv(fh, index=False)
    fh.seek(0)
    media = MediaIoBaseUpload(fh, mimetype="text/csv")
    file_id = drive_find(filename)
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        drive.files().create(
            body={"name": filename, "parents": [DRIVE_FOLDER_ID]},
            media_body=media
        ).execute()

# ====== REDDIT AUTH ======
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"]
)

# ====== YOUTUBE KEY ROTATION ======
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
        print("⚠️ All YouTube keys exhausted — switching to Reddit only.")
        raise StopIteration
    YOUTUBE_API_KEY = YT_USAGE[current_key_index]["key"]
    get_youtube_service()

def add_usage(units):
    YT_USAGE[current_key_index]["usage"] += units
    print(f"🔄 Updated key usage: {YT_USAGE[current_key_index]['usage']} units for {YOUTUBE_API_KEY[:10]}...")
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
                print("🚨 Quota exceeded → rotating key")
                rotate_key()
            else:
                print(f"⚠️ YouTube error: {e}")
                return None

# ======================================================================
# ========================== UPDATED SCRAPERS ==========================
# ======================================================================

def youtube_scrape(keywords):
    df_existing = drive_read_csv("youtube_data_comments.csv")
    seen_comments = set(df_existing.get("comment_id", []).dropna())
    rows_new = []
    since = (datetime.utcnow() - timedelta(days=7)).isoformat("T") + "Z"

    try:
        for kw in keywords:
            print(f"\n🔍 YouTube: {kw}")

            resp = yt_safe_request(
                youtube.search().list(
                    q=kw, part="snippet", type="video",
                    order="date", maxResults=50, publishedAfter=since
                ),
                100
            )
            if not resp:
                continue

            for item in resp.get("items", []):
                vid = item["id"]["videoId"]

                # Fetch full metadata for the video
                v_resp = yt_safe_request(
                    youtube.videos().list(
                        part="snippet,statistics,contentDetails,topicDetails",
                        id=vid
                    ),
                    1
                )
                if not v_resp or not v_resp.get("items"):
                    continue

                v = v_resp["items"][0]
                sn = v.get("snippet", {})
                st = v.get("statistics", {})

                # RENAMED video_id → id
                base = {
                    "id": vid,
                    "title": sn.get("title", ""),
                    "description": sn.get("description", ""),
                    "channel_title": sn.get("channelTitle", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "view_count": st.get("viewCount", None),
                    "like_count": st.get("likeCount", None),
                    "comment_count": st.get("commentCount", None),
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
                        if cid in seen_comments:
                            continue
                        seen_comments.add(cid)

                        snip = c["snippet"]["topLevelComment"]["snippet"]

                        rows_new.append({
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

    df_all = pd.concat([df_existing, pd.DataFrame(rows_new)], ignore_index=True)

    # ===== MAIN FILE =====
    drive_write_csv(df_all, "youtube_data_comments.csv")
    print(f"✅ YouTube saved → {len(df_all)} rows")

    # ===== DAILY BACKUP FILE =====
    backup_name = f"YouTube_Result_{TODAY_STR}.csv"
    drive_write_csv(df_all, backup_name)
    print(f"📁 Daily backup saved → {backup_name}")

    return df_all



def reddit_scrape(keywords):
    df_existing = drive_read_csv("Reddit_Data.csv")
    seen = set(zip(df_existing.get("id", []), df_existing.get("comment_id", [])))
    rows_new = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for kw in keywords:
        print(f"\n👽 Reddit: {kw}")
        for post in reddit.subreddit("all").search(f'"{kw}"', sort="new", time_filter="week"):
            created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
            if created < cutoff:
                continue

            # REMOVED video_id, RENAMED post_id → id
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

            post.comments.replace_more(limit=0)
            extracted = 0

            for c in post.comments.list():
                key = (post.id, c.id)
                if key in seen:
                    continue
                seen.add(key)

                rows_new.append({
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

    df_all = pd.concat([df_existing, pd.DataFrame(rows_new)], ignore_index=True)

    # ===== MAIN FILE =====
    drive_write_csv(df_all, "Reddit_Data.csv")
    print(f"✅ Reddit saved → {len(df_all)} rows")

    # ===== DAILY BACKUP FILE =====
    backup_name = f"Reddit_Result_{TODAY_STR}.csv"
    drive_write_csv(df_all, backup_name)
    print(f"📁 Daily backup saved → {backup_name}")

    return df_all



# ======================================================================
# =============================== MAIN =================================
# ======================================================================

def main():
    keywords = drive_read_csv("keywords.csv")["cluster_keyword"].dropna().tolist()[:300]

    yt = youtube_scrape(keywords)
    rd = reddit_scrape(keywords)

    combined = pd.concat([yt, rd], ignore_index=True)
    drive_write_csv(combined, "Combined_Social_Data.csv")

    print("\n📊 YouTube API Usage Summary (session):")
    for k in YT_USAGE:
        print(f"• {k['key'][:10]}… → {k['usage']} units used")

    print("\n🎉 Run Completed Successfully")

if __name__ == "__main__":
    main()
