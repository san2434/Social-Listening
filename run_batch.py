#!/usr/bin/env python3
import os, json, time, pandas as pd
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

YT_USAGE_FILENAME = "youtube_keys_usage.json"   # ✅ NEW (stored in Drive)

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
        drive.files().create(body={"name":filename,"parents":[DRIVE_FOLDER_ID]},
                             media_body=media).execute()

def drive_read_json(filename, default):
    file_id = drive_find(filename)
    if not file_id:
        return default

    request = drive.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    return json.loads(fh.read().decode())

def drive_write_json(data, filename):
    fh = BytesIO(json.dumps(data, indent=2).encode())
    media = MediaIoBaseUpload(fh, mimetype="application/json")
    file_id = drive_find(filename)
    if file_id:
        drive.files().update(fileId=file_id, media_body=media).execute()
    else:
        drive.files().create(body={"name":filename,"parents":[DRIVE_FOLDER_ID]},
                             media_body=media).execute()

# ====== REDDIT AUTH ======
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"]
)

# ====== YOUTUBE KEY ROTATION ======
YOUTUBE_KEYS = json.loads(os.environ["YOUTUBE_KEYS_JSON"])

# ✅ Load previous usage if exists, else initialize from secret template
YT_USAGE = drive_read_json(YT_USAGE_FILENAME, YOUTUBE_KEYS)  # <– NEW

current_key_index = 0
YOUTUBE_API_KEY = YT_USAGE[current_key_index]["key"]

def get_youtube_service():
    global youtube
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    return youtube

get_youtube_service()

def rotate_key():
    global current_key_index, YOUTUBE_API_KEY
    current_key_index += 1
    if current_key_index >= len(YT_USAGE):
        print("⚠️ All YT keys exhausted — continuing to Reddit.")
        drive_write_json(YT_USAGE, YT_USAGE_FILENAME)   # ✅ Save usage before stopping
        raise StopIteration
    YOUTUBE_API_KEY = YT_USAGE[current_key_index]["key"]
    get_youtube_service()

def add_usage(units):
    YT_USAGE[current_key_index]["usage"] += units
    drive_write_json(YT_USAGE, YT_USAGE_FILENAME)   # ✅ Write back to Drive every time
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
                return None

# ====== SCRAPERS (UNCHANGED) ======
def youtube_scrape(keywords):
    df_existing = drive_read_csv("youtube_data_comments.csv")
    seen = set(df_existing.get("comment_id", []).dropna())
    rows = []
    since = (datetime.utcnow() - timedelta(days=7)).isoformat("T") + "Z"

    try:
        for kw in keywords:
            print(f"\n🔍 YouTube: {kw}")
            resp = yt_safe_request(youtube.search().list(q=kw, part="snippet", type="video", order="date", maxResults=50, publishedAfter=since), 100)
            if not resp: continue

            for item in resp.get("items", []):
                vid = item["id"]["videoId"]
                token = None
                while True:
                    c = yt_safe_request(youtube.commentThreads().list(videoId=vid, part="snippet", maxResults=COMMENTS_PER_REQUEST, pageToken=token), 1)
                    if not c: break
                    for t in c.get("items", []):
                        cid = t["id"]
                        if cid in seen: continue
                        seen.add(cid)
                        snip = t["snippet"]["topLevelComment"]["snippet"]
                        rows.append({"video_id": vid, "comment_id": cid, "comment": snip.get("textDisplay",""), "keyword": kw, "source": "YouTube"})
                    token = c.get("nextPageToken")
                    if not token: break

    except StopIteration:
        pass

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    drive_write_csv(df_all, "youtube_data_comments.csv")
    return df_all

def reddit_scrape(keywords):
    df_existing = drive_read_csv("Reddit_Data.csv")
    seen = set(zip(df_existing.get("post_id", []), df_existing.get("comment_id", [])))
    rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    for kw in keywords:
        print(f"\n👽 Reddit: {kw}")
        for post in reddit.subreddit("all").search(f'"{kw}"', sort="new", time_filter="week"):
            if datetime.fromtimestamp(post.created_utc, tz=timezone.utc) < cutoff:
                continue
            post.comments.replace_more(limit=0)
            extracted = 0
            for c in post.comments.list():
                key = (post.id, c.id)
                if key in seen: continue
                seen.add(key)
                rows.append({"post_id": post.id, "comment_id": c.id, "comment": c.body, "keyword": kw, "source": "Reddit"})
                extracted += 1
                if extracted >= MAX_REDDIT_COMMENTS:
                    break

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    drive_write_csv(df_all, "Reddit_Data.csv")
    return df_all

def main():
    keywords = drive_read_csv("keywords.csv")["cluster_keyword"].dropna().tolist()[:300]
    yt = youtube_scrape(keywords)
    rd = reddit_scrape(keywords)
    combined = pd.concat([yt, rd], ignore_index=True)
    drive_write_csv(combined, "Combined_Social_Data.csv")
    print("\n✅ Stability Test Completed")

if __name__ == "__main__":
    main()
