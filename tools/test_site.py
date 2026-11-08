#!/usr/bin/env python3
"""
Worbus P7, lane H (H3): the worbus.app page (site/) and its Pages workflow. Standard library only.

  site_makes_no_outside_request  no <script>, no event-handler or inline javascript:, every src, srcset, <link href>,
                                 CSS url() and @import is a relative path, the only absolute links are plain <a> to github.com
  site_links                     releases/latest, INSTALL.md and the issue chooser are linked
  site_says_not_affiliated       the fixed non-affiliation line is present
  site_credits_upstream          Navic and Feishin are named, each with a link to its upstream repository
  site_images_exist              every <img src> is a file under site/ (and its favicon, stylesheet)
  site_has_tokens                Ink and Paper as custom properties, a prefers-color-scheme: dark block, a viewport meta
  pages_workflow                 pages.yml triggers, permissions, steps (PyYAML when importable, else a line reader)

Exits non-zero on failure.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
PAGE = SITE / "index.html"
CSS = SITE / "style.css"
WORKFLOW = ROOT / ".github/workflows/pages.yml"
GH = "https://github.com/"


class Scan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {k: (v or "") for k, v in attrs}))

    def handle_data(self, data):
        self.text.append(data)


def scan() -> Scan:
    s = Scan()
    s.feed(PAGE.read_text())
    return s


def is_relative(ref: str) -> bool:
    ref = ref.strip()
    return bool(ref) and not re.match(r"^([a-z][a-z0-9+.-]*:|//)", ref, re.I)


def site_makes_no_outside_request() -> list[str]:
    if not PAGE.is_file():
        return ["site/index.html does not exist"]
    problems = []
    s = scan()
    for tag, a in s.tags:
        if tag == "script":
            problems.append("a <script> tag")
        if tag in {"iframe", "object", "embed", "form", "base"}:
            problems.append(f"a <{tag}> tag")
        for k, v in a.items():
            if k.startswith("on"):
                problems.append(f"<{tag}> has the handler {k}")
            if k == "style" and re.search(r"url\(|@import", v, re.I):
                problems.append(f"<{tag}> has an inline style that loads something")
            if "javascript:" in v.lower():
                problems.append(f"<{tag} {k}> runs javascript:")
        for k in ("src", "poster"):
            if k in a and not is_relative(a[k]):
                problems.append(f"<{tag} {k}={a[k]!r}> is not relative")
        if "srcset" in a:
            for part in a["srcset"].split(","):
                ref = part.strip().split()[0] if part.strip() else ""
                if not is_relative(ref):
                    problems.append(f"<{tag} srcset> has {ref!r}, not relative")
        if tag == "link" and not is_relative(a.get("href", "")):
            problems.append(f"<link href={a.get('href')!r}> is not relative")
        if tag == "a" and "href" in a and not is_relative(a["href"]):
            if not a["href"].startswith(GH):
                problems.append(f"<a href={a['href']!r}> is not github.com")
        if tag == "meta" and a.get("http-equiv", "").lower() == "refresh":
            problems.append("a meta refresh")
    for f in [CSS] + sorted(SITE.glob("*.svg")):
        if not f.is_file():
            continue
        t = f.read_text()
        for m in re.finditer(r"url\(\s*['\"]?([^'\")\s]+)|@import\s+(?:url\()?['\"]?([^'\")\s;]+)", t, re.I):
            ref = m.group(1) or m.group(2)
            if not is_relative(ref) and not ref.startswith("data:"):
                problems.append(f"{f.name}: loads {ref!r}")
        if re.search(r"<script|href\s*=\s*[\"']https?:", t, re.I) and f.suffix == ".svg":
            problems.append(f"{f.name}: script or outside href")
    for f in sorted(SITE.rglob("*")):
        if f.is_file() and f.suffix in {".html", ".css", ".svg"} and re.search(r"https?://(?!github\.com/)", f.read_text()):
            # a bare namespace is allowed in svg (xmlns), nothing else
            hits = [m for m in re.findall(r"https?://[^\s\"'<>)]+", f.read_text()) if m != "http://www.w3.org/2000/svg"]
            if hits:
                problems.append(f"{f.relative_to(SITE)} names another host: {hits[:2]}")
    return problems


def site_links() -> list[str]:
    if not PAGE.is_file():
        return ["site/index.html does not exist"]
    hrefs = {a["href"] for t, a in scan().tags if t == "a" and "href" in a}
    need = [GH + "illuminator69/worbus/releases/latest",
            GH + "illuminator69/worbus/blob/main/INSTALL.md",
            GH + "illuminator69/worbus/issues/new/choose"]
    return [f"no link to {n}" for n in need if n not in hrefs]


def page_text() -> str:
    return re.sub(r"\s+", " ", " ".join(scan().text))


def site_says_not_affiliated() -> list[str]:
    if not PAGE.is_file():
        return ["site/index.html does not exist"]
    return [] if "Worbus is not affiliated with Navidrome." in page_text() else ["the non-affiliation line is missing"]


def site_credits_upstream() -> list[str]:
    if not PAGE.is_file():
        return ["site/index.html does not exist"]
    s = scan()
    hrefs = {a["href"] for t, a in s.tags if t == "a" and "href" in a}
    problems = []
    for name, url in (("Navic", GH + "ssalggnikool/Navic"), ("Feishin", GH + "jeffvli/feishin")):
        if name not in page_text():
            problems.append(f"{name} is not named")
        if url not in hrefs:
            problems.append(f"no link to {url}")
    return problems


def site_images_exist() -> list[str]:
    if not PAGE.is_file():
        return ["site/index.html does not exist"]
    problems = []
    tags = scan().tags
    imgs = [a["src"] for t, a in tags if t == "img" and "src" in a]
    if len(imgs) < 3:
        problems.append(f"{len(imgs)} images, the page shows three screenshots")
    refs = imgs + [a["href"] for t, a in tags if t == "link" and "href" in a]
    for ref in refs:
        if is_relative(ref) and not (SITE / ref.split("#")[0].split("?")[0]).is_file():
            problems.append(f"{ref!r} is not a file under site/")
    for t, a in tags:
        if t == "img" and "alt" not in a:
            problems.append(f"<img src={a.get('src')!r}> has no alt attribute")
        if t == "img" and not a.get("alt", "").strip() and a.get("src") != "favicon.svg":
            problems.append(f"<img src={a.get('src')!r}> has an empty alt but is not the decorative icon")
        if t == "img" and not (a.get("width") and a.get("height")):
            problems.append(f"<img src={a.get('src')!r}> has no width and height (layout shift)")
    return problems


def site_has_tokens() -> list[str]:
    if not PAGE.is_file() or not CSS.is_file():
        return ["site/index.html or site/style.css does not exist"]
    css = CSS.read_text()
    problems = []
    for tok in ("#18211F", "#F1F3EF"):
        if tok.lower() not in css.lower():
            problems.append(f"{tok} is not in style.css")
    if not re.search(r"--[\w-]+\s*:\s*#18211F", css, re.I) or not re.search(r"--[\w-]+\s*:\s*#F1F3EF", css, re.I):
        problems.append("Ink and Paper are not custom properties")
    if "prefers-color-scheme: dark" not in css:
        problems.append("no prefers-color-scheme: dark block")
    if not any(t == "meta" and a.get("name") == "viewport" for t, a in scan().tags):
        problems.append("no viewport meta")
    return problems


def pages_workflow() -> list[str]:
    if not WORKFLOW.is_file():
        return [".github/workflows/pages.yml does not exist"]
    text = WORKFLOW.read_text()
    problems = []
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None
    if yaml is not None:
        doc = yaml.safe_load(text)
        on = doc.get(True, doc.get("on"))  # YAML 1.1 reads the key `on` as True
        if on.get("push", {}).get("branches") != ["main"]:
            problems.append("push is not limited to main")
        if on.get("push", {}).get("paths") != ["site/**", ".github/workflows/pages.yml"]:
            problems.append("push paths are not site/** and pages.yml")
        if "workflow_dispatch" not in on:
            problems.append("no workflow_dispatch")
        perms = doc.get("permissions", {})
        if perms != {"pages": "write", "id-token": "write", "contents": "read"}:
            problems.append(f"permissions are {perms}")
        uses = [st["uses"] for job in doc["jobs"].values() for st in job["steps"] if "uses" in st]
        for u in uses:
            if not re.fullmatch(r"actions/[\w-]+@v\d+", u):
                problems.append(f"{u!r} is not pinned by major version")
        want = ["actions/checkout", "actions/configure-pages", "actions/upload-pages-artifact", "actions/deploy-pages"]
        for w in want:
            if not any(u.startswith(w + "@") for u in uses):
                problems.append(f"no step uses {w}")
        steps = [st for job in doc["jobs"].values() for st in job["steps"]]
        up = [st for st in steps if st.get("uses", "").startswith("actions/upload-pages-artifact")]
        if not up or up[0].get("with", {}).get("path") != "site":
            problems.append("upload-pages-artifact path is not site")
    else:
        for needle in ("branches: [main]", "- 'site/**'", "- '.github/workflows/pages.yml'", "workflow_dispatch:", "pages: write",
                       "id-token: write", "contents: read", "actions/configure-pages@v", "actions/upload-pages-artifact@v",
                       "actions/deploy-pages@v", "path: site"):
            if needle not in text:
                problems.append(f"workflow lacks {needle!r}")
    if (SITE / "CNAME").exists():
        problems.append("site/CNAME exists; the domain is a Pages setting")
    return problems


def main() -> int:
    failed = 0
    for check in (site_makes_no_outside_request, site_links, site_says_not_affiliated, site_credits_upstream,
                  site_images_exist, site_has_tokens, pages_workflow):
        problems = check()
        if problems:
            failed += 1
            print(f"FAIL {check.__name__}")
            for p in problems:
                print(f"  {p}")
        else:
            print(f"ok   {check.__name__}")
    if failed:
        return 1
    print("PASS test_site")
    return 0


if __name__ == "__main__":
    sys.exit(main())
