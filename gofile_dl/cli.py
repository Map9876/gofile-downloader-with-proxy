#!/usr/bin/env python3
"""GoFile Downloader - Simple CLI tool to download files from GoFile.

Usage:
    gofile-dl <gofile_url> [options]

Examples:
    gofile-dl https://gofile.io/d/5tkZZi
    gofile-dl https://gofile.io/d/5tkZZi --password MyPass
    gofile-dl https://gofile.io/d/5tkZZi --proxy https://your-worker.example.com/
    gofile-dl https://gofile.io/d/5tkZZi -o /path/to/save
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import time
from urllib.parse import urlparse

import requests

from . import __version__

# ============================
# Constants
# ============================
GOFILE_API = "https://api.gofile.io"
GOFILE_API_ACCOUNTS = f"{GOFILE_API}/accounts"
MAX_WORKERS = 3
CHUNK_SIZE = 256 * 1024  # 256KB for faster throughput
USER_AGENT = "Mozilla/5.0"
MAX_RETRIES = 20  # max retry attempts per file on connection error
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip",
    "Accept": "*/*",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}

# Thread lock for terminal output
_print_lock = threading.Lock()


def log(msg: str) -> None:
    """Thread-safe print for status messages."""
    with _print_lock:
        print(msg, flush=True)


def format_size(size_bytes: float) -> str:
    """Format bytes into human-readable size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}PB"


def apply_proxy(url: str, proxy: str | None) -> str:
    """Prepend proxy URL if configured."""
    if proxy:
        return f"{proxy.rstrip('/')}/{url}"
    return url


def get_account_token(proxy: str | None = None) -> str:
    """Create a guest account and return the access token."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }
    url = apply_proxy(GOFILE_API_ACCOUNTS, proxy)
    resp = requests.post(url, headers=headers, timeout=15).json()
    if resp["status"] != "ok":
        log(f"[ERROR] Account creation failed: {resp}")
        sys.exit(1)
    token = resp["data"]["token"]
    log("[INFO] Guest account token obtained")
    return token


def generate_website_token(account_token: str) -> str:
    """Generate the dynamic X-Website-Token header value."""
    time_window = str(int(time()) // 14400)
    token_seed = f"{USER_AGENT}::en-US::{account_token}::{time_window}::5d4f7g8sd45fsd"
    return hashlib.sha256(token_seed.encode()).hexdigest()


def get_content_id(url: str) -> str:
    """Extract content ID from a GoFile URL."""
    parts = url.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != "d":
        log(f"[ERROR] Invalid GoFile URL: {url}")
        sys.exit(1)
    return parts[-1]


def build_content_api_url(content_id: str, password: str | None = None) -> str:
    """Build the GoFile content API URL."""
    base = f"{GOFILE_API}/contents/{content_id}?cache=true&sortField=createTime&sortDirection=1"
    if password:
        base += f"&password={password}"
    return base


def fetch_content_info(
    content_id: str,
    account_token: str,
    website_token: str,
    password: str | None = None,
    proxy: str | None = None,
) -> dict:
    """Fetch content info from GoFile API."""
    api_url = build_content_api_url(content_id, password)
    proxied_url = apply_proxy(api_url, proxy)

    headers = dict(BASE_HEADERS)
    headers["Authorization"] = f"Bearer {account_token}"
    headers["X-Website-Token"] = website_token
    headers["X-BL"] = "en-US"
    headers["Cookie"] = f"accountToken={account_token}"

    resp = requests.get(proxied_url, headers=headers, timeout=15).json()

    if resp["status"] != "ok":
        log(f"[ERROR] API request failed: {resp.get('status', 'unknown')}")
        sys.exit(1)

    data = resp["data"]

    if "password" in data and data.get("passwordStatus") != "passwordOk":
        log("[ERROR] This URL requires a password. Use --password to provide it.")
        sys.exit(1)

    return data


def collect_files(data: dict, base_path: Path, password: str | None = None) -> list[dict]:
    """Recursively collect all files from the content data."""
    files = []

    if data["type"] == "folder":
        folder_path = base_path / data["name"]
        for child_id in data.get("children", {}):
            child = data["children"][child_id]
            if child["type"] == "folder":
                files.extend(collect_files(child, folder_path, password))
            else:
                files.append({
                    "filename": child["name"],
                    "download_link": child["link"],
                    "save_path": folder_path,
                })
    else:
        files.append({
            "filename": data["name"],
            "download_link": data["link"],
            "save_path": base_path,
        })

    return files


def refresh_download_link(
    content_id: str,
    filename: str,
    password: str | None,
    proxy: str | None,
) -> str | None:
    """Get a fresh download link for a specific file by re-querying the API."""
    try:
        new_token = get_account_token(proxy)
        new_website_token = generate_website_token(new_token)
        data = fetch_content_info(content_id, new_token, new_website_token, password, proxy)

        def find_link(d: dict) -> str | None:
            if d["type"] == "folder":
                for child_id in d.get("children", {}):
                    child = d["children"][child_id]
                    if child["type"] == "folder":
                        result = find_link(child)
                        if result:
                            return result
                    elif child["name"] == filename:
                        return child["link"]
            elif d.get("name") == filename:
                return d.get("link")
            return None

        return find_link(data)
    except Exception as e:
        log(f"  [WARN] Failed to refresh download link: {e}")
        return None


def download_file_with_resume(
    file_info: dict,
    account_token: str,
    proxy: str | None = None,
    content_id: str | None = None,
    hashed_password: str | None = None,
) -> bool:
    """Download a single file with resume support and auto-retry."""
    filename = file_info["filename"]
    save_path = file_info["save_path"]
    download_link = file_info["download_link"]
    final_path = save_path / filename

    save_path.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, MAX_RETRIES + 1):
        if final_path.exists() and final_path.stat().st_size > 0:
            file_size_on_disk = final_path.stat().st_size
            try:
                proxied_url = apply_proxy(download_link, proxy)
                head_headers = dict(BASE_HEADERS)
                head_headers["Cookie"] = f"accountToken={account_token}"
                parsed = urlparse(download_link)
                origin = f"{parsed.scheme}://{parsed.netloc}"
                head_headers["Referer"] = origin + "/"
                head_headers["Origin"] = origin
                head_resp = requests.head(proxied_url, headers=head_headers, timeout=10)
                remote_size = int(head_resp.headers.get("Content-Length", 0))
                if remote_size > 0 and file_size_on_disk == remote_size:
                    log(f"  [SKIP] {filename} (already complete)")
                    return True
                elif remote_size > 0 and file_size_on_disk > remote_size:
                    log(f"  [WARN] {filename} on disk ({format_size(file_size_on_disk)}) > remote ({format_size(remote_size)}), restarting")
                    final_path.unlink()
            except Exception:
                pass

        resume_from = 0
        if final_path.exists():
            resume_from = final_path.stat().st_size
            if resume_from > 0:
                log(f"  [RESUME] {filename} from {format_size(resume_from)}")

        proxied_url = apply_proxy(download_link, proxy)

        headers = dict(BASE_HEADERS)
        headers["Cookie"] = f"accountToken={account_token}"
        parsed = urlparse(download_link)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        headers["Referer"] = origin + "/"
        headers["Origin"] = origin

        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            response = requests.get(proxied_url, headers=headers, stream=True, timeout=(30, 300))

            if response.status_code == 416:
                log(f"  [DONE] {filename} (already complete)")
                return True

            if response.status_code not in (200, 206):
                log(f"  [ERROR] {filename}: HTTP {response.status_code} (attempt {attempt}/{MAX_RETRIES})")
                if attempt < MAX_RETRIES:
                    if content_id:
                        new_link = refresh_download_link(content_id, filename, hashed_password, proxy)
                        if new_link:
                            download_link = new_link
                            file_info["download_link"] = new_link
                    continue
                return False

            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    total_size = int(content_range.split("/")[1])
                else:
                    total_size = resume_from + int(response.headers.get("Content-Length", 0))
            else:
                total_size = int(response.headers.get("Content-Length", -1))
                resume_from = 0

            size_str = format_size(total_size) if total_size > 0 else "unknown size"

            downloaded = resume_from
            last_pct = (downloaded * 100 / total_size) if total_size > 0 else -1.0

            mode = "ab" if resume_from > 0 and response.status_code == 206 else "wb"
            with open(final_path, mode) as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = downloaded * 100 / total_size
                            if pct - last_pct >= 1.0 or downloaded >= total_size:
                                last_pct = pct
                                bar_len = 30
                                filled = int(bar_len * downloaded / total_size)
                                bar = "█" * filled + "░" * (bar_len - filled)
                                dl_str = format_size(downloaded)
                                with _print_lock:
                                    print(
                                        f"\r  {filename} [{bar}] {pct:5.1f}% ({dl_str}/{size_str})",
                                        end="", flush=True,
                                    )

            with _print_lock:
                print()

            if total_size > 0 and final_path.exists():
                actual_size = final_path.stat().st_size
                if actual_size >= total_size:
                    log(f"  [DONE] {filename} ({format_size(actual_size)})")
                    return True
                else:
                    log(f"  [WARN] {filename}: downloaded {format_size(actual_size)} < expected {format_size(total_size)}, will retry")
            else:
                return True

        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                ConnectionResetError,
                BrokenPipeError) as e:
            log(f"  [RETRY] {filename}: connection error on attempt {attempt}/{MAX_RETRIES}: {e}")
            if attempt < MAX_RETRIES:
                if content_id:
                    new_link = refresh_download_link(content_id, filename, hashed_password, proxy)
                    if new_link:
                        download_link = new_link
                        file_info["download_link"] = new_link
                continue
            else:
                log(f"  [FAIL] {filename}: max retries exceeded")
                return False

        except Exception as e:
            log(f"  [ERROR] {filename}: {e}")
            if attempt < MAX_RETRIES:
                if content_id:
                    new_link = refresh_download_link(content_id, filename, hashed_password, proxy)
                    if new_link:
                        download_link = new_link
                        file_info["download_link"] = new_link
                continue
            return False

    return False


def download(url: str, args: argparse.Namespace) -> None:
    """Main download logic."""
    proxy = args.proxy
    output_dir = Path(args.output) if args.output else Path.cwd() / "Downloads"
    output_dir.mkdir(parents=True, exist_ok=True)

    account_token = get_account_token(proxy)
    website_token = generate_website_token(account_token)

    content_id = get_content_id(url)
    log(f"[INFO] Content ID: {content_id}")

    hashed_password = None
    if args.password:
        hashed_password = hashlib.sha256(args.password.encode()).hexdigest()

    data = fetch_content_info(content_id, account_token, website_token, hashed_password, proxy)

    content_dir = output_dir / content_id
    files = collect_files(data, content_dir, hashed_password)

    if not files:
        log("[WARN] No files found to download.")
        return

    log(f"[INFO] Found {len(files)} file(s) to download")

    workers = getattr(args, 'workers', MAX_WORKERS) or MAX_WORKERS
    if args.sequential or len(files) == 1:
        for f in files:
            download_file_with_resume(
                f, account_token, proxy,
                content_id=content_id,
                hashed_password=hashed_password,
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    download_file_with_resume,
                    f, account_token, proxy,
                    content_id=content_id,
                    hashed_password=hashed_password,
                ): f
                for f in files
            }
            for future in as_completed(futures):
                future.result()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gofile-dl",
        description="GoFile Downloader - Simple CLI tool to download files from GoFile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  %(prog)s https://gofile.io/d/5tkZZi
  %(prog)s https://gofile.io/d/5tkZZi --password MyPass
  %(prog)s https://gofile.io/d/5tkZZi --proxy https://your-worker.example.com/
  %(prog)s https://gofile.io/d/5tkZZi -o /path/to/save
  %(prog)s https://gofile.io/d/5tkZZi --proxy https://your-worker.example.com/ -w 16

Proxy usage:
  When GoFile is inaccessible (e.g. from China), use a Cloudflare Workers
  proxy. The proxy URL is prepended to the original URL:
    https://your-worker.example.com/https://api.gofile.io/accounts

  The proxy is applied to ALL requests (API + file downloads).

Resume:
  If a download is interrupted, simply re-run the same command.
  The script will automatically resume from where it left off.
""",
    )
    parser.add_argument("url", help="GoFile URL to download (e.g. https://gofile.io/d/xxxxx)")
    parser.add_argument("--password", "-p", help="Password for password-protected albums")
    parser.add_argument(
        "--output", "-o",
        help="Output directory (default: ./Downloads)",
    )
    parser.add_argument(
        "--proxy",
        help="Cloudflare Workers proxy URL prefix (e.g. https://your-worker.example.com/). "
             "Applied to all GoFile API and download requests.",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=MAX_WORKERS,
        help=f"Number of parallel download threads (default: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Download files sequentially instead of in parallel",
    )
    parser.add_argument(
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    args = parser.parse_args()

    try:
        download(args.url, args)
    except KeyboardInterrupt:
        print("\n[INFO] Download interrupted by user. Re-run to resume.")
        sys.exit(1)
