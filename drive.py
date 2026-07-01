import re
import httpx


def extract_file_id(url: str) -> str | None:
    for pattern in [r'/file/d/([a-zA-Z0-9_-]+)', r'[?&]id=([a-zA-Z0-9_-]+)']:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


async def download_from_drive(drive_url: str) -> tuple[bytes, str]:
    file_id = extract_file_id(drive_url)
    if not file_id:
        raise ValueError(f"Invalid Drive URL: {drive_url}")

    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        resp = await client.get(url, headers=headers)

        if "text/html" in resp.headers.get("content-type", ""):
            # Large file confirm page. Google no longer embeds a confirm=TOKEN
            # in the page body — it sets a download_warning_* cookie instead
            # and expects confirm=t on the retry. Try the cookie-based flow
            # first; fall back to the old regex in case some accounts/files
            # still serve the legacy page (keeps both code paths working).
            confirm_cookie = next(
                (v for k, v in resp.cookies.items() if k.startswith("download_warning")),
                None
            )
            if confirm_cookie:
                url = f"https://drive.google.com/uc?export=download&confirm={confirm_cookie}&id={file_id}"
                resp = await client.get(url, headers=headers, cookies=dict(resp.cookies))
            else:
                confirm = re.search(r'confirm=([a-zA-Z0-9_-]+)', resp.text)
                if confirm:
                    url = f"https://drive.google.com/uc?export=download&confirm={confirm.group(1)}&id={file_id}"
                    resp = await client.get(url, headers=headers)
                else:
                    # Last resort: force confirm=t directly. Covers the case
                    # where Google shows the warning page with no token at
                    # all (small "scan" warnings on non-huge files).
                    url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
                    resp = await client.get(url, headers=headers, cookies=dict(resp.cookies))

            if "text/html" in resp.headers.get("content-type", ""):
                # Still HTML after every fallback — genuinely private/missing,
                # not a confirm-page parsing failure.
                raise ValueError("Drive file may be private or inaccessible")

        if not resp.is_success:
            raise ValueError(f"Drive download failed: HTTP {resp.status_code}")

        cd = resp.headers.get("content-disposition", "")
        fn_match = re.search(r'filename="?([^";\r\n]+)"?', cd)
        filename = fn_match.group(1).strip() if fn_match else f"{file_id}.mp3"

        return resp.content, filename
