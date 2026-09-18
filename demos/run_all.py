"""Run the demos in order. HTTP-only by default; --browser adds the chromium ones.

    env/bin/python demos/run_all.py
    env/bin/python demos/run_all.py --browser
"""

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))  # so each demo can `from _site import ...`

HTTP_ONLY = ["01_declarative_query", "03_signals", "04_portable_plans",
             "05_crawl", "06_llm_skeleton", "07_onboard_author_once"]
BROWSER = ["02_auto_transport", "08_correlation", "09_live_sequence"]


def main() -> None:
    want_browser = "--browser" in sys.argv
    names = HTTP_ONLY + (BROWSER if want_browser else [])
    if not want_browser:
        print("(HTTP-only demos; add --browser for 02 / 08 / 09)\n")
    for name in sorted(names):
        print("\n" + "=" * 78 + f"\n {name}\n" + "=" * 78)
        try:
            runpy.run_path(str(HERE / f"{name}.py"), run_name="__main__")
        except Exception as exc:  # noqa: BLE001 - one demo failing shouldn't stop the tour
            print(f"  ! {name} failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
