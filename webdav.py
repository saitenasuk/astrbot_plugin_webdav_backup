from base64 import b64encode
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp
from yarl import URL


def collection_url(value: str, allow_http: bool) -> str:
    """Validate a user-configured WebDAV collection URL before sending credentials.

    Args:
        value: Address of an existing WebDAV directory.
        allow_http: Allow cleartext transport on a trusted network.

    Returns:
        Collection URL with a trailing slash.
    """
    value = value.strip()
    url = urlsplit(value)
    if (
        url.scheme not in (("https", "http") if allow_http else ("https",))
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError(
            "请填写 WebDAV 目录地址，使用 HTTPS，并在单独字段中填写账号和密码"
        )
    return str(URL(value)).rstrip("/") + "/"


async def upload(path: Path, collection: str, username: str, password: str) -> None:
    """Stream an archive to WebDAV, then rename the completed upload.

    Args:
        path: Local archive with a unique filename.
        collection: Validated URL of an existing WebDAV collection.
        username: WebDAV user.
        password: WebDAV application password.
    """
    final = collection + quote(path.name, safe="")
    partial = final + ".partial"
    headers = {}
    if username:
        encoded = b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    timeout = aiohttp.ClientTimeout(total=3600, sock_connect=30, sock_read=300)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        cleanup_partial = True
        try:
            with path.open("rb") as file:
                async with session.put(
                    partial,
                    data=file,
                    allow_redirects=False,
                    headers={"Content-Type": "application/zip", "If-None-Match": "*"},
                ) as res:
                    if res.status == 412:
                        cleanup_partial = False
                    if res.status not in (200, 201, 204):
                        raise RuntimeError(f"WebDAV PUT 返回 HTTP {res.status}")
            async with session.request(
                "MOVE",
                partial,
                headers={"Destination": final, "Overwrite": "F"},
                allow_redirects=False,
            ) as res:
                if res.status not in (200, 201, 204):
                    raise RuntimeError(f"WebDAV MOVE 返回 HTTP {res.status}")
        except Exception:
            # Names include a random UUID. Never delete an existing final archive.
            if not cleanup_partial:
                raise
            try:
                async with session.delete(partial, allow_redirects=False) as res:
                    pass
            except (aiohttp.ClientError, TimeoutError):
                pass
            raise
