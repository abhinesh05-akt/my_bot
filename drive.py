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
            # Large file confirm page
            confirm = re.search(r'confirm=([a-zA-Z0-9_-]+)', resp.text)
            if not confirm:
                raise ValueError("Drive file may be private or inaccessible")
            url = f"https://drive.google.com/uc?export=download&confirm={confirm.group(1)}&id={file_id}"
            resp = await client.get(url, headers=headers)

        if not resp.is_success:
            raise ValueError(f"Drive download failed: HTTP {resp.status_code}")

        cd = resp.headers.get("content-disposition", "")
        fn_match = re.search(r'filename="?([^";\r\n]+)"?', cd)
        filename = fn_match.group(1).strip() if fn_match else f"{file_id}.mp3"

        return resp.content, filename
