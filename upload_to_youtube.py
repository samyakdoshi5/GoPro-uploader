from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import google_auth_httplib2
import httplib2
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError, ResumableUploadError
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# Upload needs `youtube.upload`; playlist lookup/creation needs `youtube.force-ssl`.
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
DATE_FOLDER_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\s+-\s+(?P<title>.+)$")
IGNORED_FOLDERS_FILE = "ignored_folders.txt"
LAST_STATUS_LEN = 0
DEFAULT_UPLOAD_CHUNK_MB = 16
DEFAULT_HTTP_TIMEOUT_SECONDS = 120
DEFAULT_UPLOAD_RETRIES = 5
RATE_LIMIT_RETRY_SECONDS = 60 * 30
RETRYABLE_UPLOAD_EXCEPTIONS = (
    httplib2.HttpLib2Error,
    ConnectionError,
    TimeoutError,
    ssl.SSLError,
)


@dataclass(frozen=True)
class FolderInfo:
    path: Path
    folder_date: datetime
    display_date: str
    title: str


@dataclass(frozen=True)
class UploadItem:
    folder: FolderInfo
    index: int
    path: Path
    title: str


@dataclass(frozen=True)
class FolderPlan:
    folder: FolderInfo
    playlist_title: str
    items: list[UploadItem]


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp_path.replace(path)


def ensure_state_shape(state: dict) -> dict:
    if "uploaded" in state and "files" not in state:
        state["files"] = state.pop("uploaded")
    state.setdefault("files", {})
    state.setdefault("playlists", {})
    return state


def find_client_secret(uploader_dir: Path) -> Path:
    candidates = sorted(uploader_dir.glob("client_secret*.json"))
    if not candidates:
        raise FileNotFoundError(
            f"No client_secret*.json found in {uploader_dir}. Put your Google OAuth client file there."
        )
    return candidates[0]


def validate_client_secret(client_secret: Path) -> None:
    data = load_json(client_secret, {})
    if "installed" in data:
        return
    if "web" in data:
        raise ValueError(
            "The OAuth JSON is a web client, but this uploader expects a desktop/installed app client.\n"
            "In Google Cloud Console, create an OAuth client of type 'Desktop app', download that JSON,\n"
            f"and replace {client_secret.name} with the new file."
        )
    raise ValueError(
        f"Unrecognized OAuth JSON format in {client_secret}. Expected an 'installed' desktop client."
    )


def get_credentials(uploader_dir: Path) -> Credentials:
    token_path = uploader_dir / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        try:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                raise RefreshError("No usable refresh token")
        except RefreshError:
            if token_path.exists():
                token_path.unlink()
            client_secret = find_client_secret(uploader_dir)
            validate_client_secret(client_secret)
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        save_json(token_path, json.loads(creds.to_json()))

    return creds


def auth_bootstrap(uploader_dir: Path) -> None:
    creds = get_credentials(uploader_dir)
    print("Authentication saved locally.")
    print(f"Token file: {uploader_dir / 'token.json'}")
    print(f"Scopes: {', '.join(SCOPES)}")
    if creds.expired:
        print("Note: token existed but was expired and has been refreshed.")


def get_file_state(state: dict, rel_key: str) -> dict:
    return state.setdefault("files", {}).setdefault(rel_key, {})


def update_file_state(state: dict, state_path: Path, rel_key: str, **fields) -> None:
    file_state = get_file_state(state, rel_key)
    file_state.update(fields)
    file_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_json(state_path, state)


def remove_file_state(state: dict, state_path: Path, rel_key: str) -> None:
    if rel_key in state.get("files", {}):
        state["files"].pop(rel_key, None)
        save_json(state_path, state)


def parse_folder(folder: Path) -> Optional[FolderInfo]:
    match = DATE_FOLDER_RE.match(folder.name)
    if not match:
        return None
    folder_date = datetime.strptime(match.group("date"), "%Y-%m-%d")
    display_date = folder_date.strftime("%d/%m/%y")
    title = match.group("title").strip()
    return FolderInfo(path=folder, folder_date=folder_date, display_date=display_date, title=title)


def normalize_title(title: str) -> str:
    return re.sub(r"\bw\b", "w/", title).replace("w//", "w/")


def natural_key(path: Path):
    parts = re.split(r"(\d+)", path.name.lower())
    key = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return key


def iter_video_files(folder: Path) -> list[Path]:
    files = []
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            files.append(path)
    return sorted(files, key=natural_key)


def format_title(index: int, folder_info: FolderInfo) -> str:
    return f"{index:03d} || {folder_info.display_date} || {normalize_title(folder_info.title)}"


def format_playlist_title(folder_info: FolderInfo) -> str:
    return f"{folder_info.display_date} - {normalize_title(folder_info.title)}"


def build_youtube_client(creds: Credentials, timeout_seconds: int):
    http = httplib2.Http(timeout=timeout_seconds)
    # YouTube resumable uploads use HTTP 308 as a progress response, not a redirect.
    http.redirect_codes = frozenset(code for code in http.redirect_codes if code != 308)
    authorized_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
    return build("youtube", "v3", http=authorized_http)


def retry_delay_seconds(attempt: int) -> int:
    return min(2**attempt, 60)


def format_upload_retry_error(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror:
        return f"{type(exc).__name__} [WinError {winerror}]: {exc}"
    return f"{type(exc).__name__}: {exc}"


def is_rate_limit_error(exc: HttpError) -> bool:
    message = str(exc).lower()
    return exc.resp.status == 429 or "rate_limit_exceeded" in message


def execute_with_rate_limit_retry(request):
    while True:
        try:
            return request.execute()
        except HttpError as exc:
            if not is_rate_limit_error(exc):
                raise
            print("rate limit exceeded")
            time.sleep(RATE_LIMIT_RETRY_SECONDS)


def fetch_channel_uploaded_titles(youtube) -> set[str]:
    channels_response = youtube.channels().list(
        part="contentDetails",
        mine=True,
        maxResults=1,
    )
    channels_response = execute_with_rate_limit_retry(channels_response)
    items = channels_response.get("items", [])
    if not items:
        return set()

    uploads_playlist_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads_playlist_id:
        return set()

    titles: set[str] = set()
    request = youtube.playlistItems().list(
        part="snippet",
        playlistId=uploads_playlist_id,
        maxResults=50,
    )
    while request is not None:
        response = execute_with_rate_limit_retry(request)
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            title = snippet.get("title")
            if title:
                titles.add(title.strip())
        request = youtube.playlistItems().list_next(request, response)
    return titles


def fetch_channel_uploaded_videos(youtube) -> dict[str, str]:
    channels_response = youtube.channels().list(
        part="contentDetails",
        mine=True,
        maxResults=1,
    )
    channels_response = execute_with_rate_limit_retry(channels_response)
    items = channels_response.get("items", [])
    if not items:
        return {}

    uploads_playlist_id = items[0].get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    if not uploads_playlist_id:
        return {}

    existing: dict[str, str] = {}
    request = youtube.playlistItems().list(
        part="snippet",
        playlistId=uploads_playlist_id,
        maxResults=50,
    )
    while request is not None:
        response = execute_with_rate_limit_retry(request)
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            title = snippet.get("title")
            video_id = snippet.get("resourceId", {}).get("videoId")
            if title and video_id:
                existing[title.strip()] = video_id
        request = youtube.playlistItems().list_next(request, response)
    return existing


def ensure_playlist(youtube, playlists_cache: dict[str, str], title: str) -> str:
    if title in playlists_cache:
        return playlists_cache[title]

    request = youtube.playlists().list(
        part="id,snippet,status",
        mine=True,
        maxResults=50,
    )
    while request is not None:
        response = execute_with_rate_limit_retry(request)
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            status = item.get("status", {})
            if snippet.get("title") == title and status.get("privacyStatus") == "unlisted":
                playlists_cache[title] = item["id"]
                return item["id"]
        request = youtube.playlists().list_next(request, response)

    created = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": f"Auto-created playlist for {title}",
            },
            "status": {"privacyStatus": "unlisted"},
        },
    )
    created = execute_with_rate_limit_retry(created)
    playlists_cache[title] = created["id"]
    return created["id"]


def upload_video(
    youtube,
    file_path: Path,
    title: str,
    chunk_size_mb: int,
    retries: int,
) -> str:
    chunk_size = chunk_size_mb * 1024 * 1024
    media = MediaFileUpload(str(file_path), chunksize=chunk_size, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": f"Uploaded from {file_path.parent}",
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": "unlisted",
            },
        },
        media_body=media,
    )

    response = None
    start_time = time.monotonic()
    last_percent = -1
    try:
        while response is None:
            for attempt in range(retries + 1):
                try:
                    status, response = request.next_chunk(num_retries=retries)
                    break
                except RETRYABLE_UPLOAD_EXCEPTIONS as exc:
                    if attempt >= retries:
                        raise
                    delay = retry_delay_seconds(attempt)
                    write_status_line(
                        f"Upload connection error: {format_upload_retry_error(exc)}. Retrying in {delay}s "
                        f"({attempt + 1}/{retries})..."
                    )
                    time.sleep(delay)
            if status:
                total = status.total_size or 0
                current = status.resumable_progress or 0
                if total:
                    percent = int(current * 100 / total)
                    if percent != last_percent:
                        elapsed = max(time.monotonic() - start_time, 0.001)
                        speed = current / elapsed
                        remaining = max(total - current, 0)
                        eta_seconds = int(remaining / speed) if speed > 0 else 0
                        write_status_line(
                            format_progress_line(
                                file_path.name,
                                current,
                                total,
                                percent,
                                speed,
                                eta_seconds,
                            )
                        )
                        last_percent = percent
                else:
                    write_status_line(f"Progress: {human_size(current)} uploaded")
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise
    except Exception:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise

    write_status_line(f"Progress: {file_path.name} complete")
    sys.stdout.write("\n")
    sys.stdout.flush()

    return response["id"]


def format_eta(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:d}:{seconds:02d}"


def truncate_middle(text: str, max_length: int) -> str:
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    left = (max_length - 3) // 2
    right = max_length - 3 - left
    return f"{text[:left]}...{text[-right:]}"


def format_progress_line(
    filename: str,
    current: int,
    total: int,
    percent: int,
    speed_bytes_per_sec: float,
    eta_seconds: int,
) -> str:
    terminal_width = shutil.get_terminal_size(fallback=(120, 20)).columns
    prefix = (
        f"Progress: {human_size(current)} / {human_size(total)}"
        f" | {percent:3d}%"
        f" | {human_size(int(speed_bytes_per_sec))}/s"
        f" | ETA {format_eta(eta_seconds)}"
        f" | "
    )
    filename_width = max(10, terminal_width - len(prefix) - 1)
    return f"{prefix}{truncate_middle(filename, filename_width)}"


def write_status_line(text: str) -> None:
    global LAST_STATUS_LEN
    clear_width = max(LAST_STATUS_LEN, len(text))
    sys.stdout.write("\r" + (" " * clear_width) + "\r")
    sys.stdout.write(text)
    sys.stdout.flush()
    LAST_STATUS_LEN = len(text)


def add_video_to_playlist(youtube, playlist_id: str, video_id: str) -> None:
    request = youtube.playlistItems().insert(
        part="snippet",
        body={
            "snippet": {
                "playlistId": playlist_id,
                "resourceId": {
                    "kind": "youtube#video",
                    "videoId": video_id,
                },
            }
        },
    )
    execute_with_rate_limit_retry(request)


def discover_date_folders(root: Path) -> list[FolderInfo]:
    folders = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir():
            info = parse_folder(child)
            if info is not None:
                folders.append(info)
    return folders


def load_ignored_folders(uploader_dir: Path) -> set[str]:
    ignore_path = uploader_dir / IGNORED_FOLDERS_FILE
    if not ignore_path.exists():
        return set()

    ignored: set[str] = set()
    for raw_line in ignore_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ignored.add(line)
    return ignored


def build_upload_plan(
    folders: list[FolderInfo],
    uploaded: dict[str, object],
) -> list[FolderPlan]:
    plan: list[FolderPlan] = []
    for folder_info in folders:
        videos = iter_video_files(folder_info.path)
        pending: list[UploadItem] = []
        for index, video_path in enumerate(videos, start=1):
            rel_key = str(video_path.resolve())
            title = format_title(index, folder_info)
            file_state = uploaded.get(rel_key, {})
            status = file_state.get("status")
            if status == "playlist_added":
                continue
            pending.append(
                UploadItem(
                    folder=folder_info,
                    index=index,
                    path=video_path,
                    title=title,
                )
            )
        if pending:
            plan.append(
                FolderPlan(
                    folder=folder_info,
                    playlist_title=format_playlist_title(folder_info),
                    items=pending,
                )
            )
    return plan


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{num_bytes} B"


def print_plan(plan: list[FolderPlan]) -> None:
    total_files = sum(len(folder.items) for folder in plan)
    total_bytes = sum(item.path.stat().st_size for folder in plan for item in folder.items)
    print(f"Planned uploads: {total_files} file(s), {human_size(total_bytes)} total")
    print()
    for folder_plan in plan:
        print(f"[Folder] {folder_plan.folder.path.name}")
        print(f"  Playlist: {folder_plan.playlist_title}")
        for item in folder_plan.items:
            print(
                f"  {item.index:03d} | {item.path.name} | {human_size(item.path.stat().st_size)} | {item.title}"
            )
        print()


def print_state_summary(state: dict) -> None:
    files = state.get("files", {})
    counts = {
        "planned": 0,
        "uploading": 0,
        "uploaded": 0,
        "playlist_added": 0,
    }
    for item in files.values():
        status = item.get("status", "planned")
        if status in counts:
            counts[status] += 1
    print(
        "State: "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
    )


def confirm(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no", ""}:
            return False
        print("Please answer y or n.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review GoPro footage, then upload selected files to YouTube as unlisted videos and organize them into playlists."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Folder that contains the date folders, e.g. F:\\Media\\GoPro Hero 7",
    )
    parser.add_argument(
        "--folder",
        default=None,
        help="Optional single date folder name to process, e.g. 2026-05-08 - Kanchangiri w Juniors",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without uploading anything.")
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Delete the saved token first so Google asks for the expanded playlist scope again.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation step and upload immediately.",
    )
    parser.add_argument(
        "--auth-only",
        action="store_true",
        help="Only perform Google sign-in and save the local token, then exit.",
    )
    parser.add_argument(
        "--upload-chunk-mb",
        type=int,
        default=DEFAULT_UPLOAD_CHUNK_MB,
        help=f"Upload chunk size in MB. Smaller chunks show progress more often. Default: {DEFAULT_UPLOAD_CHUNK_MB}.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_HTTP_TIMEOUT_SECONDS,
        help=f"Seconds to wait on a stalled Google API socket before failing/retrying. Default: {DEFAULT_HTTP_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--upload-retries",
        type=int,
        default=DEFAULT_UPLOAD_RETRIES,
        help=f"Retries per upload chunk for transient errors. Default: {DEFAULT_UPLOAD_RETRIES}.",
    )
    args = parser.parse_args()

    root = Path(args.root)
    uploader_dir = Path(__file__).resolve().parent
    state_path = uploader_dir / "state.json"
    token_path = uploader_dir / "token.json"
    state = ensure_state_shape(load_json(state_path, {"files": {}, "playlists": {}}))
    uploaded = state.setdefault("files", {})
    playlists_cache = state.setdefault("playlists", {})

    if args.reauth and token_path.exists():
        token_path.unlink()
        print("Removed saved token.json so the next sign-in can request the playlist scope again.")

    if args.auth_only:
        auth_bootstrap(uploader_dir)
        return 0

    if args.upload_chunk_mb <= 0:
        print("--upload-chunk-mb must be greater than 0.")
        return 1
    if args.request_timeout <= 0:
        print("--request-timeout must be greater than 0.")
        return 1
    if args.upload_retries < 0:
        print("--upload-retries cannot be negative.")
        return 1

    folders = discover_date_folders(root)
    if args.folder:
        folders = [folder for folder in folders if folder.path.name == args.folder]

    ignored_folders = load_ignored_folders(uploader_dir)
    if ignored_folders:
        folders = [folder for folder in folders if folder.path.name not in ignored_folders]

    if not folders:
        print(f"No date folders found under {root}")
        return 1

    creds = get_credentials(uploader_dir)
    youtube = build_youtube_client(creds, args.request_timeout)

    plan = build_upload_plan(folders, uploaded)
    if not plan:
        print("Nothing new to upload after checking your channel.")
        return 0

    print_state_summary(state)
    print_plan(plan)
    if not args.yes and not args.dry_run:
        if not confirm("Proceed with these uploads? [y/N]: "):
            print("Cancelled. No files were uploaded.")
            return 0

    if args.dry_run:
        print("Dry run complete. No files were uploaded.")
        return 0

    for folder_plan in plan:
        playlist_id = ensure_playlist(youtube, playlists_cache, folder_plan.playlist_title)
        print(f"Playlist ready: {folder_plan.playlist_title} -> {playlist_id}")

        for item in folder_plan.items:
            rel_key = str(item.path.resolve())
            file_state = get_file_state(state, rel_key)
            existing_status = file_state.get("status")
            existing_video_id = file_state.get("video_id")

            if existing_status == "playlist_added":
                print(f"Skipping already completed: {item.path.name}")
                continue

            if existing_status == "uploaded" and existing_video_id:
                print(f"Adding existing upload to playlist: {item.path.name}")
                add_video_to_playlist(youtube, playlist_id, existing_video_id)
                update_file_state(
                    state,
                    state_path,
                    rel_key,
                    status="playlist_added",
                    video_id=existing_video_id,
                    playlist_id=playlist_id,
                    title=item.title,
                )
                print(f"Playlist updated for {existing_video_id}")
                continue

            print(f"Uploading {item.path.name} as {item.title}")
            if existing_status == "uploading":
                print("Previous run stopped mid-upload; restarting this file from the beginning.")
            update_file_state(
                state,
                state_path,
                rel_key,
                status="uploading",
                title=item.title,
                playlist_id=playlist_id,
                path=str(item.path),
            )
            try:
                video_id = upload_video(
                    youtube,
                    item.path,
                    item.title,
                    args.upload_chunk_mb,
                    args.upload_retries,
                )
            except (HttpError, ResumableUploadError) as exc:
                message = str(exc)
                if "Video Uploads per day" in message or "quota exceeded" in message.lower():
                    remove_file_state(state, state_path, rel_key)
                    print("\nUpload quota was exceeded for today. Nothing was uploaded for this file.")
                    print("The file has been returned to the queue, so you can retry later.")
                    return 1
                raise
            except RETRYABLE_UPLOAD_EXCEPTIONS as exc:
                print(f"\nUpload stopped after connection retries: {format_upload_retry_error(exc)}")
                print("The current file is still marked as 'uploading'; rerun the script to retry it.")
                return 1
            except KeyboardInterrupt:
                print("\nUpload interrupted. Current file state was saved as 'uploading'.")
                raise
            add_video_to_playlist(youtube, playlist_id, video_id)
            update_file_state(
                state,
                state_path,
                rel_key,
                status="playlist_added",
                video_id=video_id,
                playlist_id=playlist_id,
                title=item.title,
                uploaded_at=datetime.now().isoformat(timespec="seconds"),
            )
            print(f"Uploaded {video_id}")

    save_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
