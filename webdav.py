import asyncio
from base64 import b64encode
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from urllib.parse import quote

import aiohttp
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from yarl import URL

from astrbot.api import logger


class WebDAVError(RuntimeError):
    """A transfer failure with a diagnostic safe to display in the dashboard."""


def collection_url(value: str, allow_http: bool) -> str:
    """Validate a user-configured WebDAV collection URL before sending credentials.

    Args:
        value: Address of an existing WebDAV directory.
        allow_http: Allow cleartext transport on a trusted network.

    Returns:
        Collection URL with a trailing slash.

    Raises:
        ValueError: If the address is unsuitable for sending credentials.
    """
    message = "请填写 WebDAV 目录地址，使用 HTTPS，并在单独字段中填写账号和密码"
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
        return str(url).rstrip("/") + "/"
    except ValueError as exc:
        raise ValueError(message) from exc


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


async def _verify(session, final, size):
    """Check the final resource's own successful getcontentlength property.

    Args:
        session: Authenticated HTTP client.
        final: Expected URL after MOVE.
        size: Byte length of the local archive.

    Raises:
        WebDAVError: If the server does not confirm the expected file and size.
    """
    async with _request(
        session,
        "PROPFIND",
        final,
        (207,),
        headers={
            "Depth": "0",
            "Content-Type": "application/xml; charset=utf-8",
            "Cache-Control": "no-cache",
        },
        data=b'<propfind xmlns="DAV:"><prop><getcontentlength/></prop></propfind>',
    ) as res:
        try:
            await res.content.readexactly(65537)
        except asyncio.IncompleteReadError as exc:
            xml = exc.partial
        else:
            raise WebDAVError("WebDAV PROPFIND 返回的文件属性过大")
    try:
        root = ElementTree.fromstring(xml, forbid_dtd=True)
    except (
        ElementTree.ParseError,
        DefusedXmlException,
        LookupError,
        ValueError,
    ) as exc:
        raise WebDAVError(
            "WebDAV PROPFIND 未返回有效的文件属性，请检查 WebDAV 目录地址"
        ) from exc
    target = URL(final)
    for response in (
        root.findall("{DAV:}response") if root.tag == "{DAV:}multistatus" else ()
    ):
        href = response.findtext("{DAV:}href")
        if not href or not href.strip():
            continue
        try:
            if target.join(URL(href.strip())) != target:
                continue
        except ValueError:
            continue
        for propstat in response.findall("{DAV:}propstat"):
            status = (propstat.findtext("{DAV:}status") or "").split()
            length = (
                propstat.findtext("{DAV:}prop/{DAV:}getcontentlength") or ""
            ).strip()
            if (
                len(status) < 2
                or status[1] != "200"
                or not (length.isascii() and length.isdecimal() and len(length) <= 20)
            ):
                continue
            if int(length) != size:
                raise WebDAVError(
                    f"WebDAV 远端文件大小不符（远端 {int(length)} 字节，本地 {size} 字节）"
                )
            logger.info("WebDAV verified %s (%d bytes)", final, size)
            return
    raise WebDAVError(
        "WebDAV PROPFIND 未确认最终文件的路径与大小，请检查远端目录和读取权限"
    )


async def upload(
    path: Path, collection: str, username: str, password: str, *, probe: bool = False
) -> None:
    """Upload, rename and verify an archive, removing the final file for a probe.

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
    partial = final + ".partial"
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
        cleanup = None
        try:
            with path.open("rb") as file:
                async with _request(
                    session,
                    "PUT",
                    partial,
                    (200, 201, 204),
                    data=file,
                    headers={"Content-Type": "application/zip", "If-None-Match": "*"},
                ):
                    cleanup = partial
            async with _request(
                session,
                "MOVE",
                partial,
                (200, 201, 204),
                headers={"Destination": final, "Overwrite": "F"},
            ):
                cleanup = final if probe else None
            await _verify(session, final, size)
        except (Exception, asyncio.CancelledError):
            # Only clean a name we successfully created. Keep real final archives for inspection.
            if cleanup:
                try:
                    async with _request(session, "DELETE", cleanup, (200, 204, 404)):
                        pass
                except WebDAVError as exc:
                    logger.warning("WebDAV cleanup failed for %s: %s", cleanup, exc)
            raise
        if probe:
            async with _request(session, "DELETE", final, (200, 204)):
                pass
