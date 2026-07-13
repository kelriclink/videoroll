"""Loopback-only RFB bridge for noVNC sessions.

The browser is authorized by the reverse proxy before it can reach websockify.
This bridge then authenticates to the local x11vnc server with a password which
never crosses the browser connection.  It deliberately presents RFB's ``None``
security type to the already-authorized browser and relays the established RFB
session unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import stat
import struct
from pathlib import Path

from Cryptodome.Cipher import DES


RFB_VERSION_3_8 = b"RFB 003.008\n"
RFB_SECURITY_NONE = 1
RFB_SECURITY_VNC_AUTH = 2
_SERVER_INIT_FIXED_BYTES = 24


class RfbProtocolError(Exception):
    """Raised when either end of the RFB handshake is invalid."""


def _parse_version(value: bytes) -> tuple[int, int]:
    if len(value) != 12 or not value.startswith(b"RFB ") or value[-1:] != b"\n":
        raise RfbProtocolError("invalid RFB protocol version")
    try:
        major = int(value[4:7])
        minor = int(value[8:11])
    except ValueError as exc:
        raise RfbProtocolError("invalid RFB protocol version") from exc
    if value[7:8] != b".":
        raise RfbProtocolError("invalid RFB protocol version")
    return major, minor


def _target_version(server_version: bytes) -> bytes:
    major, minor = _parse_version(server_version)
    if major != 3:
        raise RfbProtocolError("unsupported RFB major version")
    if minor >= 8:
        return RFB_VERSION_3_8
    if minor >= 7:
        return b"RFB 003.007\n"
    return b"RFB 003.003\n"


def _read_password(path: Path) -> bytearray:
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RfbProtocolError("VNC password path is not a regular file")
    if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RfbProtocolError("VNC password file permissions are unsafe")
    password = bytearray(path.read_bytes().rstrip(b"\r\n"))
    if not password:
        raise RfbProtocolError("VNC password file is empty")
    return password


def _vnc_auth_response(challenge: bytes, password: bytearray) -> bytes:
    if len(challenge) != 16:
        raise RfbProtocolError("invalid VNC authentication challenge")
    # VNC's historical DES key reverses every bit of the first eight password
    # bytes.  DES is required by the RFB VNC-auth protocol, not used to store
    # the password locally.
    key = bytearray(8)
    for index, value in enumerate(password[:8]):
        key[index] = int(f"{value:08b}"[::-1], 2)
    try:
        return DES.new(bytes(key), DES.MODE_ECB).encrypt(challenge)
    finally:
        key[:] = b"\0" * len(key)


async def _read_failure_reason(reader: asyncio.StreamReader) -> str:
    length = struct.unpack(">I", await reader.readexactly(4))[0]
    # A failing server is untrusted input.  Limit the read and never relay its
    # arbitrary reason text to logs or a browser.
    if length > 4096:
        raise RfbProtocolError("RFB failure reason is too large")
    await reader.readexactly(length)
    return "RFB authentication failed"


async def _authenticate_target(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    password_file: Path,
    client_init: bytes,
) -> bytes:
    server_version = await reader.readexactly(12)
    selected_version = _target_version(server_version)
    writer.write(selected_version)
    await writer.drain()

    _, minor = _parse_version(selected_version)
    if minor == 3:
        security_type = struct.unpack(">I", await reader.readexactly(4))[0]
        if security_type != RFB_SECURITY_VNC_AUTH:
            raise RfbProtocolError("x11vnc did not require VNC authentication")
    else:
        count = (await reader.readexactly(1))[0]
        if count == 0:
            await _read_failure_reason(reader)
            raise RfbProtocolError("x11vnc rejected authentication")
        security_types = await reader.readexactly(count)
        if RFB_SECURITY_VNC_AUTH not in security_types:
            raise RfbProtocolError("x11vnc did not offer VNC authentication")
        writer.write(bytes([RFB_SECURITY_VNC_AUTH]))
        await writer.drain()

    password = _read_password(password_file)
    try:
        challenge = await reader.readexactly(16)
        writer.write(_vnc_auth_response(challenge, password))
        await writer.drain()
    finally:
        password[:] = b"\0" * len(password)

    result = struct.unpack(">I", await reader.readexactly(4))[0]
    if result:
        if minor >= 8:
            await _read_failure_reason(reader)
        raise RfbProtocolError("x11vnc rejected authentication")

    writer.write(client_init)
    await writer.drain()
    fixed_init = await reader.readexactly(_SERVER_INIT_FIXED_BYTES)
    name_length = struct.unpack(">I", fixed_init[-4:])[0]
    if name_length > 4096:
        raise RfbProtocolError("RFB desktop name is too large")
    return fixed_init + await reader.readexactly(name_length)


async def _relay(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_reader: asyncio.StreamReader,
    target_writer: asyncio.StreamWriter,
) -> None:
    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    tasks = [
        asyncio.create_task(copy(client_reader, target_writer)),
        asyncio.create_task(copy(target_reader, client_writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    target_host: str,
    target_port: int,
    password_file: Path,
) -> None:
    target_writer: asyncio.StreamWriter | None = None
    try:
        client_writer.write(RFB_VERSION_3_8)
        await client_writer.drain()
        _parse_version(await client_reader.readexactly(12))
        client_writer.write(bytes([1, RFB_SECURITY_NONE]))
        await client_writer.drain()
        if (await client_reader.readexactly(1))[0] != RFB_SECURITY_NONE:
            raise RfbProtocolError("browser chose an unsupported security type")
        client_writer.write(struct.pack(">I", 0))
        await client_writer.drain()
        client_init = await client_reader.readexactly(1)

        target_reader, target_writer = await asyncio.open_connection(target_host, target_port)
        server_init = await _authenticate_target(target_reader, target_writer, password_file, client_init)
        client_writer.write(server_init)
        await client_writer.drain()
        await _relay(client_reader, client_writer, target_reader, target_writer)
    except (ConnectionError, asyncio.IncompleteReadError, RfbProtocolError, OSError):
        # Connection failures are expected while a user closes/reloads noVNC.
        # Do not log protocol details because they can contain VNC server data.
        pass
    finally:
        client_writer.close()
        with contextlib.suppress(ConnectionError):
            await client_writer.wait_closed()
        if target_writer is not None:
            target_writer.close()
            with contextlib.suppress(ConnectionError):
                await target_writer.wait_closed()


async def serve(
    *,
    listen_host: str,
    listen_port: int,
    target_host: str,
    target_port: int,
    password_file: Path,
) -> None:
    server = await asyncio.start_server(
        lambda reader, writer: _handle_client(
            reader,
            writer,
            target_host=target_host,
            target_port=target_port,
            password_file=password_file,
        ),
        host=listen_host,
        port=listen_port,
    )
    async with server:
        await server.serve_forever()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge authorized noVNC RFB sessions to x11vnc")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=5901)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=5900)
    parser.add_argument("--password-file", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        serve(
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            target_host=args.target_host,
            target_port=args.target_port,
            password_file=Path(args.password_file),
        )
    )


if __name__ == "__main__":
    main()
