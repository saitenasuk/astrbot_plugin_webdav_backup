import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from urllib.parse import quote, unquote

import aiohttp
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from yarl import URL

from astrbot.api import logger


class WebDAVError(RuntimeError):
    """A transfer failure with a diagnostic safe to display in the dashboard."""


def collection_url(value: str, allow_http: bool, remote_dir: str = "/") -> str:
    """Validate a user-configured WebDAV collection URL before sending credentials.

    Args:
        value: WebDAV service address, including its mount path.
        allow_http: Allow cleartext transport on a trusted network.
        remote_dir: Directory relative to the service root, using unescaped path segments.

    Returns:
        Collection URL with a trailing slash.

    Raises:
        ValueError: If the address is unsuitable for sending credentials.
    """
    message = "请填写 WebDAV 服务地址，使用 HTTPS，并在单独字段中填写账号和密码"
    try:
        url = URL(value.strip())
        if (
            url.scheme not in (("https", "http") if allow_http else ("https",))
            or not url.host
            or url.raw_user is not None
            or url.raw_password is not None
            or url.query_string
            or url.fragment
        ):
            raise ValueError(message)
    except ValueError as exc:
        raise ValueError(message) from exc
    directory = remote_dir.strip()
    parts = [part for part in directory.split("/") if part]
    if (
        directory.startswith("//")
        or "://" in directory
        or any(
            unquote(part) in (".", "..")
            or "/" in unquote(part)
            or "\\" in unquote(part)
            or any(ord(c) < 32 for c in part)
            for part in parts
        )
    ):
        raise ValueError("请填写服务地址下的远程目录，例如 / 或 /backups")
    return (
        str(url).rstrip("/")
        + "/"
        + "".join(quote(part, safe="") + "/" for part in parts)
    )


@asynccontextmanager
async def _request(session, method, url, expected, **kwargs):
    """Log a WebDAV exchange without disclosing headers or response bodies.

    Args:
        session: Authenticated HTTP client.
        method: WebDAV method.
        url: Validated target without URL credentials or a query string.
        expected: HTTP status codes accepted for this operation.
        **kwargs: Additional aiohttp request arguments.

    Yields:
        The response while its connection is still open.

    Raises:
        WebDAVError: If the request fails or the server rejects it.
    """
    start = monotonic()
    logger.info("WebDAV %s %s started", method, url)
    try:
        async with session.request(method, url, allow_redirects=False, **kwargs) as res:
            logger.info(
                "WebDAV %s %s returned HTTP %d (%.2fs)",
                method,
                url,
                res.status,
                monotonic() - start,
            )
            if res.status not in expected:
                raise WebDAVError(f"WebDAV {method} 返回 HTTP {res.status}")
            yield res
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise WebDAVError(f"WebDAV {method} 连接异常（{type(exc).__name__}）") from exc


def _reported_size(root, request_url, target):
    """Read the target file's properties from a file or directory multistatus.

    Args:
        root: Parsed DAV multistatus document.
        request_url: Request URI against which relative hrefs are resolved.
        target: Expected absolute URL of the final file.

    Returns:
        The file size and an empty message, or None and a diagnostic.
    """
    responses = root.findall("{DAV:}response")
    reason = f"返回 {len(responses)} 项，未找到与目标路径一致的 href"
    for response in responses:
        href = (response.findtext("{DAV:}href") or "").strip()
        if not href:
            continue
        try:
            resource = request_url.join(URL(href))
        except ValueError:
            continue
        if resource != target:
            if str(resource) == str(target) + "/":
                reason = "服务器把目标 ZIP 列为目录（href 以 / 结尾）"
            elif resource.name == target.name:
                reason = "找到同名条目，但 href 的目录或主机与目标不符"
            continue
        reason = "目标条目缺少可用的 getcontentlength"
        reported = None
        for propstat in response.findall("{DAV:}propstat"):
            status = (propstat.findtext("{DAV:}status") or "").split()
            if len(status) < 2 or status[1] != "200":
                if (
                    len(status) >= 2
                    and len(status[1]) == 3
                    and status[1].isascii()
                    and status[1].isdecimal()
                ):
                    reason = f"目标条目属性返回 HTTP {status[1]}"
                continue
            prop = propstat.find("{DAV:}prop")
            if prop is None:
                continue
            if prop.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                return None, "服务器把目标 ZIP 列为目录"
            length = (prop.findtext("{DAV:}getcontentlength") or "").strip()
            if length.isascii() and length.isdecimal() and len(length) <= 20:
                reported = int(length)
        if reported is not None:
            return reported, ""
    return None, reason


async def _verify(session, final, size):
    """Verify the final ZIP directly or through its parent directory entry.

    Args:
        session: Authenticated HTTP client.
        final: URL used for the upload.
        size: Byte length of the local archive.

    Raises:
        WebDAVError: If the server does not confirm the expected file and size.
    """
    target = URL(final)
    # Keep the collection's trailing slash so relative child hrefs resolve inside it.
    for depth, url, limit in [
        ("0", final, 64 * 1024),
        ("1", final.rsplit("/", 1)[0] + "/", 4 * 1024 * 1024),
    ]:
        async with _request(
            session,
            "PROPFIND",
            url,
            (207,),
            headers={
                "Depth": depth,
                "Content-Type": "application/xml; charset=utf-8",
                "Cache-Control": "no-cache",
            },
            data=b'<propfind xmlns="DAV:"><prop><getcontentlength/><resourcetype/></prop></propfind>',
        ) as res:
            try:
                await res.content.readexactly(limit + 1)
            except asyncio.IncompleteReadError as exc:
                xml = exc.partial
            else:
                raise WebDAVError(
                    f"WebDAV PROPFIND Depth={depth} 返回的属性超过 {limit // 1024} KiB"
                )
        try:
            root = ElementTree.fromstring(xml, forbid_dtd=True)
        except (
            ElementTree.ParseError,
            DefusedXmlException,
            LookupError,
            ValueError,
        ) as exc:
            raise WebDAVError(
                "WebDAV PROPFIND 未返回有效的文件属性，请检查 WebDAV 服务地址"
            ) from exc
        if root.tag != "{DAV:}multistatus":
            raise WebDAVError(
                "WebDAV PROPFIND 缺少 DAV multistatus，请检查 WebDAV 服务地址"
            )
        reported, reason = _reported_size(root, URL(url), target)
        if reported is not None:
            if reported != size:
                raise WebDAVError(
                    f"WebDAV 远端文件大小不符（远端 {reported} 字节，本地 {size} 字节）"
                )
            logger.info(
                "WebDAV verified %s (%d bytes, PROPFIND Depth=%s)", final, size, depth
            )
            return
        if depth == "0":
            logger.info(
                "WebDAV PROPFIND Depth=0 inconclusive for %s (%d entries, %d collections, %d length properties), checking parent collection",
                final,
                len(root.findall("{DAV:}response")),
                len(root.findall(".//{DAV:}collection")),
                len(root.findall(".//{DAV:}getcontentlength")),
            )
        else:
            raise WebDAVError(f"WebDAV PROPFIND 未确认最终文件：父目录 {reason}")


async def upload(
    path: Path, collection: str, username: str, password: str, *, probe: bool = False
) -> None:
    """Upload and verify a uniquely named archive, removing it after a probe.

    Args:
        path: Local archive with a unique filename.
        collection: Validated URL of an existing WebDAV collection.
        username: WebDAV user.
        password: WebDAV application password.
        probe: Delete the uniquely named test file after checking its properties.

    Raises:
        WebDAVError: If the transfer, verification or probe cleanup fails.
    """
    final = collection + quote(path.name, safe="")
    size = path.stat().st_size
    headers = {}
    if username:
        encoded = b64encode(f"{username}:{password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    timeout = aiohttp.ClientTimeout(
        total=8 if probe else 3600,
        sock_connect=5 if probe else 30,
        sock_read=8 if probe else 300,
    )
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        with path.open("rb") as file:
            async with _request(
                session,
                "PUT",
                final,
                (200, 201, 204),
                data=file,
                headers={"Content-Type": "application/zip", "If-None-Match": "*"},
            ):
                pass
        try:
            await _verify(session, final, size)
        except (Exception, asyncio.CancelledError):
            if probe:
                try:
                    async with _request(session, "DELETE", final, (200, 204, 404)):
                        pass
                except WebDAVError as exc:
                    logger.warning("WebDAV cleanup failed for %s: %s", final, exc)
            raise
        if probe:
            async with _request(session, "DELETE", final, (200, 204)):
                pass
