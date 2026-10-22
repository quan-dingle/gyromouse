from pathlib import Path
import os

import webview

from .api import DesktopAPI


def main():
    root = Path(__file__).resolve().parent.parent
    ui = root / "ui" / "index.html"
    api = DesktopAPI()
    window = webview.create_window(
        "Gyromouse",
        ui.as_uri(),
        js_api=api,
        width=1380,
        height=940,
        min_size=(1024, 720),
        background_color="#090d14",
        text_select=True,
    )
    window.events.closed += api.shutdown
    webview.start(debug=os.environ.get("GYROMOUSE_DEBUG", "0") == "1", http_server=True)
