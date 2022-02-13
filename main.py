#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import re
from configparser import ConfigParser
from pathlib import Path
from random import shuffle
from shutil import rmtree
from time import perf_counter
from typing import Callable, Mapping

from aiohttp import ClientSession
from aiohttp_socks import ProxyConnector


class Proxy:
    __slots__ = (
        "socket_address",
        "ip",
        "is_anonymous",
        "geolocation",
        "timeout",
    )

    def __init__(self, socket_address: str, ip: str) -> None:
        """
        Args:
            socket_address: ip:port
        """
        self.socket_address = socket_address
        self.ip = ip
        self.is_anonymous: bool | None = None
        self.geolocation = "|?|?|?"
        self.timeout = float("inf")

    def update(self, info: Mapping[str, str]) -> None:
        """Set geolocation and is_anonymous.

        Args:
            info: Response from ip-api.com.
        """
        country = info.get("country") or "?"
        region = info.get("regionName") or "?"
        city = info.get("city") or "?"
        self.geolocation = f"|{country}|{region}|{city}"
        self.is_anonymous = self.ip != info.get("query")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Proxy):
            return NotImplemented
        return self.socket_address == other.socket_address

    def __hash__(self) -> int:
        return hash(("socket_address", self.socket_address))


class Folder:
    __slots__ = ("path", "for_anonymous", "for_geolocation")

    def __init__(self, path: Path, folder_name: str) -> None:
        self.path = path / folder_name
        self.for_anonymous = "anon" in folder_name
        self.for_geolocation = "geo" in folder_name

    def remove(self) -> None:
        try:
            rmtree(self.path)
        except FileNotFoundError:
            pass

    def create(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)


def speed_sorting_key(proxy: Proxy) -> float:
    return proxy.timeout


def alphabet_sorting_key(proxy: Proxy) -> tuple[int, ...]:
    return tuple(map(int, proxy.socket_address.replace(":", ".").split(".")))


class ProxyScraperChecker:
    """HTTP, SOCKS4, SOCKS5 proxies scraper and checker."""

    __slots__ = (
        "all_folders",
        "console",
        "enabled_folders",
        "path",
        "proxies_count",
        "proxies",
        "regex",
        "sem",
        "sort_by_speed",
        "sources",
        "timeout",
    )

    def __init__(
        self,
        *,
        timeout: float,
        max_connections: int,
        sort_by_speed: bool,
        save_path: str,
        proxies: bool,
        proxies_anonymous: bool,
        proxies_geolocation: bool,
        proxies_geolocation_anonymous: bool,
        http_sources: str | None,
        socks4_sources: str | None,
        socks5_sources: str | None,
    ) -> None:
        """HTTP, SOCKS4, SOCKS5 proxies scraper and checker.

        Args:
            timeout: How many seconds to wait for the connection. The
                higher the number, the longer the check will take and
                the more proxies you get.
            max_connections: Maximum concurrent connections. Don't set
                higher than 900, please.
            sort_by_speed: Set to False to sort proxies alphabetically.
            save_path: Path to the folder where the proxy folders will
                be saved. Leave empty to save the proxies to the current
                directory.
        """
        self.path = Path(save_path)
        folders_mapping = {
            "proxies": proxies,
            "proxies_anonymous": proxies_anonymous,
            "proxies_geolocation": proxies_geolocation,
            "proxies_geolocation_anonymous": proxies_geolocation_anonymous,
        }
        self.all_folders = tuple(
            Folder(self.path, folder_name) for folder_name in folders_mapping
        )
        self.enabled_folders = tuple(
            folder
            for folder in self.all_folders
            if folders_mapping[folder.path.name]
        )
        if not self.enabled_folders:
            raise ValueError("all folders are disabled in the config")

        regex = (
            r"(?:^|\D)?(("
            + r"(?:[1-9]|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])"  # 1-255
            + r"\."
            + r"(?:\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])"  # 0-255
            + r"\."
            + r"(?:\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])"  # 0-255
            + r"\."
            + r"(?:\d|[1-9]\d|1\d{2}|2[0-4]\d|25[0-5])"  # 0-255
            + r"):"
            + (
                r"(?:\d|[1-9]\d{1,3}|[1-5]\d{4}|6[0-4]\d{3}"
                + r"|65[0-4]\d{2}|655[0-2]\d|6553[0-5])"
            )  # 0-65535
            + r")(?:\D|$)"
        )
        self.regex = re.compile(regex)

        self.sort_by_speed = sort_by_speed
        self.timeout = timeout
        self.sources = {
            proto: frozenset(filter(None, sources.splitlines()))
            for proto, sources in (
                ("http", http_sources),
                ("socks4", socks4_sources),
                ("socks5", socks5_sources),
            )
            if sources
        }
        self.proxies: dict[str, set[Proxy]] = {
            proto: set() for proto in self.sources
        }
        self.proxies_count = {proto: 0 for proto in self.sources}
        self.sem = asyncio.Semaphore(max_connections)

    async def fetch_source(
        self, session: ClientSession, source: str, proto: str
    ) -> None:
        """Get proxies from source.

        Args:
            source: Proxy list URL.
            proto: http/socks4/socks5.
        """
        source = source.strip()
        try:
            async with session.get(source, timeout=15) as r:
                status = r.status
                text = await r.text()
        except Exception:
            logging.error("%s - error", source)
        else:
            proxies = tuple(self.regex.finditer(text))
            if proxies:
                for proxy in proxies:
                    p = Proxy(proxy.group(1), proxy.group(2))
                    self.proxies[proto].add(p)
            else:
                status_code_msg = (
                    "" if status == 200 else f" (status code {status})"
                )
                logging.warning(
                    "%s - no proxies found%s", source, status_code_msg
                )

    async def check_proxy(self, proxy: Proxy, proto: str) -> None:
        """Check if proxy is alive."""
        try:
            async with self.sem:
                proxy_url = f"{proto}://{proxy.socket_address}"
                connector = ProxyConnector.from_url(proxy_url)
                start = perf_counter()
                async with ClientSession(connector=connector) as session:
                    async with session.get(
                        "http://ip-api.com/json/?fields=8217",
                        timeout=self.timeout,
                    ) as r:
                        res = await r.json() if r.status == 200 else None
        except Exception as e:
            # Too many open files
            if isinstance(e, OSError) and e.errno == 24:
                logging.error("Please, set MAX_CONNECTIONS to lower value.")

            self.proxies[proto].remove(proxy)
        else:
            proxy.timeout = perf_counter() - start
            if res:
                proxy.update(res)

    async def fetch_all_sources(self) -> None:
        logging.info("Fetching sources")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; rv:102.0)"
                + " Gecko/20100101 Firefox/102.0"
            )
        }
        async with ClientSession(headers=headers) as session:
            coroutines = (
                self.fetch_source(session, source, proto)
                for proto, sources in self.sources.items()
                for source in sources
            )
            await asyncio.gather(*coroutines)

        # Remember total count so we could print it in the table
        for proto, proxies in self.proxies.items():
            self.proxies_count[proto] = len(proxies)

    async def check_all_proxies(self) -> None:
        logging.info(
            "Checking %s proxies",
            ", ".join(
                f"{len(proxies)} {proto.upper()}"
                for proto, proxies in self.proxies.items()
            ),
        )
        coroutines = [
            self.check_proxy(proxy, proto)
            for proto, proxies in self.proxies.items()
            for proxy in proxies
        ]
        shuffle(coroutines)
        await asyncio.gather(*coroutines)

    def save_proxies(self) -> None:
        """Delete old proxies and save new ones."""
        sorted_proxies = self.sorted_proxies.items()
        for folder in self.all_folders:
            folder.remove()
        for folder in self.enabled_folders:
            folder.create()
            for proto, proxies in sorted_proxies:
                text = "\n".join(
                    "{}{}".format(
                        proxy.socket_address,
                        proxy.geolocation if folder.for_geolocation else "",
                    )
                    for proxy in proxies
                    if (proxy.is_anonymous if folder.for_anonymous else True)
                )
                file = folder.path / f"{proto}.txt"
                file.write_text(text, encoding="utf-8")

    async def main(self) -> None:
        await self.fetch_all_sources()
        await self.check_all_proxies()

        logging.info("Result:")
        for proto, proxies in self.proxies.items():
            working = len(proxies)
            total = self.proxies_count[proto]
            percentage = working / total * 100 if total else 0
            logging.info(
                "%s - %d/%d (%.1f%%)",
                proto.upper(),
                working,
                total,
                percentage,
            )
        self.save_proxies()
        logging.info(
            "Proxy folders have been created in the %s folder.",
            self.path.absolute(),
        )
        logging.info("Thank you for using proxy-scraper-checker :)")

    @property
    def sorted_proxies(self) -> dict[str, list[Proxy]]:
        key: Callable[[Proxy], float] | Callable[[Proxy], tuple[int, ...]] = (
            speed_sorting_key if self.sort_by_speed else alphabet_sorting_key
        )
        return {
            proto: sorted(proxies, key=key)
            for proto, proxies in self.proxies.items()
        }


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = ConfigParser(interpolation=None)
    cfg.read("config.ini", encoding="utf-8")
    general = cfg["General"]
    folders = cfg["Folders"].getboolean
    http = cfg["HTTP"]
    socks4 = cfg["SOCKS4"]
    socks5 = cfg["SOCKS5"]
    await ProxyScraperChecker(
        timeout=general.getfloat("Timeout", 10),
        max_connections=general.getint("MaxConnections", 900),
        sort_by_speed=general.getboolean("SortBySpeed", True),
        save_path=general.get("SavePath", ""),
        proxies=folders("proxies", True),
        proxies_anonymous=folders("proxies_anonymous", True),
        proxies_geolocation=folders("proxies_geolocation", True),
        proxies_geolocation_anonymous=folders(
            "proxies_geolocation_anonymous", True
        ),
        http_sources=http.get("Sources")
        if http.getboolean("Enabled", True)
        else None,
        socks4_sources=socks4.get("Sources")
        if socks4.getboolean("Enabled", True)
        else None,
        socks5_sources=socks5.get("Sources")
        if socks5.getboolean("Enabled", True)
        else None,
    ).main()


if __name__ == "__main__":
    try:
        import uvloop
    except ImportError:
        pass
    else:
        uvloop.install()
    asyncio.run(main())
