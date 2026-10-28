import asyncio
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from watch_connection import CompanionWatchLockdownClient, WatchConnection
from watch_device import package_info


class PackageValidationTests(unittest.TestCase):
    def test_rejects_iphone_package(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Phone.app"
            app.mkdir()
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleSupportedPlatforms": ["iPhoneOS"]}))
            with self.assertRaises(ValueError):
                package_info(app)

    def test_rejects_ambiguous_ipa_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            ipa = Path(directory) / "ambiguous.ipa"
            with ZipFile(ipa, "w") as archive:
                for name in ["First", "Second"]:
                    archive.writestr(f"Payload/{name}.app/Info.plist", plistlib.dumps({}))
            with self.assertRaises(ValueError):
                package_info(ipa)


class ConnectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_continues_when_watch_close_fails(self):
        connection = WatchConnection(Path("unused"))
        connection.watch = AsyncMock()
        connection.watch.close.side_effect = ConnectionError("disconnected")
        connection.proxy = AsyncMock()
        connection.phone = AsyncMock()
        connection.ports = {62078, 1234}
        with self.assertRaises(ConnectionError):
            await connection.__aexit__()
        self.assertEqual(connection.proxy.stop_forwarding_service_port.await_count, 2)
        connection.proxy.close.assert_awaited_once()
        connection.phone.close.assert_awaited_once()

    async def test_multiple_watches_do_not_forward_any_ports(self):
        phone = AsyncMock()
        proxy = AsyncMock()
        proxy.list.return_value = ["first", "second"]
        with patch("watch_connection.create_using_usbmux", AsyncMock(return_value=phone)), \
                patch("watch_connection.CompanionProxyService", return_value=proxy):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                async with WatchConnection(Path("unused")):
                    self.fail("Ambiguous Watch selection must not connect")
        proxy.start_forwarding_service_port.assert_not_awaited()
        proxy.close.assert_awaited_once()
        phone.close.assert_awaited_once()

    async def test_rsd_tunnel_opens_forwarded_ports_over_tcp(self):
        phone = AsyncMock()
        proxy = AsyncMock()
        proxy.list.return_value = ["watch"]
        proxy.start_forwarding_service_port.return_value = 5555
        watch = AsyncMock()
        watch.paired = True
        watch.all_values = {"DeviceClass": "Watch"}
        tcp = AsyncMock()
        with patch("watch_connection.RemoteServiceDiscoveryService", return_value=phone), \
                patch("watch_connection.create_using_usbmux", AsyncMock(side_effect=AssertionError("no USB"))), \
                patch("watch_connection.CompanionProxyService", return_value=proxy), \
                patch("watch_connection.ServiceConnection.create_using_tcp", tcp), \
                patch("watch_connection.CompanionWatchLockdownClient.create", AsyncMock(return_value=watch)) as create:
            async with WatchConnection(Path("unused"), rsd=("fd00::1", 58783)) as connected:
                self.assertIs(connected, watch)
        phone.connect.assert_awaited_once()
        tcp.assert_awaited_once_with("fd00::1", 5555, ssl_context=None, strip_ssl=False)
        self.assertNotIn("system_buid", create.await_args.kwargs)


class ServiceForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_tls_context_is_passed_to_the_forwarded_port(self):
        client = CompanionWatchLockdownClient.__new__(CompanionWatchLockdownClient)
        client.proxy = AsyncMock()
        client.proxy.start_forwarding_service_port.return_value = 6000
        client.ports = set()
        client.open_port = AsyncMock(return_value="connection")
        result = await client.create_service_connection(1234, ssl_context="tls")
        self.assertEqual(result, "connection")
        client.open_port.assert_awaited_once_with(6000, "tls", False)
        self.assertEqual(client.ports, {1234})


if __name__ == "__main__":
    unittest.main()
