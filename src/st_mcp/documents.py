"""Loading st.com technical documents and slicing them into readable pieces.

A document is either
  * a PDF: units are pages, the outline comes from the PDF bookmarks, or
  * a Markdown zip (ST's newer format) / HTML page: units are sections split
    at headings, the outline is the heading tree.
"""

from __future__ import annotations

import logging
import re
import threading
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymupdf
import pymupdf4llm
from bs4 import BeautifulSoup

from .http import StClient, StError, absolute_url
from .site import StSite, _clean, _md

# MuPDF is not thread-safe, and its messages must never reach stdout (the MCP channel).
_PDF_LOCK = threading.Lock()
pymupdf.set_messages(pylogging=True)
log = logging.getLogger(__name__)

# Pages rendered with table detection per read; beyond that plain text is used.
MAX_RICH_PAGES = 40

DOC_CODE_RE = re.compile(r"^(AN|RM|DS|ES|UM|PM|TN|DB|DT|TA|UG|FL|BR|TR)\d{3,5}$", re.I)

TYPE_FOLDERS = {
    "datasheet": "datasheet",
    "reference manual": "reference_manual",
    "application note": "application_note",
    "errata sheet": "errata_sheet",
    "user manual": "user_manual",
    "programming manual": "programming_manual",
    "technical note": "technical_note",
    "databrief": "data_brief",
    "data brief": "data_brief",
    "design tip": "design_tip",
}


@dataclass
class Unit:
    """A page (PDF) or a section (Markdown/HTML)."""

    label: str
    text: str
    level: int = 0
    title: str = ""


@dataclass
class OutlineEntry:
    level: int
    title: str
    unit: int  # index into Document.units


@dataclass
class Document:
    url: str
    kind: str  # "pdf" | "markdown" | "html"
    title: str
    units: list[Unit]
    outline: list[OutlineEntry]
    path: Path | None = None
    rich_pages: dict[int, str] = field(default_factory=dict)

    @property
    def unit_name(self) -> str:
        return "page" if self.kind == "pdf" else "section"

    @property
    def total_chars(self) -> int:
        return sum(len(u.text) for u in self.units)


# --------------------------------------------------------------------- loaders


def load_pdf(path: Path, url: str) -> Document:
    with _PDF_LOCK, pymupdf.open(path) as pdf:
        units = [Unit(label=f"page {i + 1}", text=_tidy_pdf_text(page.get_text(sort=False))) for i, page in enumerate(pdf)]
        outline = [
            OutlineEntry(level=lvl, title=_clean(t), unit=max(0, p - 1)) for lvl, t, p in pdf.get_toc(simple=True) if p >= 1
        ]
        title = _clean(pdf.metadata.get("title") or "") or path.stem
    return Document(url=url, kind="pdf", title=title, units=units, outline=outline, path=path)


def pdf_page_markdown(doc: Document, page: int) -> str:
    """Layout-aware Markdown (with tables) for one PDF page; plain text as fallback."""
    if page in doc.rich_pages:
        return doc.rich_pages[page]
    text = doc.units[page].text
    if doc.path:
        try:
            with _PDF_LOCK, pymupdf.open(doc.path) as pdf:
                text = (
                    pymupdf4llm.to_markdown(
                        pdf, pages=[page], use_ocr=False, header=False, footer=False, show_progress=False
                    ).strip()
                    or text
                )
        except Exception:  # noqa: BLE001 - layout analysis is best effort
            log.exception("Markdown conversion failed for page %d of %s", page + 1, doc.url)
    doc.rich_pages[page] = text
    return text


def _tidy_pdf_text(text: str) -> str:
    # Lone bullet glyphs get their own line; join them with the item text.
    text = re.sub(r"^\s*([•–◦▪-])\s*\n", r"\1 ", text, flags=re.M)
    # Register bit maps come out as one token per line; fold runs of short lines.
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    run: list[str] = []
    for ln in lines:
        if 0 < len(ln.strip()) <= 8:
            run.append(ln.strip())
            continue
        if run:
            out.append(" ".join(run) if len(run) > 3 else "\n".join(run))
            run = []
        out.append(ln)
    if run:
        out.append(" ".join(run) if len(run) > 3 else "\n".join(run))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


_TABLE_RE = re.compile(r"(?:<div[^>]*>\s*)?<table\b.*?</table>(?:\s*</div>)?", re.S | re.I)
_IMG_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)')


def _html_table_to_md(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[list[str]] = []
    for tr in soup.find_all("tr"):
        row: list[str] = []
        for cell in tr.find_all(["td", "th"]):
            for br in cell.find_all("br"):
                br.replace_with(" ")
            parts = [_clean(p.get_text(" ")) for p in cell.find_all("p")] or [_clean(cell.get_text(" "))]
            text = " / ".join(p for p in parts if p).replace("|", "\\|")
            span = int(cell.get("colspan") or 1) if str(cell.get("colspan") or "1").isdigit() else 1
            row.extend([text] * min(span, 32))
        if any(row):
            rows.append(row)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n" + "\n".join(lines) + "\n"


def _tidy_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = _TABLE_RE.sub(lambda m: _html_table_to_md(m.group(0)), text)
    text = _IMG_RE.sub(lambda m: f"[figure: {m.group(3) or m.group(1) or m.group(2)}]", text)
    text = re.sub(r"\s*\{#?[A-Za-z0-9_-]*\s*(?:\.[a-z_-]+\s*)*\}\s*$", "", text, flags=re.M)  # {#id .concept}
    text = re.sub(r"<[^>\n]{1,200}>", "", text)  # stray inline html
    text = re.sub(r"\\([_()\[\]#.!{}<>-])", r"\1", text)  # needless escapes break searching for RCC_CR etc.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


def _split_sections(markdown: str, base_level: int = 0) -> list[Unit]:
    units: list[Unit] = []
    matches = list(_HEADING_RE.finditer(markdown))
    if not matches or matches[0].start() > 0:
        head = markdown[: matches[0].start() if matches else len(markdown)].strip()
        if head:
            units.append(Unit(label="", text=head, level=base_level + 1, title="(preamble)"))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        title = _clean(m.group(2))
        units.append(Unit(label="", text=markdown[m.start() : end].strip(), level=base_level + len(m.group(1)), title=title))
    return units


def _is_minor_heading(title: str) -> bool:
    return bool(re.match(r"(figure|table)\s+\d", title, re.I))


def load_markdown_zip(path: Path, url: str) -> Document:
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.lower().endswith(".md")]
        if not names:
            raise StError(f"{url} is a zip without Markdown documentation (e.g. a model or firmware archive).")
        order: list[str] = []
        title = _title_from_slug(url)
        if "index.md" in names:
            index = z.read("index.md").decode("utf-8", "replace")
            for link in re.findall(r"\]\(([^)]+\.md)\)", index):
                link = unquote(link)
                if link in names and link not in order:
                    order.append(link)
        skip = {"index.md", "00_README.md", "Disclaimer.md", "Important_security_notice.md"}
        order += sorted(n for n in names if n not in order and n not in skip)
        order = [n for n in order if n not in skip]
        units: list[Unit] = []
        for name in order:
            text = _tidy_markdown(z.read(name).decode("utf-8", "replace"))
            if text:
                units.extend(_split_sections(text))
    return _finish_sectioned(url, "markdown", title, units)


def _title_from_slug(url: str) -> str:
    slug = Path(urlparse(url).path).stem.removesuffix("-stmicroelectronics")
    code, _, rest = slug.partition("-")
    return f"{code.upper()}: {rest.replace('-', ' ')}" if rest else slug


def load_html(html: str, url: str) -> Document:
    soup = BeautifulSoup(html, "html.parser")
    title = _clean(soup.title.get_text()) if soup.title else url
    for t in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "form", "select"]):
        t.decompose()
    main = (
        soup.find("main")
        or soup.find(id="content-main")
        or soup.find(class_=re.compile("techdoc|document-content"))
        or soup.body
        or soup
    )
    text = _md(main)
    return _finish_sectioned(url, "html", title, _split_sections(_tidy_markdown(text)))


def _finish_sectioned(url: str, kind: str, title: str, units: list[Unit]) -> Document:
    # Fold figure/table captions into their parent section so the outline stays readable.
    merged: list[Unit] = []
    for u in units:
        if merged and _is_minor_heading(u.title):
            merged[-1].text += "\n\n" + u.text
        else:
            merged.append(u)
    for i, u in enumerate(merged):
        u.label = f"section {i}"
    outline = [OutlineEntry(level=u.level, title=u.title, unit=i) for i, u in enumerate(merged)]
    return Document(url=url, kind=kind, title=title, units=merged, outline=outline)


# -------------------------------------------------------------------- resolver


class DocumentStore:
    def __init__(self, client: StClient, site: StSite, max_loaded: int = 6):
        self.client = client
        self.site = site
        self._docs: OrderedDict[str, Document] = OrderedDict()
        self._max = max_loaded
        self._lock = threading.Lock()

    def resolve(self, ref: str, fmt: str = "auto") -> str:
        """Turn a URL, a document code (RM0440) or a part number into a document URL."""
        ref = ref.strip()
        if fmt not in ("auto", "pdf", "markdown"):
            raise StError("format must be 'auto', 'pdf' or 'markdown'")
        if ref.startswith(("http", "/")) or "st.com/" in ref:
            url = absolute_url(ref)
            return self._prefer_format(url, fmt)
        if DOC_CODE_RE.match(ref):
            return self._resolve_code(ref.upper(), fmt)
        # Otherwise assume a part number and go for its datasheet.
        url = f"https://www.st.com/resource/en/datasheet/{ref.lower()}.pdf"
        if "pdf" in (self.client.exists(url) or ""):
            return url
        product = self.site.get_product(ref)
        for d in self.site.list_documents(product):
            if d.category.lower().startswith(("product specification", "datasheet")):
                return self._pick(d.formats, fmt)
        raise StError(f"Could not find a datasheet for '{ref}'. Use list_product_documents or search_documents.")

    def _pick(self, formats: dict[str, str], fmt: str) -> str:
        if fmt in ("auto", "markdown") and "markdown" in formats:
            return formats["markdown"]
        if fmt == "markdown" and "html" in formats:
            return formats["html"]
        return formats.get("pdf") or next(iter(formats.values()))

    def _prefer_format(self, url: str, fmt: str) -> str:
        path = urlparse(url).path
        if path.endswith(".html") and "/resource/" in path:
            # The HTML edition is a multi-page viewer; its siblings hold the whole document.
            stem = url[: -len(".html")]
            if fmt != "pdf" and "zip" in (self.client.exists(stem + ".zip") or ""):
                return stem + ".zip"
            return stem + ".pdf"
        if fmt == "markdown" and path.endswith(".pdf") and "/resource/" in path:
            alt = url[: -len(".pdf")] + ".zip"
            if "zip" in (self.client.exists(alt) or ""):
                return alt
            raise StError(f"No Markdown version found for {url}; use format='pdf'.")
        if fmt == "pdf" and path.endswith(".zip") and "/resource/" in path:
            return url[: -len(".zip")] + ".pdf"
        return url

    def _resolve_code(self, code: str, fmt: str) -> str:
        hits = self.site.search_documents(code, limit=20)
        match = next((h for h in hits if h["code"].upper() == code), None)
        if not match:
            return self._resolve_code_via_products(code, hits, fmt)
        # The search returns an opaque /content/ccc/... link. The nicer /resource/ URL is
        # derived from code + title; it is the one that also has Markdown siblings.
        folder = TYPE_FOLDERS.get(match["type"].lower())
        if folder:
            slug = re.sub(r"[^a-z0-9 ]", "", f"{code} {match['title']}".lower())
            slug = re.sub(r"\s+", "-", slug.strip())
            base = f"https://www.st.com/resource/en/{folder}/{slug}-stmicroelectronics"
            if fmt in ("auto", "markdown") and "zip" in (self.client.exists(base + ".zip") or ""):
                return base + ".zip"
            if fmt == "markdown":
                raise StError(f"No Markdown version of {code} found; use format='pdf'.")
            if "pdf" in (self.client.exists(base + ".pdf") or ""):
                return base + ".pdf"
        if fmt == "markdown":
            raise StError(f"No Markdown version of {code} found; use format='pdf'.")
        return match["url"]

    def _resolve_code_via_products(self, code: str, hits: list[dict], fmt: str) -> str:
        # Some documents (e.g. reference manuals) are missing from the search index, but
        # documents that mention them are found; their products link the document.
        tried: set[str] = set()
        for hit in hits:
            part = (hit.get("parts") or "").split(",")[0].strip()
            if not part or part in tried:
                continue
            tried.add(part)
            if len(tried) > 3:
                break
            try:
                product = self.site.get_product(part)
            except StError:
                continue
            for d in self.site.list_documents(product):
                if d.code.upper() == code:
                    return self._pick(d.formats, fmt)
        raise StError(f"Document {code} not found on st.com. Try list_product_documents for a related part.")

    def load(self, url: str) -> Document:
        with self._lock:
            if url in self._docs:
                self._docs.move_to_end(url)
                return self._docs[url]
        path = urlparse(url).path.lower()
        if path.endswith(".pdf") or "/content/ccc/" in path:
            doc = load_pdf(self.client.fetch_file(url), url)
        elif path.endswith(".zip"):
            doc = load_markdown_zip(self.client.fetch_file(url), url)
        else:
            resp = self.client.get(url)
            ctype = resp.headers.get("content-type", "")
            if "pdf" in ctype:
                doc = load_pdf(self.client.fetch_file(url), url)
            else:
                doc = load_html(resp.text, url)
        with self._lock:
            self._docs[url] = doc
            while len(self._docs) > self._max:
                self._docs.popitem(last=False)
        return doc


# ------------------------------------------------------------------- slicing


def parse_page_range(pages: str, total: int) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:[-–:]\s*(\d+)?)?\s*", pages)
    if not m:
        raise StError("pages must look like '12' or '12-20'")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else (start if "-" not in pages and ":" not in pages else total)
    start, end = max(1, start), min(total, max(start, end))
    if start > total:
        raise StError(f"Document has only {total} pages")
    return start - 1, end - 1


def find_section(doc: Document, section: str) -> tuple[int, int]:
    """Return (first, last) unit indices covered by a section reference."""
    ref = section.strip()
    entries = doc.outline
    idx = None
    if doc.kind != "pdf" and ref.isdigit():
        idx = int(ref)
        if idx >= len(entries):
            raise StError(f"Section index {idx} out of range (0-{len(entries) - 1})")
    else:
        low = ref.lower()
        candidates = [i for i, e in enumerate(entries) if e.title.lower() == low]
        candidates = candidates or [i for i, e in enumerate(entries) if e.title.lower().startswith(low)]
        candidates = candidates or [i for i, e in enumerate(entries) if low in e.title.lower()]
        if not candidates:
            raise StError(f"No section matching '{section}'. Call get_document_outline to see the section titles.")
        idx = candidates[0]
    entry = entries[idx]
    nxt = next((e for e in entries[idx + 1 :] if e.level <= entry.level), None)
    if doc.kind == "pdf":
        # The next section usually starts mid-page, so that page is included.
        last = max(entry.unit, nxt.unit) if nxt else len(doc.units) - 1
        return entry.unit, min(last, len(doc.units) - 1)
    last = (nxt.unit - 1) if nxt else len(doc.units) - 1
    return entry.unit, last


def render_units(doc: Document, first: int, last: int) -> str:
    parts = []
    for i in range(first, last + 1):
        u = doc.units[i]
        if doc.kind == "pdf":
            text = pdf_page_markdown(doc, i) if i - first < MAX_RICH_PAGES else u.text
            parts.append(f"--- page {i + 1} ---\n{text}")
        else:
            parts.append(u.text)
    return "\n\n".join(parts)


def search(doc: Document, query: str, max_results: int = 20, context: int = 160) -> list[dict]:
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        raise StError("Empty query")
    phrase = re.compile(re.escape(query.strip()).replace(r"\ ", r"\s+"), re.I)
    hits: list[dict] = []

    def where(i: int) -> dict:
        if doc.kind == "pdf":
            sec = None
            for e in doc.outline:
                if e.unit <= i:
                    sec = e.title
                else:
                    break
            return {"page": i + 1, "section": sec}
        return {"section_index": i, "section": doc.units[i].title}

    for i, u in enumerate(doc.units):
        for m in phrase.finditer(u.text):
            hits.append({**where(i), "snippet": _snippet(u.text, m.start(), m.end(), context)})
            if len(hits) >= max_results:
                return hits
    if hits:
        return hits
    # No exact phrase: fall back to units containing all terms.
    lowered = [t.lower() for t in terms]
    for i, u in enumerate(doc.units):
        low = u.text.lower()
        if all(t in low for t in lowered):
            pos = low.find(lowered[0])
            hits.append({**where(i), "snippet": _snippet(u.text, pos, pos + len(lowered[0]), context), "match": "all terms"})
            if len(hits) >= max_results:
                break
    return hits


def _snippet(text: str, start: int, end: int, context: int) -> str:
    a, b = max(0, start - context), min(len(text), end + context)
    s = text[a:b].replace("\n", " ")
    return ("…" if a > 0 else "") + re.sub(r"\s+", " ", s).strip() + ("…" if b < len(text) else "")
