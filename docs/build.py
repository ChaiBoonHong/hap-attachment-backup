#!/usr/bin/env python3
"""Build docs/index.html (the GitHub Pages runbook) from docs/guide.tpl.html.

@@FILE:name@@ is replaced with that file from the repository root, HTML-escaped, and
@@VERSION@@ with VERSION from hap_attachment_backup.py. Run after changing a script:

    python3 docs/build.py
"""

import html
import os
import re

DOCS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DOCS)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    tpl = read(os.path.join(DOCS, "guide.tpl.html"))
    version = re.search(r'^VERSION = "(.+)"', read(os.path.join(ROOT, "hap_attachment_backup.py")), re.M).group(1)
    out = re.sub(r"@@FILE:([\w.\-]+)@@",
                 lambda m: html.escape(read(os.path.join(ROOT, m.group(1))).rstrip("\n"), quote=False), tpl)
    out = out.replace("@@VERSION@@", version)
    if "@@" in re.sub(r"<pre>.*?</pre>", "", out, flags=re.S):
        raise SystemExit("unfilled placeholder in guide.tpl.html")
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(out)
    print(f"docs/index.html: {len(out)} bytes, version {version}")


if __name__ == "__main__":
    main()
