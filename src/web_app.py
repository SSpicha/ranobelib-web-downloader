"""Lightweight proxy backend for RanobeLIB web client.

This server does NOT download chapters or build books.
It only:
- proxies RanobeLIB API requests to avoid browser CORS limits
- exchanges OAuth authorization code for access token
- serves the static SPA
- serves generated files from /downloads
"""
import asyncio
import copy
import os
import re
import sys
import time
from pathlib import Path

# ----- keep Hermes/global site-packages available -----
# Add only what's needed for this project.
APP_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = APP_DIR / "src"
LOCAL_DOWNLOADER_DIR = SRC_DIR / "ranobelib_downloader"
WEB_DIR = APP_DIR / "web"
USER_DATA_DIR = APP_DIR / "user_data"
DOWNLOADS_DIR = APP_DIR / "downloads"

for p in [USER_DATA_DIR, DOWNLOADS_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# Prefer local bundled core so the project is self-contained.
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(LOCAL_DOWNLOADER_DIR) not in sys.path:
    sys.path.insert(0, str(LOCAL_DOWNLOADER_DIR))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from ranobelib_downloader.api import RanobeLibAPI
from ranobelib_downloader.auth import RanobeLibAuth
from ranobelib_downloader.api_dto import normalize_novel_info, normalize_chapter
from ranobelib_downloader.branches import (
    get_formatted_branches_with_teams,
    get_default_branch_chapters,
    get_unique_chapters_count,
)
from ranobelib_downloader.parser import RanobeLibParser
from ranobelib_downloader.img import ImageHandler
from ranobelib_downloader.settings import settings
from ranobelib_downloader.creators.epub import EpubCreator
from ranobelib_downloader.creators.fb2 import Fb2Creator
from ranobelib_downloader.creators.html import HtmlCreator
from ranobelib_downloader.creators.txt import TxtCreator

import uuid
import threading
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=3)
tasks = {}
tasks_lock = threading.Lock()


TASK_TTL_SECONDS = 3600
# Temp images (covers, processed chapter images) live under user_data/temp.
# On Render free (1GB disk) these must be purged or the disk fills up.
TEMP_TTL_SECONDS = 3600


def _cleanup_temp_images():
    temp_dir = USER_DATA_DIR / "temp"
    if not temp_dir.exists():
        return
    now = time.time()
    for f in temp_dir.glob("*"):
        if f.is_file() and (now - f.stat().st_mtime > TEMP_TTL_SECONDS):
            try:
                f.unlink()
            except Exception:
                pass


def cleanup_old_files():
    now = time.time()
    if DOWNLOADS_DIR.exists():
        for f in DOWNLOADS_DIR.glob("*"):
            if f.is_file() and (now - f.stat().st_mtime > 3600):
                try:
                    f.unlink()
                except Exception:
                    pass
    _cleanup_temp_images()


def _purge_old_tasks() -> None:
    now = time.time()
    expired = [task_id for task_id, task in tasks.items() if now - task.get("created_at", 0) > TASK_TTL_SECONDS]
    for task_id in expired:
        tasks.pop(task_id, None)


def _norm_team_name(name: str) -> str:
    """Normalize a translation-team name for fuzzy matching.

    API team names are inconsistent with what the bot stores (e.g. trailing
    dots, case, extra spaces): 'OneSecond Evil Corp.' vs 'OneSecond Evil Corp'.
    Strip casing, surrounding whitespace and trailing/ambient punctuation so
    the selected_team_keys filter actually matches chapters.
    """
    if not name:
        return ""
    s = str(name).strip().lower()
    # drop a trailing period and any stray punctuation, collapse spaces
    s = re.sub(r"[.\u2026]+$", "", s)          # trailing dots / ellipsis
    s = re.sub(r"[^\w\s\-]", " ", s)            # keep word chars, dash, space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _dedup_chapters(chapters, selected_team_names, branch_id):
    """Collapse chapters to ONE per number, preferring the selected team/branch.

    RanobeLib returns the same chapter number under multiple branches/teams
    (e.g. '1-50' resolves to 80 raw chapters across two teams). A user asking
    for '1-50' expects exactly 50 files. We keep one chapter per number:

      1. a chapter whose branch matches branch_id AND team is in selected_team_names
      2. else a chapter whose branch matches branch_id (any team)
      3. else the first chapter for that number

    The kept chapter's ``branches`` is narrowed to the matched branch so the
    creator downloads exactly that translation, not a sibling team's.
    """
    def _group_key(ch):
        # Normalize the chapter number so "541" and "541.0" collapse to one
        # group (APIs occasionally emit trailing .0). Invalid numbers are
        # returned as None so the caller can skip them instead of merging
        # every number-less chapter into a single "".
        num = ch.get("number")
        if num is None:
            return None
        try:
            return f"{float(num):g}"
        except (TypeError, ValueError):
            return str(num).strip()

    groups = {}
    for ch in chapters:
        key = _group_key(ch)
        if key is None:
            continue
        groups.setdefault(key, []).append(ch)

    out = []
    for key, items in groups.items():
        pick = None  # (chapter, branch_entry)
        # 1) selected team + branch
        if selected_team_names:
            for ch in items:
                for b in ch.get("branches", []) or []:
                    if not isinstance(b, dict):
                        continue
                    bid = str(b.get("branch_id") if b.get("branch_id") is not None else "0")
                    if branch_id and bid != branch_id:
                        continue
                    names = [
                        _norm_team_name(t["name"])
                        for t in (b.get("teams") or [])
                        if isinstance(t, dict) and t.get("name")
                    ]
                    if any(n in selected_team_names for n in names):
                        pick = (ch, b)
                        break
                if pick:
                    break
        # 2) selected branch, any team (only when a specific team was NOT chosen)
        if pick is None and branch_id and not selected_team_names:
            for ch in items:
                for b in ch.get("branches", []) or []:
                    if not isinstance(b, dict):
                        continue
                    bid = str(b.get("branch_id") if b.get("branch_id") is not None else "0")
                    if bid == branch_id:
                        pick = (ch, b)
                        break
                if pick:
                    break
        # 3) first available — but if a branch was requested, only accept a
        #    chapter that actually belongs to that branch (else skip the number)
        if pick is None:
            if branch_id:
                for ch in items:
                    for b in ch.get("branches", []) or []:
                        if isinstance(b, dict) and str(
                            b.get("branch_id") if b.get("branch_id") is not None else "0"
                        ) == branch_id:
                            pick = (ch, b)
                            break
                    if pick:
                        break
                if pick is None:
                    continue  # requested branch has no chapter for this number
            else:
                ch0 = items[0]
                b0 = (ch0.get("branches", []) or [None])[0]
                pick = (ch0, b0)

        if pick is None:
            continue  # requested branch has no chapter for this number

        ch, b = pick
        if isinstance(b, dict):
            ch = copy.deepcopy(ch)  # never mutate the source chapter
            ch["branches"] = [copy.deepcopy(b)]  # nor the matched branch entry
        out.append(ch)

    def _num(ch):
        try:
            return float(ch.get("number", 0))
        except (TypeError, ValueError):
            return float("inf")

    out.sort(key=_num)
    return out


def run_download_task(task_id: str, body: dict):
    _purge_old_tasks()
    try:
        token = (body.get("token") or "").strip()
        if token:
            api.set_token(token)

        slug = body.get("slug")
        if not slug:
            raise ValueError("slug required")

        novel_info = api.get_novel_info(slug)
        if not isinstance(novel_info, dict):
            raise ValueError("Не удалось получить информацию о новелле")
        novel_info = normalize_novel_info(novel_info)

        all_chapters = api.get_novel_chapters(slug)
        if not all_chapters:
            raise ValueError("Не удалось загрузить список глав")

        selected_raw = body.get("chapters") or []
        if selected_raw:
            selected_ids = {str(c.get("id")) for c in selected_raw if c.get("id") is not None}
            selected_vol_num = {(str(c.get("volume", "")), str(c.get("number", ""))) for c in selected_raw}
            # Bot passes volume="0" to mean "any volume" — match by chapter number alone.
            any_volume_numbers = {str(c.get("number", "")) for c in selected_raw if str(c.get("volume", "")) == "0"}
            filtered_chapters = [
                ch for ch in all_chapters
                if str(ch.get("id")) in selected_ids
                or (str(ch.get("volume", "")), str(ch.get("number", ""))) in selected_vol_num
                or (any_volume_numbers and str(ch.get("number", "")) in any_volume_numbers)
            ]
            chapters_data = filtered_chapters if filtered_chapters else all_chapters
        else:
            chapters_data = all_chapters

        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["total_chapters"] = len(chapters_data)

        fmt = str(body.get("format", "epub")).lower()
        profile = str(body.get("profile", "generic"))
        cover_enabled = bool(body.get("cover", True))
        images_enabled = bool(body.get("images", True))
        grayscale = bool(body.get("grayscale", False))
        compress = bool(body.get("compress", True))
        branch_id = body.get("branch_id")
        if branch_id == "" or branch_id == "default":
            branch_id = None
        elif branch_id:
            branch_id = str(branch_id)
            known_ids = {str(b.get("id")) for b in (novel_info.get("branches") or []) if isinstance(b, dict)}
            if known_ids and branch_id not in known_ids:
                raise ValueError("Unknown branch_id")

        selected_team_keys = body.get("selected_team_keys") or []
        selected_team_names = set()
        for key in selected_team_keys:
            if isinstance(key, str) and "::" in key:
                selected_team_names.add(_norm_team_name(key.split("::", 1)[0]))

        # If a team was selected but no branch, derive the branch from the team key.
        if not branch_id and selected_team_keys:
            for key in selected_team_keys:
                if isinstance(key, str) and "::" in key:
                    branch_id = key.split("::", 1)[1] or None
                    break

        # Deduplicate to one chapter per number, preferring selected team/branch.
        # A user asking for '1-50' gets exactly 50 chapters (no duplicate translations).
        if chapters_data:
            chapters_data = _dedup_chapters(chapters_data, selected_team_names, branch_id)

        settings.set("download_cover", cover_enabled)
        settings.set("download_images", images_enabled)
        settings.set("compress_images", compress)
        settings.set("grayscale_images", grayscale)
        settings.set("device_profile", profile)
        settings.set("save_directory", str(DOWNLOADS_DIR))

        creators_map = {
            "epub": EpubCreator,
            "fb2": Fb2Creator,
            "html": HtmlCreator,
            "txt": TxtCreator,
        }
        creator_cls = creators_map.get(fmt, EpubCreator)

        class WebProgressCreator(creator_cls):
            def _process_single_chapter(self, ch_data, novel_info, image_folder, upcoming_requests=0):
                res = super()._process_single_chapter(ch_data, novel_info, image_folder, upcoming_requests)
                with tasks_lock:
                    if task_id in tasks:
                        tasks[task_id]["processed_chapters"] += 1
                        total = tasks[task_id]["total_chapters"] or 1
                        current = tasks[task_id]["processed_chapters"]
                        tasks[task_id]["progress"] = min(95, int(5 + (current / total) * 90))

                return res

        image_handler = ImageHandler(api)
        creator = WebProgressCreator(api, parser, image_handler)

        split_mode = str(body.get("split_mode", "none")).lower()
        chunk_size = int(body.get("chunk_size", 150))

        if split_mode in ("volume", "chunk") and len(chapters_data) > 1:
            chapter_groups = []
            if split_mode == "volume":
                vol_map = {}
                for ch in chapters_data:
                    v = str(ch.get("volume", "0"))
                    vol_map.setdefault(v, []).append(ch)
                for v_name, ch_list in vol_map.items():
                    chapter_groups.append((f"Том {v_name}", ch_list))
            elif split_mode == "chunk":
                for i in range(0, len(chapters_data), chunk_size):
                    subset = chapters_data[i:i + chunk_size]
                    nums = [c.get("number") for c in subset if c.get("number") is not None]
                    label = f"Глав {nums[0]}-{nums[-1]}" if nums else f"Частина {i//chunk_size + 1}"
                    chapter_groups.append((label, subset))

            created_files = []
            for label, sub_chapters in chapter_groups:
                info_copy = novel_info.copy()
                title_orig = info_copy.get("rus_name") or info_copy.get("eng_name") or slug
                info_copy["rus_name"] = f"{title_orig} ({label})"
                fpath = creator.create(info_copy, sub_chapters, selected_branch_id=branch_id)
                created_files.append(os.path.basename(fpath))

            file_name = created_files[0] if created_files else ""
            file_list = created_files
        else:
            created_file_path = creator.create(novel_info, chapters_data, selected_branch_id=branch_id)
            file_name = os.path.basename(created_file_path)
            file_list = [file_name]

        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "done"
                tasks[task_id]["progress"] = 100
                tasks[task_id]["file"] = file_name
                tasks[task_id]["file_list"] = file_list
    except Exception as e:
        import traceback
        traceback.print_exc()
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["error"] = str(e)


api = RanobeLibAPI()
auth = RanobeLibAuth(api)
parser = RanobeLibParser(api)
api.set_token_refresh_callback(auth.refresh_token)

app = FastAPI(title="RanobeLIB Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _slug_from_url(url: str) -> str | None:
    from urllib.parse import urlparse
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "ru":
        if parts[1] == "book" and len(parts) >= 3:
            return parts[2]
        return parts[1]
    return None


def _title_from_novel_info(novel_info: dict) -> str:
    raw = novel_info.get("rus_name") or novel_info.get("eng_name") or novel_info.get("name") or ""
    return parser.decode_html_entities(raw)


@app.get("/api/health")
def health():
    return {"status": "ok", "time": time.time()}


@app.post("/api/load")
async def load_novel(request: Request):
    body = await request.json()
    url = (body or {}).get("url", "")
    slug = _slug_from_url(url)
    if not slug:
        raise HTTPException(400, "Неверный формат ссылки")

    novel_info = await asyncio.to_thread(api.get_novel_info, slug)
    if not isinstance(novel_info, dict):
        raise HTTPException(404, "Не удалось загрузить новеллу")

    novel_info = normalize_novel_info(novel_info)
    if not novel_info.get("id"):
        raise HTTPException(404, "Не удалось загрузить новеллу. Неверная ссылка или требуется авторизация.")

    # Build the full slug needed by the chapters/chapter API
    # Priority: slug_url field > {id}--{slug} > bare slug from URL
    novel_id = novel_info.get("id")
    novel_slug = novel_info.get("slug") or slug
    slug_url = novel_info.get("slug_url")
    if not slug_url:
        if novel_id and novel_slug and novel_slug != str(novel_id):
            slug_url = f"{novel_id}--{novel_slug}"
        else:
            slug_url = slug

    chapters_data = await asyncio.to_thread(api.get_novel_chapters, slug_url)
    if not chapters_data:
        if novel_info.get("is_licensed"):
            raise HTTPException(403, "Доступ ограничен.")
        raise HTTPException(404, "Не удалось загрузить список глав.")

    title = __import__("re").sub(
        r"\s*\((?:Новелла|Novel)\)\s*$", "", _title_from_novel_info(novel_info), flags=__import__("re").IGNORECASE
    ).strip()

    raw_branches = get_formatted_branches_with_teams(novel_info, chapters_data)
    branches = []
    if isinstance(raw_branches, dict):
        for b_id, b_info in raw_branches.items():
            b_item = dict(b_info)
            b_item["id"] = str(b_id)
            chapter_count = b_item.get("chapter_count", 0)
            branch_name = b_item.get("name", "Ветка #" + str(b_id))
            team_names = b_item.get("team_names", []) or []
            unique_teams = [t for t in team_names if t and t.strip() and t.strip() != branch_name]
            team_str = f" [{', '.join(unique_teams)}]" if unique_teams else ""
            count_str = f" ({chapter_count} глав)"
            b_item["label"] = f"{branch_name}{team_str}{count_str}"
            branches.append(b_item)
    elif isinstance(raw_branches, list):
        branches = raw_branches

    teams = []
    seen_team_keys = set()
    for ch in chapters_data:
        for branch in ch.get("branches", []) or []:
            branch_id = "0"
            team_name = None
            if isinstance(branch, dict):
                branch_id = str(branch.get("branch_id") if branch.get("branch_id") is not None else "0")
                team_info = branch.get("team") or {}
                team_name = team_info.get("name") if isinstance(team_info, dict) else None
                if not team_name:
                    teams_list = branch.get("teams") or []
                    if teams_list:
                        team_name = teams_list[0].get("name") if isinstance(teams_list[0], dict) else str(teams_list[0])
            elif branch is not None:
                branch_id = str(branch)

            if not team_name:
                continue

            key = (team_name, branch_id)
            if key in seen_team_keys:
                continue
            seen_team_keys.add(key)

            branch_label = next((b.get("label", "") for b in branches if b.get("id") == branch_id), "")
            teams.append({
                "name": team_name,
                "branch_id": branch_id,
                "branch_label": branch_label,
                "label": f"{team_name} ({branch_label})" if branch_label else team_name,
            })
    teams.sort(key=lambda x: x.get("label", ""))

    total_unique = get_unique_chapters_count(chapters_data)

    compact_chapters = []
    for ch in chapters_data:
        normalized_branches = []
        for branch in ch.get("branches", []) or []:
            branch_id = "0"
            team_names = []
            if isinstance(branch, dict):
                branch_id = str(branch.get("branch_id") if branch.get("branch_id") is not None else "0")
                teams_list = branch.get("teams") or []
                if teams_list:
                    for t in teams_list:
                        if isinstance(t, dict) and t.get("name"):
                            team_names.append(t["name"])
                if not team_names:
                    team_info = branch.get("team") or {}
                    if isinstance(team_info, dict) and team_info.get("name"):
                        team_names.append(team_info["name"])
            elif branch is not None:
                branch_id = str(branch)

            normalized_branches.append({
                "branch_id": branch_id,
                "team_names": team_names,
            })

        compact_chapters.append({
            "id": ch.get("id"),
            "volume": ch.get("volume"),
            "number": ch.get("number"),
            "name": ch.get("name"),
            "index": ch.get("index"),
            "branches": normalized_branches,
        })

    default_chapters_raw = get_default_branch_chapters(chapters_data)
    default_chapters = []
    for item in default_chapters_raw:
        ch = item.get("chapter", {})
        normalized_branches = []
        for branch in ch.get("branches", []) or []:
            branch_id = "0"
            team_names = []
            if isinstance(branch, dict):
                branch_id = str(branch.get("branch_id") if branch.get("branch_id") is not None else "0")
                teams_list = branch.get("teams") or []
                if teams_list:
                    for t in teams_list:
                        if isinstance(t, dict) and t.get("name"):
                            team_names.append(t["name"])
                if not team_names:
                    team_info = branch.get("team") or {}
                    if isinstance(team_info, dict) and team_info.get("name"):
                        team_names.append(team_info["name"])
            elif branch is not None:
                branch_id = str(branch)
            normalized_branches.append({
                "branch_id": branch_id,
                "team_names": team_names,
            })
        default_chapters.append({
            "id": ch.get("id"),
            "volume": ch.get("volume"),
            "number": ch.get("number"),
            "name": ch.get("name"),
            "index": ch.get("index"),
            "branches": normalized_branches,
        })

    return {
        "id": novel_info.get("id"),
        "slug": novel_slug,
        "slug_url": slug_url,
        "title": title,
        "author": (novel_info.get("authors") or [{}])[0].get("name", ""),
        "summary": novel_info.get("summary", ""),
        "cover": (novel_info.get("cover") or {}).get("default"),
        "status_id": novel_info.get("status_id"),
        "genres": [g.get("name") for g in (novel_info.get("genres") or []) if g.get("name")],
        "tags": [g.get("name") for g in (novel_info.get("tags") or []) if g.get("name")],
        "branches": branches,
        "chapters": compact_chapters,
        "default_chapters": default_chapters,
        "teams": teams,
        "total_unique_chapters": total_unique,
    }


@app.post("/api/download")
async def download_novel(request: Request):
    cleanup_old_files()
    body = await request.json()
    slug = body.get("slug")
    if not slug:
        raise HTTPException(400, "slug required")
    task_id = str(uuid.uuid4())
    _purge_old_tasks()
    with tasks_lock:
        tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "file": None,
            "error": None,
            "total_chapters": 0,
            "processed_chapters": 0,
            "created_at": time.time(),
        }
    executor.submit(run_download_task, task_id, body)
    return {"task_id": task_id}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return {
            "status": task["status"],
            "progress": task["progress"],
            "file": task["file"],
            "error": task["error"],
        }


@app.post("/api/chapter-content")
async def chapter_content(request: Request):
    """Return parsed chapter HTML content and metadata for preview."""
    body = await request.json()
    slug = body.get("slug")
    volume = body.get("volume")
    number = body.get("number")
    branch_id = body.get("branch_id")
    if not slug or volume is None or number is None:
        raise HTTPException(400, "slug, volume, number required")

    effective_branch = str(branch_id) if branch_id and str(branch_id) not in ("", "0", "default") else None
    data = await asyncio.to_thread(api.get_chapter_content, str(slug), str(volume), str(number), effective_branch)

    if not isinstance(data, dict):
        return {"volume": volume, "number": number, "name": "", "html": "<p>Не удалось загрузить содержимое главы</p>", "teams": []}

    html_str = ""
    content = data.get("content")
    if content:
        if isinstance(content, dict) and content.get("type") == "doc" and content.get("content"):
            attachments = data.get("attachments", [])
            html_str = parser.json_to_html(content["content"], attachments)
        elif isinstance(content, str):
            html_str = content
        else:
            html_str = str(content)

    raw_teams = data.get("teams") or []
    if not raw_teams and data.get("team"):
        raw_teams = [data["team"]]
    if not raw_teams and data.get("branches") and isinstance(data["branches"], list):
        for b in data["branches"]:
            if isinstance(b, dict):
                if b.get("teams"):
                    raw_teams.extend(b["teams"])
                elif b.get("team"):
                    raw_teams.append(b["team"])

    teams = []
    for t in raw_teams:
        if isinstance(t, dict):
            t_name = t.get("name") or t.get("username")
            if t_name and t_name not in teams:
                teams.append(t_name)
        elif isinstance(t, str) and t not in teams:
            teams.append(t)

    return {
        "volume": volume,
        "number": number,
        "name": data.get("name") or "",
        "html": html_str or "<p>Текст главы отсутствует</p>",
        "teams": teams,
    }


@app.post("/api/auth/token")
async def auth_token(request: Request):
    body = await request.json()
    code = body.get("code")
    secret = body.get("secret")
    redirect_uri = body.get("redirect_uri")
    if not all([code, secret, redirect_uri]):
        raise HTTPException(400, "code, secret, redirect_uri required")
    token_data = await asyncio.to_thread(auth._exchange_code_for_token, code, secret, redirect_uri)
    if not token_data:
        raise HTTPException(401, "Failed to exchange code")
    return {"access_token": token_data.get("access_token")}


@app.get("/api/files/{name}")
def get_file(name: str):
    path = DOWNLOADS_DIR / name
    if not path.resolve().is_relative_to(DOWNLOADS_DIR.resolve()):
        raise HTTPException(400, "Недопустимий шлях")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path, filename=name)


# Serve SPA
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


# Optional self-contained keep-alive thread to prevent free-tier sleep.
# Disabled by default; enable with KEEPALIVE=1 (no external cron needed).
def _start_keepalive_loop(interval_seconds: int = 600):
    import threading
    import urllib.request

    def _ping():
        # Point at our own health endpoint so the service stays warm.
        for attempt in range(3):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8080/api/health", timeout=10) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(2)

    def _loop():
        while True:
            time.sleep(interval_seconds)
            _ping()

    t = threading.Thread(target=_loop, daemon=True)
    t.start()


if os.environ.get("KEEPALIVE", "").strip() == "1":
    _start_keepalive_loop(int(os.environ.get("KEEPALIVE_INTERVAL", "600")))

