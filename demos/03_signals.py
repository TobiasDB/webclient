"""03 · Conclusions, not raw DOM.

THE POINT (vs Playwright): Playwright hands you a DOM and a shrug -- is this a SPA? is there
a login wall? You write that analysis yourself, per site. webclient runs a registry of
confidence-scored detectors and gives you FLAGS -- conclusions the pipeline (and you) act
on -- each backed by its evidence, all from the cheap static response (no browser needed).
"""

from webclient import WebClient
from _site import serve, h1, kv


def main() -> None:
    base = serve()
    with WebClient() as wc:
        h1("A login wall (static response)")
        login = wc.fetch(f"{base}/login", browser=False)
        kv("login_present", login.login_present().present)
        forms = login.forms()
        kv("forms", (forms.present, [f.field_names for f in (forms.value or [])]))

        h1("A JS-gated SPA -- detected from the empty shell, BEFORE rendering")
        shell = wc.fetch(f"{base}/spa", browser=False)   # static only, on purpose
        flag = shell.spa()
        kv("spa", flag.present)
        kv("confidence", round(flag.confidence, 2))
        kv("evidence", [s.name for s in flag.signals])   # empty_root_shell -> a bundle fills it
        kv("remedy", flag.remedy)                        # -> "browser": what auto will do
        kv("records now", len(shell.select_all("li.item")))  # 0: the shell is empty (static)

    h1("Why it matters")
    kv("act on it", "spa.remedy='browser' is exactly what browser='auto' escalates on (demo 02)")
    kv("evidence", "every flag carries the signals that produced it -- auditable, not magic")
    kv("free", "all detected from one cheap HTTP GET; no browser launched")
    kv("Playwright", "gives you the DOM; the analysis above is yours to write, per site")


if __name__ == "__main__":
    main()
