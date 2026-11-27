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
    decode_wdtt,
    encode_openflux,
    openflux_exit_host,
    openflux_to_vless,
    personalize_link,
    wdtt_to_vless,
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


class TestPersonalize(unittest.TestCase):
    def test_roundtrip_encode(self):
        link = make_link(SAMPLE)
        self.assertEqual(decode_openflux(link)["name"], "exit-78")
        back = decode_openflux(encode_openflux(SAMPLE))
        self.assertEqual(back["secret"], SAMPLE["secret"])
        self.assertEqual(back["transports"], SAMPLE["transports"])

    def test_personalize_name_and_doc(self):
        base = make_link(SAMPLE)
        own = personalize_link(base, name="tg83137873", doc_url="https://disk.yandex.ru/i/XXXX")
        cfg = decode_openflux(own)
        self.assertEqual(cfg["name"], "tg83137873")
        self.assertEqual(cfg["context"], "https://disk.yandex.ru/i/XXXX")
        yandex = [t for t in cfg["transports"] if t["type"] == "yandex"][0]
        self.assertEqual(yandex["url"], "https://disk.yandex.ru/i/XXXX")
        # secret не меняется — он общий с exit-нодой
        self.assertEqual(cfg["secret"], SAMPLE["secret"])
        # каждый пользователь получает разную ссылку
        other = personalize_link(base, name="tg999")
        self.assertNotEqual(own, other)

    def test_personalize_keeps_shared_doc_by_default(self):
        base = make_link(SAMPLE)
        cfg = decode_openflux(personalize_link(base, name="tg1"))
        self.assertEqual(cfg.get("context", ""), SAMPLE.get("context", ""))
        yandex = [t for t in cfg["transports"] if t["type"] == "yandex"][0]
        self.assertEqual(yandex["url"], SAMPLE["transports"][1]["url"])


class TestWdttToVless(unittest.TestCase):
    UUID = "fb7d3ea5-1111-2222-3333-444455556666"

    def _b64(self, obj):
        import base64, json
        return "wdtt://" + base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()

    def test_universal_payload(self):
        link = self._b64({"ps": "WDTT", "add": "78.17.16.23", "dtls": 56000, "wg": 56001, "pass": "pw", "hash": "h1,h2"})
        d = decode_wdtt(link)
        self.assertEqual(d["host"], "78.17.16.23")
        self.assertEqual(d["dtls"], 56000)
        self.assertEqual(d["wg"], 56001)
        self.assertEqual(d["raw"], 56003)  # dtls + 3
        v = wdtt_to_vless(link, uuid=self.UUID, pbk="pbk", sid="sid", security="reality")
        self.assertTrue(v.startswith(f"vless://{self.UUID}@78.17.16.23:443?"))
        self.assertIn("security=reality", v)
        self.assertIn("pbk=pbk", v)
        # пароль/hash из wdtt не утекают в vless
        self.assertNotIn("pw", v.split("#")[0])

    def test_real_panel_payload_ip_field(self):
        # реальный payload панели: host в поле ip, есть raw/csqtt/sub
        link = self._b64({"vpn": "K.O.Gateway", "name": "ADMIN", "ip": "78.17.16.23",
                          "dtls": 56000, "raw": 56003, "csqtt": 46000, "pass": "secret", "hash": "h"})
        d = decode_wdtt(link)
        self.assertEqual(d["host"], "78.17.16.23")
        self.assertEqual(d["raw"], 56003)
        self.assertEqual(d["name"], "ADMIN")
        v = wdtt_to_vless(link, uuid=self.UUID)
        self.assertIn("@78.17.16.23:443?", v)
        self.assertNotIn("secret", v.split("#")[0])

    def test_colon_format(self):
        link = "wdtt://78.17.16.23:56000:56001:56003:local:pw123:h1,h2#myname"
        d = decode_wdtt(link)
        self.assertEqual((d["host"], d["dtls"], d["wg"], d["raw"]), ("78.17.16.23", 56000, 56001, 56003))
        self.assertEqual(d["pass"], "pw123")
        self.assertEqual(d["hash"], "h1,h2")
        self.assertEqual(d["name"], "myname")
        v = wdtt_to_vless(link, vless_server="", uuid=self.UUID, remark="")
        self.assertIn("#myname", v)

    def test_vless_server_overrides_host(self):
        link = self._b64({"add": "10.0.0.5", "dtls": 56000})
        v = wdtt_to_vless(link, vless_server="78.17.16.23", vless_port=443, uuid=self.UUID)
        self.assertIn("@78.17.16.23:443?", v)

    def test_requires_uuid(self):
        link = self._b64({"add": "78.17.16.23", "dtls": 56000})
        with self.assertRaises(LinkError):
            wdtt_to_vless(link, uuid="")

    def test_bad_link(self):
        with self.assertRaises(LinkError):
            decode_wdtt("vless://abc")
        with self.assertRaises(LinkError):
            decode_wdtt("wdtt://!!!notb64!!!")


if __name__ == "__main__":
    unittest.main()
