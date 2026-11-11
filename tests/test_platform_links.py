"""Тест: десктопная ссылка (Windows/PWDTT) со страницы подписки панели попадает в platform_links."""
from wdtt import WdttClient

PAGE = """<html><body>
<textarea id="subscription-links">wdtt://ios-link
wdtt://android-link
wdtt://1.2.3.4:56000:56001:56003:local:
</textarea>
<textarea id="subscription-link-titles">iOS — VK Turn Proxy
Android — WDTT
Windows — PWDTT</textarea>
</body></html>"""


class _Resp:
    def __init__(self, text):
        self.status_code = 200
        self.text = text


class _StubClient:
    def __init__(self, page):
        self._page = page

    async def get(self, url, headers=None):
        return _Resp(self._page)


def _make_client(page=PAGE, vk_hash="hash1,hash2"):
    c = WdttClient.__new__(WdttClient)
    c._client = _StubClient(page)

    async def _gui(pwd):
        return {"vk_hash": vk_hash, "sub_url": "https://panel.example/sub/abc"}

    c.get_user_info = _gui
    return c


async def test_platform_links_extracts_windows():
    links = await _make_client().platform_links("pwd")
    assert set(links) == {"ios", "android", "windows"}
    # windows-ссылка — десктопная wdtt:// с хэшем из записи панели
    assert links["windows"] == "wdtt://1.2.3.4:56000:56001:56003:local:hash1,hash2"


async def test_platform_links_without_windows_entry():
    page = PAGE.replace("wdtt://1.2.3.4:56000:56001:56003:local:\n", "").replace(
        "Windows — PWDTT", "")
    links = await _make_client(page).platform_links("pwd")
    assert "windows" not in links
    assert set(links) == {"ios", "android"}


async def test_platform_links_desktop_qwdtt_title():
    page = PAGE.replace("Windows — PWDTT", "Desktop — qWDTT")
    links = await _make_client(page).platform_links("pwd")
    assert "windows" in links
