#!/usr/bin/env python3
import os, json, time, pandas as pd
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from google.oauth2.service_account import Credentials
import praw
from io import BytesIO

# ====== CONFIG ======
DRIVE_FOLDER_ID = "1Lp6rU11WMfmvEwZoIaTKyg8ex52EwAc0"   # ✅ Your Drive Folder
SWITCH_THRESHOLD = 300   # ✅ Stability test threshold
COMMENTS_PER_REQUEST = 100
MAX_REDDIT_COMMENTS = 10  # ✅ Stability test Reddit cap

# ====== AUTH: Google Drive (Service Account in GitHub Secret) ======
creds_json = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
creds = Credentials.from_service_account_info(creds_json)
drive = build("drive", "v3", credentials=creds)

def drive_read_csv(filename):
    query = f"'{DRIVE_FOLDER_ID}' in parents and name='{filename}'"
    results = drive.files().list(q=query).execute().get("files", [])
    if not results:
        return pd.DataFrame()
    file_id = results[0]["id"]
    request = drive.files().get_media(fileId=file_id)
    fh = BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    fh.seek(0)
    return pd.read_csv(fh)

def drive_write_csv(df, filename):
    query = f"'{DRIVE_FOLDER_ID}' in parents and name='{filename}'"
    results = drive.files().list(q=query).execute().get("files", [])
    fh = BytesIO()
    df.to_csv(fh, index=False)
    fh.seek(0)
    media = MediaIoBaseUpload(fh, mimetype="text/csv")

    if results:
        drive.files().update(fileId=results[0]["id"], media_body=media).execute()
    else:
        drive.files().create(body={"name": filename, "parents": [DRIVE_FOLDER_ID]}, media_body=media).execute()

# ====== REDDIT AUTH ======
reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.environ["REDDIT_USER_AGENT"]
)

# ====== YOUTUBE KEY ROTATION ======
YOUTUBE_KEYS = json.loads(os.environ["YOUTUBE_KEYS_JSON"])
current_key_index = 0
YOUTUBE_API_KEY = YOUTUBE_KEYS[current_key_index]["key"]

def get_youtube_service():
    global youtube
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    return youtube

def rotate_key():
    global current_key_index, YOUTUBE_API_KEY
    current_key_index += 1
    if current_key_index >= len(YOUTUBE_KEYS):
        raise SystemExit("🚨 All YouTube keys exhausted")
    YOUTUBE_API_KEY = YOUTUBE_KEYS[current_key_index]["key"]
    get_youtube_service()

def add_usage(units):
    YOUTUBE_KEYS[current_key_index]["usage"] += units
    if YOUTUBE_KEYS[current_key_index]["usage"] >= SWITCH_THRESHOLD:
        rotate_key()

def yt_safe_request(request, cost):
    while True:
        try:
            resp = request.execute()
            add_usage(cost)
            return resp
        except Exception as e:
            if "quotaExceeded" in str(e):
                rotate_key()
            else:
                return None

# ====== SCRAPERS ======
def youtube_scrape(keywords):
    df_existing = drive_read_csv("youtube_data_comments.csv")
    seen = set(df_existing.get("comment_id", []).dropna())

    rows = []
    get_youtube_service()
    since = (datetime.utcnow() - timedelta(days=7)).isoformat("T") + "Z"

    for kw in keywords:
        print(f"\n🔍 YouTube: {kw}")
        resp = yt_safe_request(youtube.search().list(q=kw, part="snippet", type="video", order="date", maxResults=50, publishedAfter=since), 100)
        if not resp: continue

        for item in resp.get("items", []):
            vid = item["id"]["videoId"]
            meta = yt_safe_request(youtube.videos().list(part="snippet,statistics", id=vid), 1)
            if not meta: continue

            sn = meta["items"][0]["snippet"]
            st = meta["items"][0].get("statistics", {})
            base = {"video_id": vid, "title": sn.get("title",""), "keyword": kw, "source": "YouTube"}

            token = None
            while True:
                c = yt_safe_request(youtube.commentThreads().list(videoId=vid, part="snippet", maxResults=COMMENTS_PER_REQUEST, pageToken=token), 1)
                if not c: break
                for t in c.get("items", []):
                    cid = t["id"]
                    if cid in seen: continue
                    seen.add(cid)
                    snip = t["snippet"]["topLevelComment"]["snippet"]
                    rows.append({**base, "comment_id": cid, "comment": snip.get("textDisplay","")})
                token = c.get("nextPageToken")
                if not token: break

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
                pair = (post.id, c.id)
                if pair in seen: continue
                seen.add(pair)
                rows.append({
                    "post_id": post.id, "comment_id": c.id, "comment": c.body,
                    "keyword": kw, "source": "Reddit"
                })
                extracted += 1
                if extracted >= MAX_REDDIT_COMMENTS:
                    break

    df_all = pd.concat([df_existing, pd.DataFrame(rows)], ignore_index=True)
    drive_write_csv(df_all, "Reddit_Data.csv")
    return df_all

def main():
    keywords = pd.read_excel("keywords.xlsx")["cluster_keyword"].dropna().tolist()
    keywords = keywords[:20]   # ✅ Stability test subset
    yt = youtube_scrape(keywords)
    rd = reddit_scrape(keywords)
    combined = pd.concat([yt, rd], ignore_index=True)
    drive_write_csv(combined, "Combined_Social_Data.csv")

    print("\n✅ Stability Test Completed Successfully")
    print(f"YouTube rows total: {len(yt)}")
    print(f"Reddit rows total: {len(rd)}")
    print(f"Combined rows total: {len(combined)}")

if __name__ == "__main__":
    main()
