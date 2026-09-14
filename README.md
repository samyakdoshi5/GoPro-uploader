# GoPro YouTube Uploader

This project uploads GoPro footage from a dated folder structure into YouTube as unlisted videos and organizes each date group into a matching unlisted playlist.

It is designed for a folder layout like this:

```text
F:\Media\GoPro Hero 7
├── 2026-05-08 - Kanchangiri w Juniors
│   ├── GH010123.MP4
│   ├── GH010124.MP4
│   └── GH010125.MP4
├── 2026-05-09 - Terrace Session
│   └── ...
└── ...
```

The script scans each dated folder, assigns a numeric title such as `001 || 08/05/26 || Kanchangiri w Juniors`, and uploads each clip to the connected YouTube channel.

## Features

- Finds date folders matching `YYYY-MM-DD - Label`
- Sorts clips in natural filename order
- Uploads each file as an unlisted video
- Creates or reuses an unlisted playlist named after the folder title
- Tracks completed uploads in `state.json`
- Can skip a folder via `ignored_folders.txt`
- Supports dry-run and authentication-only modes

## Requirements

- Python 3.9+
- A Google Cloud project with the YouTube Data API v3 enabled
- A desktop OAuth client JSON from Google Cloud Console

## Files in this folder

- `upload_to_youtube.py` - main uploader script
- `requirements.txt` - Python dependencies
- `client_secret_*.json` - Google OAuth desktop app credentials
- `token.json` - saved OAuth token after login
- `state.json` - records upload status and playlist metadata
- `ignored_folders.txt` - exact folder names to skip

## Install dependencies

```powershell
pip install -r "F:\Media\GoPro Hero 7\YT Uploader\requirements.txt"
```

## Google authentication

1. Open Google Cloud Console.
2. Enable the YouTube Data API v3.
3. Create an OAuth client of type Desktop app.
4. Download the JSON file into this folder.
5. Ensure the file contains an `installed` section, not a `web` section.

The file name should start with `client_secret` and end with `.json`.

## Basic usage

Preview the upload plan without uploading:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --root "F:\Media\GoPro Hero 7" --dry-run
```

Authenticate only and save a token locally:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --auth-only
```

Run the uploader normally:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --root "F:\Media\GoPro Hero 7"
```

The script will print a planned upload summary, then ask for confirmation before uploading.

## Useful flags

Skip the confirmation prompt:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --yes
```

Process only one specific date folder:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --folder "2026-05-08 - Kanchangiri w Juniors"
```

Force a fresh Google sign-in if permissions changed:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --reauth --auth-only
```

Adjust resumable upload behavior:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --upload-chunk-mb 8 --request-timeout 120 --upload-retries 5
```

## Title format

The uploader builds titles such as:

```text
035 || 08/05/26 || Kanchangiri w Juniors
```

The number starts at `001` within each date folder, and the date is taken from the folder name.

## Playlist behavior

Each folder becomes a YouTube playlist with a title like:

```text
08/05/26 - Kanchangiri w Juniors
```

All matching uploads in that folder are added to that playlist as unlisted entries.

## Ignoring folders

If you want to exclude a date folder from the upload queue, add its exact folder name to `ignored_folders.txt`.

Example:

```text
2026-05-09 - Terrace Session
```

## Notes

- The script does not move or delete videos.
- It skips files already marked as uploaded in `state.json`.
- It only processes valid video extensions: `.mp4`, `.mov`, `.m4v`, `.avi`, and `.mkv`.
- A dry run is the safest way to review the queue before uploading.

## Troubleshooting

If Google refuses to authenticate, remove the token and re-run the login flow:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --reauth
```

If you hit rate limits or upload connection issues, rerun the script. The script saves the current file state so interrupted uploads can be resumed cleanly.
