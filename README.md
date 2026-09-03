# YT Uploader

This folder contains a small uploader that:

- scans date folders named like `2026-05-08 - Kanchangiri w Juniors`
- numbers clips inside each folder starting at `001`
- uploads each clip to YouTube as `unlisted`
- creates or reuses a playlist for the folder title
- adds the uploaded video to that playlist

## Files

- `client_secret_*.json` - your Google OAuth client file
- `upload_to_youtube.py` - the uploader script
- `ignored_folders.txt` - folder names to skip when planning uploads
- `requirements.txt` - Python dependencies
- `state.json` - created automatically after uploads
- `token.json` - created automatically after first login

## Setup

1. Install the Python dependencies:

   ```powershell
   pip install -r "F:\Media\GoPro Hero 7\YT Uploader\requirements.txt"
   ```

2. In Google Cloud Console:

   - enable `YouTube Data API v3`
   - create an OAuth client for a **Desktop app**
   - download the JSON file into this `YT Uploader` folder
   - make sure the downloaded JSON contains an `installed` block, not `web`

3. Run the uploader:

   ```powershell
   python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --root "F:\Media\GoPro Hero 7"
   ```

The script will first print every planned upload and ask for confirmation before anything is uploaded.

If you already authenticated before adding playlist support, run once with:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --root "F:\Media\GoPro Hero 7" --reauth
```

That removes the old `token.json` so Google can issue a new token with playlist permissions.

If you want to do auth separately from uploads, run:

```powershell
python "F:\Media\GoPro Hero 7\YT Uploader\upload_to_youtube.py" --auth-only
```

After that, every normal run reuses the saved local token automatically.

## Title format

The script generates titles like:

`035 || 08/05/26 || Kanchangiri w Juniors`

The number is assigned in file order inside that date folder, starting from `001`.

## Notes

- Nothing is deleted or moved.
- The script skips files already listed in `state.json`.
- Add exact folder names to `ignored_folders.txt` to keep them out of the upload queue.
- Playlists are also created as `unlisted`.
- Use `--yes` only if you want to skip the confirmation prompt.
