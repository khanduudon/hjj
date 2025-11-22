"""
courses_bot_full.py
- /start shows numbered batches with Batch ID (copyable)
- choose a number -> bot asks for Course ID (string/hex allowed)
- send Course ID -> bot fetches /classes?populate=full and active list to get PDF
- builds a flat line TXT (one item per line) containing:
    [Subject] <Full Title> : <link>
  (class video links and class PDFs both appear as separate lines with same title)
- appends summary at end of TXT
- sends the txt as a document with summary in caption
- robust: handles errors, always returns safe values
"""

import os
import tempfile
import logging
from pathlib import Path
import time
import json
import requests
import telebot
import re
from flask import Flask
from telebot.apihelper import ApiTelegramException

# ---------------- CONFIG ----------------
BOT_TOKEN = "8555830501:AAHUlM8DbKC-oWKl1JE1VDKILUNJjlanIhE" # <-- REPLACE with your Bot token
BASE_URL = "https://backend.multistreaming.site/api"
USER_ID_FOR_ACTIVE = "1448640"
BASE_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}
# ----------------------------------------

if BOT_TOKEN.startswith("PUT_"):
    raise SystemExit("Please set your BOT_TOKEN in the script before running.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

# Simple in-memory user state
user_state = {}      # chat_id -> "await_batch" / "await_course_id" / None
user_batches = {}    # chat_id -> list_of_batches (from /courses/active)
user_selected = {}   # chat_id -> selected batch object

app = Flask("render_web")
def safe_send(send_func, *args, **kwargs):
    try:
        return send_func(*args, **kwargs)
    except Exception as e:
        print(f"[safe_send error] {e}")
        return None



@app.route("/")
def home():
    return "✅ Bot is running on Render!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ---------------- Helpers ----------------
def safe_json_get(r):
    try:
        return r.json()
    except Exception as e:
        logging.warning("safe_json_get failed: %s", e)
        return {}


def get_active_batches():
    """Return (ok, batches_list). Always safe."""
    url = f"{BASE_URL}/courses/active?userId={USER_ID_FOR_ACTIVE}"
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=15)
        data = safe_json_get(r)
        if isinstance(data, dict) and data.get("state") == 200 and isinstance(data.get("data"), list):
            return True, data["data"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return True, data["data"]
        return False, []
    except Exception as e:
        logging.exception("get_active_batches error")
        return False, []


def get_course_classes(course_id):
    """Fetch classes for a course_id using classes?populate=full. Returns (ok, classes_list)."""
    url = f"{BASE_URL}/courses/{course_id}/classes?populate=full"
    try:
        r = requests.get(url, headers=BASE_HEADERS, timeout=20)
        data = safe_json_get(r)
        if isinstance(data, dict) and data.get("state") == 200 and isinstance(data.get("data"), list):
            return True, data["data"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "classes" in inner and isinstance(inner["classes"], list):
                return True, inner["classes"]
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            return True, data["data"]
        return False, []
    except Exception as e:
        logging.exception("get_course_classes error")
        return False, []


def find_pdf_from_active(course_id, batches=None):
    """Search active batches list for batchInfoPdfUrl. Return list (may be empty)."""
    try:
        if batches is None:
            ok, batches = get_active_batches()
            if not ok:
                return []
        for b in batches:
            if str(b.get("id")) == str(course_id) or str(b.get("_id")) == str(course_id):
                pdf = b.get("batchInfoPdfUrl") or b.get("batch_info_pdf") or b.get("pdf") or ""
                if not pdf:
                    return []
                if isinstance(pdf, list):
                    return [p for p in pdf if p]
                if isinstance(pdf, str):
                    parts = re.split(r"[\n,;]+", pdf)
                    return [p.strip() for p in parts if p.strip()]
        return []
    except Exception:
        return []


def _extract_subject_from_title(title, fallback=None):
    """Extract a compact subject token for bracket prefix."""
    try:
        if "||" in title:
            parts = [p.strip() for p in title.split("||")]
            if len(parts) > 1:
                second = parts[1]
                if "|" in second:
                    return second.split("|")[0].strip()
                return second.strip()
        if "|" in title:
            parts = [p.strip() for p in title.split("|")]
            for p in parts:
                if p and not re.search(r"(?i)class[\s-]*\d+", p):
                    return p
        if fallback:
            return fallback
        return "Course"
    except Exception:
        return fallback or "Course"


def normalize_video_entries(class_item):
    """Extract primary link, mp4s, and PDFs from class_item."""
    title = (
        class_item.get("title")
        or class_item.get("classTitle")
        or class_item.get("name")
        or class_item.get("heading")
        or "Untitled"
    )

    candidate_links = []
    direct_keys = [
        "class_link", "videoLink", "video_link", "video_url", "videoUrl",
        "link", "url", "playbackUrl", "playback_url", "streamUrl", "stream_url"
    ]
    for k in direct_keys:
        v = class_item.get(k)
        if isinstance(v, str) and v:
            candidate_links.append(v)

    m3u8_keys = [
        "masterPlaylist", "master_playlist",
        "hlsLink", "hls_link",
        "secureLink", "secure_link",
        "m3u8", "m3u8Url", "m3u8_url",
        "playlist", "playlistUrl"
    ]
    for k in m3u8_keys:
        v = class_item.get(k)
        if isinstance(v, str) and v:
            candidate_links.append(v)

    array_keys = ["rawSources", "sources", "recordings", "files", "videoFiles", "videos", "assets"]
    for k in array_keys:
        arr = class_item.get(k)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and it:
                    candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str) and vv:
                            candidate_links.append(vv)

    nested_keys = ["playback", "video", "stream", "media"]
    for nk in nested_keys:
        obj = class_item.get(nk)
        if isinstance(obj, dict):
            for subk in ("url", "file", "m3u8", "mp4", "hls", "src"):
                vv = obj.get(subk)
                if isinstance(vv, str) and vv:
                    candidate_links.append(vv)
        elif isinstance(obj, list):
            for it in obj:
                if isinstance(it, str):
                    candidate_links.append(it)
                elif isinstance(it, dict):
                    for subk in ("url", "file", "src", "mp4", "m3u8"):
                        vv = it.get(subk)
                        if isinstance(vv, str):
                            candidate_links.append(vv)

    for k in ("embed", "iframe", "embedHtml"):
        v = class_item.get(k)
        if isinstance(v, str) and "http" in v:
            m = re.search(r"https?://[^\s'\"<>]+", v)
            if m:
                candidate_links.append(m.group(0))

    seen = set()
    clean_candidates = []
    for u in candidate_links:
        if not isinstance(u, str) or not u.strip():
            continue
        u = u.strip()
        if u not in seen:
            seen.add(u)
            clean_candidates.append(u)

    hls_links = [u for u in clean_candidates if "m3u8" in u or "playlist-mpl" in u or "hls" in u.lower()]
    other_links = [u for u in clean_candidates if u not in hls_links]

    mp4_list = []
    for u in clean_candidates:
        if u.lower().endswith(".mp4") or ".mp4?" in u.lower():
            mp4_list.append(u)

    explicit_mp4 = class_item.get("mp4Recordings") or class_item.get("mp4_recordings") or class_item.get("mp4records")
    if isinstance(explicit_mp4, list):
        for it in explicit_mp4:
            if isinstance(it, str) and it.strip():
                if it not in mp4_list:
                    mp4_list.append(it.strip())
            elif isinstance(it, dict):
                for subk in ("url", "file", "mp4"):
                    vv = it.get(subk)
                    if isinstance(vv, str) and vv.strip() and vv not in mp4_list:
                        mp4_list.append(vv.strip())

    mp4_seen = set()
    mp4_clean = []
    for m in mp4_list:
        if m not in mp4_seen:
            mp4_seen.add(m)
            mp4_clean.append(m)

    class_pdfs = []
    pdf_keys = ["classPdf", "class_pdf", "pdfs", "materials", "resources", "files"]
    for key in pdf_keys:
        arr = class_item.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, str) and ".pdf" in it.lower():
                    class_pdfs.append(it.strip())
                elif isinstance(it, dict):
                    for subk in ("url", "file", "pdf"):
                        vv = it.get(subk)
                        if isinstance(vv, str) and ".pdf" in vv.lower():
                            class_pdfs.append(vv.strip())

    for k in ("pdf", "pdfUrl", "pdf_url", "file"):
        v = class_item.get(k)
        if isinstance(v, str) and ".pdf" in v.lower():
            class_pdfs.append(v.strip())

    pdf_seen = set()
    pdf_clean = []
    for p in class_pdfs:
        if p not in pdf_seen:
            pdf_seen.add(p)
            pdf_clean.append(p)

    primary_link = ""
    if hls_links:
        primary_link = hls_links[0]
    elif other_links:
        primary_link = other_links[0]
    else:
        primary_link = ""

    include_mp4s = False if primary_link and ("m3u8" in primary_link or "hls" in primary_link.lower() or "playlist-mpl" in primary_link) else True

    return {
        "title": title,
        "class_link": primary_link,
        "mp4Recordings": mp4_clean if include_mp4s else [],
        "classPdf": pdf_clean
    }


def build_txt_for_course(course_id, course_title=None):
    """Build TXT content and summary for a course."""
    ok, classes = get_course_classes(course_id)
    batches_ok, batches = get_active_batches()

    if not ok:
        return False, "ERROR: Failed to fetch classes for this course.", {}

    items_to_process = []
    try:
        if isinstance(classes, list) and classes and isinstance(classes[0], dict) and classes[0].get("topicName") and classes[0].get("classes"):
            for topic_block in classes:
                for cls in topic_block.get("classes", []):
                    items_to_process.append(cls)
        else:
            items_to_process = classes if isinstance(classes, list) else []
    except Exception:
        items_to_process = classes if isinstance(classes, list) else []

    lines = []

    total_videos = 0
    total_mp4 = 0
    total_m3u8 = 0
    total_youtube = 0
    total_pdfs = 0

    for cls in items_to_process:
        normalized = normalize_video_entries(cls)
        title = normalized.get("title", "Untitled")
        subject = _extract_subject_from_title(title, fallback=(course_title or "Course"))

        primary = normalized.get("class_link") or ""
        if primary:
            lines.append(f"[{subject}] {title} : {primary}")
            total_videos += 1
            u = primary.lower()
            if "m3u8" in u or "playlist" in u or "hls" in u:
                total_m3u8 += 1
            elif "youtube" in u:
                total_youtube += 1
            else:
                total_mp4 += 1
        elif normalized.get("mp4Recordings"):
            for m in normalized.get("mp4Recordings"):
                lines.append(f"[{subject}] {title} : {m}")
                total_videos += 1
                total_mp4 += 1

        for p in normalized.get("classPdf", []):
            lines.append(f"[{subject}] {title} : {p}")
            total_pdfs += 1

    course_level_pdfs = find_pdf_from_active(course_id, batches if batches_ok else None)
    if isinstance(course_level_pdfs, str):
        if course_level_pdfs and course_level_pdfs.lower() != "no pdf":
            course_level_pdfs = [u.strip() for u in re.split(r"[\n,;]+", course_level_pdfs) if u.strip()]
        else:
            course_level_pdfs = []

    if isinstance(course_level_pdfs, list) and course_level_pdfs:
        subj = course_title or "Course"
        for p in course_level_pdfs:
            lines.append(f"[{subj}] {subj} : {p}")
            total_pdfs += 1

    txt_content = "\n".join(lines)
    summary_text = (
        f"📊 Export Summary:\n"
        f"🔗 Total Links: {len(lines)}\n"
        f"🎬 Videos: {total_videos}\n"
        f"📄 PDFs: {total_pdfs}"
    )
    txt_content += "\n\n" + summary_text

    summary_dict = {
        "total_links": len(lines),
        "total_videos": total_videos,
        "total_mp4": total_mp4,
        "total_m3u8": total_m3u8,
        "total_youtube": total_youtube,
        "total_pdfs": total_pdfs,
        "summary_text": summary_text
    }

    return True, txt_content, summary_dict


# ---------------- BOT HANDLERS ----------------
@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id
    ok, batches = get_active_batches()
    if not ok:
        bot.send_message(chat_id, "❌ Unable to fetch batch list. Try again later.")
        return

    user_batches[chat_id] = {str(b.get("id") or b.get("_id")): b for b in batches}
    user_state[chat_id] = "await_course_id"

    msg_lines = ["➡ *Batch select karo by Batch ID*\n"]
    for i, b in enumerate(batches, start=1):
        title = b.get("title") or b.get("name") or "Untitled"
        bid = b.get("id") or b.get("_id") or ""
        msg_lines.append(f"{i}. *{title}*")
        msg_lines.append(f"   🎯 Batch ID: `{bid}`\n")

    bot.send_message(chat_id, "\n".join(msg_lines), parse_mode="Markdown")
    bot.send_message(chat_id, "➡ Send the *Batch ID* directly for the course you want.", parse_mode="Markdown")


@bot.message_handler(func=lambda m: user_state.get(m.chat.id) == "await_course_id")
def handle_course_id(message):
    chat_id = message.chat.id
    batch_id = (message.text or "").strip()
    if not batch_id:
        bot.reply_to(message, "❌ Please send a valid Batch ID (string).")
        return

    selected = user_batches.get(chat_id, {}).get(batch_id)
    if not selected:
        bot.reply_to(message, f"❌ Invalid Batch ID: {batch_id}. Make sure it's exact.")
        return

    user_selected[chat_id] = selected
    course_title = selected.get("title") or "Course"
    bot.send_message(chat_id, "⏳ Fetching course data... Please wait.")

    ok, txt, summary = build_txt_for_course(batch_id, course_title=course_title)
    if not ok:
        bot.send_message(chat_id, f"❌ Failed to fetch course data for ID: {batch_id}")
        return

    tmp_path = None
    try:
        safe_title = re.sub(r"[^\w\s-]", "", course_title).strip().replace(" ", "_")
        tmp_file_name = f"{safe_title}.txt"
        tmp_path = os.path.join(tempfile.gettempdir(), tmp_file_name)
        with open(tmp_path, "w", encoding="utf-8") as tf:
            tf.write(txt)

        with open(tmp_path, "rb") as doc:
            bot.send_document(chat_id, doc, caption=f"Course export: {course_title}\n\n{summary.get('summary_text','')}")

    except Exception as e:
        logging.exception("Error sending document")
        bot.send_message(chat_id, "❌ Error while preparing/sending file.")
    finally:
        try:
            if tmp_path and Path(tmp_path).exists():
                os.remove(tmp_path)
        except Exception:
            pass

    user_state[chat_id] = None
    user_selected.pop(chat_id, None)
    user_batches.pop(chat_id, None)


@bot.message_handler(func=lambda m: True)
def fallback(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Use /start to list batches and export a course. If you're in the flow, follow instructions.")


# ---------------- RUN ----------------
if __name__ == "__main__":
    logging.info("Bot starting...")
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        logging.info("Bot stopped by user.")
    except Exception:
        logging.exception("Bot crashed")
