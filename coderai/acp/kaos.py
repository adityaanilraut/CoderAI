"""KAOS backend that routes filesystem and terminal execution through ACP."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from typing import Literal

import acp
from coderai.kaos import Kaos, KaosProcess, StatResult, StrOrKaosPath
from coderai.kaos.local import local_kaos
from coderai.kaos.path import KaosPath

_DEFAULT_TERMINAL_OUTPUT_LIMIT = 50_000
_DEFAULT_POLL_INTERVAL = 0.2


class ACPKaos:
    """KAOS backend that routes supported operations through ACP."""

    name: str = "acp"

    def __init__(
        self,
        client: acp.Client,
        session_id: str,
        client_capabilities: acp.schema.ClientCapabilities | None,
        fallback: Kaos | None = None,
        *,
        output_byte_limit: int | None = _DEFAULT_TERMINAL_OUTPUT_LIMIT,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._fallback = fallback or local_kaos
        fs = client_capabilities.fs if client_capabilities else None
        self._supports_read = bool(fs and fs.read_text_file)
        self._supports_write = bool(fs and fs.write_text_file)
        self._supports_terminal = bool(client_capabilities and client_capabilities.terminal)
        self._output_byte_limit = output_byte_limit
        self._poll_interval = poll_interval

    def pathclass(self):
        return self._fallback.pathclass()

    def normpath(self, path: StrOrKaosPath) -> KaosPath:
        return self._fallback.normpath(path)

    def gethome(self) -> KaosPath:
        return self._fallback.gethome()

    def getcwd(self) -> KaosPath:
        return self._fallback.getcwd()

    async def chdir(self, path: StrOrKaosPath) -> None:
        await self._fallback.chdir(path)

    async def stat(self, path: StrOrKaosPath, *, follow_symlinks: bool = True) -> StatResult:
        return await self._fallback.stat(path, follow_symlinks=follow_symlinks)

    def iterdir(self, path: StrOrKaosPath) -> AsyncGenerator[KaosPath]:
        return self._fallback.iterdir(path)

    def glob(
        self, path: StrOrKaosPath, pattern: str, *, case_sensitive: bool = True
    ) -> AsyncGenerator[KaosPath]:
        return self._fallback.glob(path, pattern, case_sensitive=case_sensitive)

    async def readbytes(self, path: StrOrKaosPath, n: int | None = None) -> bytes:
        return await self._fallback.readbytes(path, n=n)

    async def readtext(
        self,
        path: StrOrKaosPath,
        *,
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> str:
        abs_path = self._abs_path(path)
        if not self._supports_read:
            return await self._fallback.readtext(abs_path, encoding=encoding, errors=errors)
        response = await self._client.read_text_file(path=abs_path, session_id=self._session_id)
        return response.content

    async def readlines(
        self,
        path: StrOrKaosPath,
        *,
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> AsyncGenerator[str]:
        text = await self.readtext(path, encoding=encoding, errors=errors)
        for line in text.splitlines(keepends=True):
            yield line

    async def writebytes(self, path: StrOrKaosPath, data: bytes) -> int:
        return await self._fallback.writebytes(path, data)

    async def writetext(
        self,
        path: StrOrKaosPath,
        data: str,
        *,
        mode: Literal["w", "a"] = "w",
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> int:
        abs_path = self._abs_path(path)
        if mode == "a":
            if self._supports_read and self._supports_write:
                existing = await self.readtext(abs_path, encoding=encoding, errors=errors)
                await self._client.write_text_file(
                    path=abs_path,
                    content=existing + data,
                    session_id=self._session_id,
                )
                return len(data)
            return await self._fallback.writetext(
                abs_path, data, mode="a", encoding=encoding, errors=errors
            )

        if not self._supports_write:
            return await self._fallback.writetext(
                abs_path, data, mode=mode, encoding=encoding, errors=errors
            )

        await self._client.write_text_file(
            path=abs_path,
            content=data,
            session_id=self._session_id,
        )
        return len(data)

    async def mkdir(
        self, path: StrOrKaosPath, parents: bool = False, exist_ok: bool = False
    ) -> None:
        await self._fallback.mkdir(path, parents=parents, exist_ok=exist_ok)

    async def exec(self, *args: str, env: Mapping[str, str] | None = None) -> KaosProcess:
        return await self._fallback.exec(*args, env=env)

    def _abs_path(self, path: StrOrKaosPath) -> str:
        kaos_path = path if isinstance(path, KaosPath) else KaosPath(path)
        return str(kaos_path.canonical())


__all__ = ["ACPKaos"]
