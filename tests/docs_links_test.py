"""Links in README.md, CHANGELOG.md and docs/ name files and headings that exist, and the README's are absolute."""

import re
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCS = [REPO / "README.md", REPO / "CHANGELOG.md", *sorted((REPO / "docs").glob("*.md"))]
# This repository's public copy on GitHub: its front page, or a file or folder at any ref, with an optional anchor.
REPO_URL = re.compile(r"https://github\.com/harmonic-ai/aristotle(?:/(?:blob|tree)/[^/#]+/([^#]*))?(?:#(.*))?")
FENCE = re.compile(r"^ *```.*?^ *```", re.MULTILINE | re.DOTALL)


def _anchors(path: Path) -> set[str]:
    """The anchors GitHub gives the headings of ``path``."""
    seen: Counter[str] = Counter()
    anchors = set()
    for heading in re.findall(r"^#{1,6} +(.+?) *$", FENCE.sub("", path.read_text(encoding="utf-8")), re.MULTILINE):
        text = re.sub(r"\[([^]]*)\]\([^)]*\)", r"\1", heading).replace("`", "").lower()
        anchor = re.sub(r"[^\w\- ]", "", text).replace(" ", "-")
        anchors.add(f"{anchor}-{seen[anchor]}" if seen[anchor] else anchor)
        seen[anchor] += 1
    return anchors


def _links(path: Path) -> list[str]:
    return re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("doc", DOCS, ids=lambda doc: doc.name)
def test_every_link_names_a_file_and_heading_that_exist(doc: Path):
    broken = []
    for link in _links(doc):
        if match := REPO_URL.fullmatch(link):
            target, anchor = REPO / (match[1] or "README.md"), match[2]
        elif link.startswith("#"):
            target, anchor = doc, link[1:]
        elif re.match(r"[a-z]+:", link):
            continue
        else:
            path, _, anchor = link.partition("#")
            target = doc.parent / path
        if not target.exists() or (anchor and target.suffix == ".md" and anchor not in _anchors(target)):
            broken.append(link)

    assert broken == []


def test_the_readme_links_to_other_files_absolutely():
    """PyPI shows the README without the repository, so a relative link there leads nowhere."""
    relative = [link for link in _links(REPO / "README.md") if not re.match(r"[a-z]+:|#", link)]

    assert relative == []
