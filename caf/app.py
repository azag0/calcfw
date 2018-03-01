# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from pathlib import Path
from collections import OrderedDict
import os
import asyncio
from contextlib import contextmanager

from .hooks import Hookable
from . import asyncio as _asyncio

from typing import (
    Any, Dict, List, Optional, Callable, Awaitable, Iterator, TypeVar, overload,
    Iterable,
)

_T = TypeVar('_T')

RouteFunc = Callable[[], Any]
Executor = Callable[[bytes], Awaitable[bytes]]

CAFDIR = Path(os.environ.get('CAF_DIR', '.caf')).resolve()


class UnfinishedTask(Exception):
    pass


@overload
async def collect(coros: Iterable[Awaitable[_T]]) -> List[Optional[_T]]: ...


@overload
async def collect(coros: Iterable[Awaitable[_T]], unfinished: _T) -> List[_T]: ...


async def collect(coros, unfinished=None):  # type: ignore
    results = await _asyncio.gather(*coros, returned_exception=UnfinishedTask)
    return [unfinished if isinstance(r, UnfinishedTask) else r for r in results]


class Context:
    def __init__(self, executing: bool = False, readonly: bool = True) -> None:
        self.executing = executing
        self.readonly = readonly
        self.g: Dict[str, Any] = {}

    def __repr__(self) -> str:
        return f'<Context executing={self.executing} readonly={self.readonly} g={self.g!r}>'


class Caf(Hookable):
    def __init__(self, cafdir: Path = None) -> None:
        super().__init__()
        self.cafdir = cafdir.resolve() if cafdir else CAFDIR
        self._routes: Dict[str, RouteFunc] = OrderedDict()
        self._executors: Dict[str, Executor] = {}
        self._ctx: Optional[Context] = None

    def __repr__(self) -> str:
        return f'<Caf routes={list(self._routes)!r} cafdir={self.cafdir!r}>'

    async def task(self, execid: str, inp: bytes, label: str = None) -> bytes:
        exe = self._executors[execid]
        if self.has_hook('dispatch'):
            assert label
            exe = self.get_hook('dispatch')(exe, label)
        if self.has_hook('cache'):
            assert label
            return await self.get_hook('cache')(exe, execid, inp, label)  # type: ignore
        return await exe(inp)

    def route(self, label: str) -> Callable[[RouteFunc], RouteFunc]:
        def decorator(route_func: RouteFunc) -> RouteFunc:
            self._routes[label] = route_func
            return route_func
        return decorator

    def register_exec(self, execid: str, exe: Executor) -> None:
        self._executors[execid] = exe

    @contextmanager
    def context(self, executing: bool = False, readonly: bool = True) -> Iterator[None]:
        self._ctx = Context(executing=executing, readonly=readonly)
        try:
            yield
        finally:
            self._ctx = None

    @property
    def ctx(self) -> Context:
        assert self._ctx
        return self._ctx

    def get(self, *routes: str) -> Any:
        tasks = asyncio.gather(*(self._routes[route]() for route in routes))
        loop = asyncio.get_event_loop()
        try:
            result = loop.run_until_complete(tasks)
        except KeyboardInterrupt:
            tasks.cancel()
            try:
                loop.run_until_complete(tasks)
            except asyncio.CancelledError:
                pass
            raise
        if self.has_hook('postget'):
            self.get_hook('postget')()
        return result[0] if len(routes) == 1 else result
