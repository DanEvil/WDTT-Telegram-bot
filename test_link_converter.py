"""Тесты link_converter: encode/decode openflux:// и конвертация в vless://."""
import base64
import json
import unittest
import zlib

from link_converter import (
    PREFIX,
    LinkError,
    build_vless_url,
    decode_openflux,
    openflux_exit_host,
    openflux_to_vless,
)


def make_link(cfg: dict) -> str:
    raw = json.dumps(cfg).encode()
    packed = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = base64.urlsafe_b64encode(packed.compress(raw) + packed.flush()).decode().rstrip("=")
    return PREFIX + body


SAMPLE = {
    "name": "exit-78",
    "negotiate": False,
    "codec": "batched",
    "secret": "0" * 64,
    "transports": [
        {"type": "direct", "priority": 1, "dial": "78.17.16.23:56004"},
        {"type": "yandex", "priority": 2, "url": "https://docs.yandex.ru/..."},
    ],
}

VLESS_PARAMS = dict(
    vless_server="78.17.16.23",
    uuid="fb7d3ea5-0dd1-4991-b05c-f06c7e958caf",
    pbk="UwSaTq779-9Q-f0J3XY5SaPch2E8aqafM7xWkZs6gCA",
    sid="8a9eea9a38c41c52",
    sni="www.samsung.com",
)


class TestDecode(unittest.TestCase):
    def test_roundtrip(self):
        cfg = decode_openflux(make_link(SAMPLE))
        self.assertEqual(cfg["secret"], "0" * 64)
        self.assertEqual(len(cfg["transports"]), 2)

    def test_tolerates_padding_and_whitespace(self):
        link = make_link(SAMPLE)
        padded = PREFIX + link[len(PREFIX):] + "=="
        wrapped = padded[:20] + "\n  " + padded[20:]
        self.assertEqual(decode_openflux(wrapped)["name"], "exit-78")

    def test_rejects_garbage(self):
        for bad in ("", "vless://abc", "openflux://v2/abc", PREFIX + "!!!not-base64!!!"):
            with self.assertRaises(LinkError):
                decode_openflux(bad)


class TestConvert(unittest.TestCase):
    def test_exit_host(self):
        self.assertEqual(openflux_exit_host(SAMPLE), "78.17.16.23")
        self.assertIsNone(openflux_exit_host({"transports": [{"type": "yandex"}]}))

    def test_to_vless(self):
        link = make_link(SAMPLE)
        vless = openflux_to_vless(link, **VLESS_PARAMS, remark="WDTT-Happ")
        self.assertTrue(vless.startswith("vless://fb7d3ea5-0dd1-4991-b05c-f06c7e958caf@78.17.16.23:443?"))
        for part in ("security=reality", "type=tcp", "flow=xtls-rprx-vision",
                     "sni=www.samsung.com", "fp=chrome",
                     "pbk=UwSaTq779-9Q-f0J3XY5SaPch2E8aqafM7xWkZs6gCA",
                     "sid=8a9eea9a38c41c52", "#WDTT-Happ"):
            self.assertIn(part, vless)

    def test_strict_host_mismatch(self):
        link = make_link(SAMPLE)
        params: dict = dict(VLESS_PARAMS, vless_server="1.2.3.4", strict_host=True)
        with self.assertRaises(LinkError):
            openflux_to_vless(link, **params)

    def test_build_vless_plain(self):
        u = build_vless_url("u", "h", 8443, security="none", flow="")
        self.assertEqual(u, "vless://u@h:8443?encryption=none&security=none&type=tcp&fp=chrome#OpenFlux-%3EVLESS")


if __name__ == "__main__":
    unittest.main()
