import asyncio
from pathlib import Path
from pymobiledevice3.lockdown import LockdownClient, create_using_usbmux
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.service_connection import ServiceConnection
from pymobiledevice3.services.companion import CompanionProxyService


class CompanionWatchLockdownClient(LockdownClient):
    def __init__(self, *args, proxy, ports, open_port, **kwargs):
        super().__init__(*args, **kwargs)
        self.proxy = proxy
        self.ports = ports
        # Opens a phone-side port: through usbmux over USB, or over TCP inside a Wi-Fi RSD tunnel.
        self.open_port = open_port

    async def create_service_connection(self, remote_port, ssl_context=None, strip_ssl=False):
        local_port = await asyncio.wait_for(self.proxy.start_forwarding_service_port(remote_port), 8)
        if local_port is None:
            raise RuntimeError("Cannot forward requested Watch service.")
        self.ports.add(remote_port)
        # pymobiledevice3 passes the TLS context here when a service enables SSL.
        return await asyncio.wait_for(self.open_port(local_port, ssl_context, strip_ssl), 8)


class WatchConnection:
    """Route a trusted Watch's lockdown services through its paired iPhone, over USB or a Wi-Fi RSD tunnel."""

    def __init__(self, directory: Path, pair_watch=False, rsd=None):
        self.directory = directory
        self.pair_watch = pair_watch
        # (host, port) of the iPhone's RemoteServiceDiscovery, from `pymobiledevice3 remote start-tunnel`.
        self.rsd = rsd
        self.phone = None
        self.watch = None
        self.proxy = None
        self.initial_service = None
        self.ports = set()

    async def _connect_phone(self):
        if self.rsd is None:
            self.phone = await asyncio.wait_for(create_using_usbmux(autopair=False, connection_type="USB"), 8)
            udid = self.phone.udid

            def open_usb_port(port, ssl_context=None, strip_ssl=False):
                return ServiceConnection.create_using_usbmux(udid, port, ssl_context=ssl_context, strip_ssl=strip_ssl)
            return open_usb_port, {"system_buid": self.phone.system_buid}

        host, port = self.rsd
        self.phone = RemoteServiceDiscoveryService((host, port))
        await asyncio.wait_for(self.phone.connect(), 15)

        def open_tunnel_port(port, ssl_context=None, strip_ssl=False):
            return ServiceConnection.create_using_tcp(host, port, ssl_context=ssl_context, strip_ssl=strip_ssl)
        # The trusted tunnel authenticates this host; the Watch pair record supplies its own SystemBUID.
        return open_tunnel_port, {}

    async def __aenter__(self):
        try:
            open_port, buid = await self._connect_phone()
            self.proxy = CompanionProxyService(self.phone)
            registry = await asyncio.wait_for(self.proxy.list(), 8)
            if len(registry) != 1:
                raise RuntimeError("Expected exactly one paired Watch; no device selected.")
            port = await asyncio.wait_for(self.proxy.start_forwarding_service_port(62078), 8)
            if port is None:
                raise RuntimeError("Cannot forward Watch lockdown service.")
            self.ports.add(62078)
            cache = self.directory / "watch-pairing"
            service = await asyncio.wait_for(open_port(port), 8)
            self.initial_service = service
            self.watch = await asyncio.wait_for(CompanionWatchLockdownClient.create(
                service, identifier=registry[0], port=62078, autopair=False,
                pairing_records_cache_folder=cache, proxy=self.proxy, ports=self.ports,
                open_port=open_port, **buid
            ), 15)
            if not self.watch.paired and self.pair_watch:
                print("Unlock the Watch and accept its Trust prompt.", flush=True)
                await asyncio.wait_for(self.watch.pair(timeout=30), 35)
            if not self.watch.paired:
                raise RuntimeError("The Watch must first trust this PC.")
            if self.watch.all_values.get("DeviceClass") != "Watch":
                raise RuntimeError("Connection did not reach an Apple Watch.")

            return self.watch
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        try:
            if self.watch is not None:
                await asyncio.wait_for(self.watch.close(), 5)
            elif self.initial_service is not None:
                await asyncio.wait_for(self.initial_service.close(), 5)
        finally:
            try:
                if self.proxy is not None:
                    for port in sorted(self.ports, reverse=True):
                        try:
                            await asyncio.wait_for(self.proxy.stop_forwarding_service_port(port), 5)
                        except Exception:
                            pass
                    await asyncio.wait_for(self.proxy.close(), 5)
            finally:
                if self.phone is not None:
                    await asyncio.wait_for(self.phone.close(), 5)
