"""Офлайн-фикстура actual hh composer: стороннее textarea и перекрытие поля не перехватывают ответ."""

from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402
from hh_auto.hh import chats  # noqa: E402


HTML = """<!doctype html><meta charset="utf-8"><style>
textarea { width:300px; height:70px; } #field {position:relative;width:320px;height:90px;}
#cover {position:absolute;inset:0;background:#eee;opacity:.1;z-index:2;}
</style><div data-qa="chatik-message-input"><div id="field">
<textarea data-qa="text-input" id="real" placeholder="Message"></textarea><div id="cover"></div></div>
<button aria-label="Отправить" id="send" onclick="window.clicks++; throw Error('native send button must not be clicked')">Send</button>
<button data-qa="record-complete" aria-label="Отправить запись" onclick="window.clicks++; throw Error('voice must not be sent')">Voice</button>
</div><textarea id="decoy"></textarea><div id="messages"></div><script>
window.sent = []; window.clicks = 0;
const sendText = () => {
 const box = document.querySelector('#real'); window.sent.push(box.value);
 document.querySelector('#messages').textContent = box.value; box.value = '';
};
document.querySelector('#real').addEventListener('keydown', (event) => {
 if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); sendText(); }
});</script>"""


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content(HTML)
        with patch.object(chats.control, "sleep"), patch.object(
                chats, "_transcript", side_effect=lambda page: page.locator("#messages").inner_text()):
            assert chats._send(page, "Offline fixture answer")
        assert page.evaluate("window.sent") == ["Offline fixture answer"]
        assert page.evaluate("window.clicks") == 0
        assert page.locator("#decoy").input_value() == ""
        page.locator("#real").evaluate("el => el.disabled = true")
        try:
            chats._send(page, "Must not be sent")
        except chats._SendNotAttempted:
            pass
        else:
            raise AssertionError("A disabled composer must fail before sending")
        assert page.evaluate("window.sent") == ["Offline fixture answer"]
        assert page.evaluate("window.clicks") == 0
        context.close()
        browser.close()
    print("Offline composer fixture passed: scoped input, Enter only, no button/voice/decoy send, safe pre-send failure.")


if __name__ == "__main__":
    main()
