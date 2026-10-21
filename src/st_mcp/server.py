"""MCP server exposing st.com product data and technical documentation."""

from __future__ import annotations

import functools
import shutil
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import documents as docs
from .http import StClient, StError
from .site import StSite

INSTRUCTIONS = """\
Access to STMicroelectronics (st.com) product information and technical documentation
(datasheets, reference manuals, application notes, errata, user/programming manuals).

Typical flow:
1. search_products / get_product to identify the part and see its key features and orderable variants.
2. list_product_documents to find the right document (datasheet, reference manual, errata...).
3. get_document_outline, then read_document with a section or page range. Reference manuals
   are thousands of pages long, so never read them whole; use search_document to locate
   registers, parameters or keywords first.
Documents can be referenced by URL, by document code (RM0440, AN2606, DS12589, ES0431) or,
for datasheets, by part number (STM32G431KB).
"""

client = StClient()
site = StSite(client)
store = docs.DocumentStore(client, site)

mcp = MCPServer("st-docs", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

DocumentRef = Annotated[
    str,
    Field(description="Document URL from st.com, a document code like 'RM0440' / 'AN2606', or a part number for its datasheet."),
]
Format = Annotated[
    str,
    Field(
        description="'auto' (Markdown edition when ST publishes one, else PDF), 'pdf' (official, has page numbers) or 'markdown'."
    ),
]


def tool_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _table(rows: list[dict], columns: list[str] | None = None) -> str:
    if not rows:
        return "(none)"
    columns = columns or list(dict.fromkeys(k for r in rows for k in r))
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "") or "").replace("|", "\\|") for c in columns) + " |")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def search_products(
    query: Annotated[
        str, Field(description="Part number, part prefix or keywords, e.g. 'STM32G431', 'LSM6DSO', 'buck converter 3A'.")
    ],
    limit: Annotated[int, Field(ge=1, le=100)] = 15,
) -> str:
    """Search st.com for products (commercial part numbers) and return their status and page URL."""
    results = site.search_products(query, limit)
    if not results:
        return f"No products found for '{query}'."
    return _table(results, ["part", "status", "title", "url"])


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def search_documents(
    query: Annotated[str, Field(description="Part number, document code or keywords, e.g. 'STM32G4 ADC', 'AN2606', 'LSM6DSO'.")],
    doc_type: Annotated[
        str | None,
        Field(
            description="Optional filter on document type, e.g. 'Datasheet', 'Reference Manual', 'Application Note', 'Errata Sheet', 'User Manual'."
        ),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """Full-text search over st.com technical documents. Returns code, title, type, version and URL."""
    results = site.search_documents(query, doc_type, limit)
    if not results:
        types = site.doc_type_facets(query)
        hint = f" Document types available for this query: {', '.join(types)}." if types else ""
        return f"No documents found for '{query}'" + (f" with type '{doc_type}'." if doc_type else ".") + hint
    return _table(results, ["code", "title", "type", "version", "size_kb", "url"])


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def get_product(
    part: Annotated[
        str,
        Field(
            description="Part number (e.g. 'STM32G431KB', orderable codes like 'STM32G431KBT6' also work) or st.com product URL."
        ),
    ],
    include_orderable_parts: Annotated[
        bool, Field(description="Include the table of orderable part numbers (package, temperature range, price, status...).")
    ] = True,
) -> str:
    """Product overview from st.com: status, description, full feature list and orderable variants."""
    p = site.get_product(part)
    out = [f"# {p.part}", f"URL: {p.url}"]
    if p.status:
        out.append(f"Status: {p.status}")
    if p.tagline:
        out.append(f"\n{p.tagline}")
    if p.description:
        out.append(f"\n## Description\n{p.description}")
    if p.features:
        out.append(f"\n## Features\n{p.features}")
    if include_orderable_parts:
        rows = site.orderable_parts(p)
        if rows:
            out.append(f"\n## Orderable part numbers\n{_table(rows)}")
    out.append("\nUse list_product_documents for datasheet, reference manual, application notes, errata etc.")
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def list_product_documents(
    part: Annotated[str, Field(description="Part number or st.com product URL.")],
    category: Annotated[
        str | None,
        Field(
            description="Optional filter, e.g. 'Product Specifications' (datasheet), 'Reference Manuals', 'Application Notes', 'Errata Sheets', 'User Manuals'."
        ),
    ] = None,
) -> str:
    """List all documentation ST links from a product page, grouped by category, with available formats."""
    p = site.get_product(part)
    entries = site.list_documents(p)
    if category:
        key = category.lower().rstrip("s")
        entries = [e for e in entries if key in e.category.lower()]
    if not entries:
        return f"No documents found for {p.part}" + (f" in category '{category}'." if category else ".")
    out = [
        f"# Documents for {p.part}",
        "Formats: 'markdown' is ST's structured Markdown edition (best for reading); 'pdf' is the official document.",
    ]
    current = None
    for e in entries:
        if e.category != current:
            current = e.category
            out.append(f"\n## {current}")
        fmts = ", ".join(f"[{k}]({v})" for k, v in e.formats.items())
        meta = ", ".join(x for x in (f"v{e.version}" if e.version else "", e.date) if x)
        name = f"**{e.code}** {e.title}" if e.code else e.title
        out.append(f"- {name} ({meta}) — {fmts}" if meta else f"- {name} — {fmts}")
    return "\n".join(out)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def get_document_outline(
    document: DocumentRef,
    format: Format = "auto",
    max_level: Annotated[int | None, Field(ge=1, le=6, description="Only show headings up to this depth.")] = None,
    filter: Annotated[str | None, Field(description="Only show outline entries whose title contains this text.")] = None,
) -> str:
    """Table of contents of a document, with page numbers (PDF) or section indices (Markdown) to pass to read_document."""
    url = store.resolve(document, format)
    doc = store.load(url)
    entries = doc.outline
    if max_level:
        entries = [e for e in entries if e.level <= max_level]
    if filter:
        entries = [e for e in entries if filter.lower() in e.title.lower()]
    header = [
        f"# {doc.title}",
        f"URL: {doc.url}",
        f"Format: {doc.kind}, {len(doc.units)} {doc.unit_name}s, ~{doc.total_chars // 1000}k characters",
    ]
    if not doc.outline:
        header.append("This PDF has no bookmarks; use search_document or read_document with page ranges.")
        return "\n".join(header)
    lines = []
    for e in entries[:600]:
        loc = f"p.{e.unit + 1}" if doc.kind == "pdf" else f"[{e.unit}]"
        lines.append(f"{'  ' * max(0, e.level - 1)}- {e.title} {loc}")
    if len(entries) > 600:
        lines.append(f"... {len(entries) - 600} more entries; narrow with max_level or filter.")
    return "\n".join(header + [""] + lines)


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def read_document(
    document: DocumentRef,
    section: Annotated[
        str | None,
        Field(
            description="Section to read: a title (or the start of one, e.g. '7.4.11' or 'RCC_APB1RSTR1') or, for Markdown documents, a section index from the outline. Includes its subsections."
        ),
    ] = None,
    pages: Annotated[
        str | None, Field(description="PDF page range like '45' or '45-52' (1-based, as printed by the outline).")
    ] = None,
    offset: Annotated[int, Field(ge=0, description="Character offset to continue a truncated read.")] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 25000,
    format: Format = "auto",
) -> str:
    """Read part of a document as text. Choose a section or a page range; without either, reads from the start."""
    url = store.resolve(document, format)
    doc = store.load(url)
    if pages and doc.kind != "pdf":
        raise ToolError("This is a Markdown/HTML document without pages; use section (see get_document_outline) or format='pdf'.")
    if section:
        first, last = docs.find_section(doc, section)
    elif pages:
        first, last = docs.parse_page_range(pages, len(doc.units))
    else:
        first, last = 0, len(doc.units) - 1
    text = docs.render_units(doc, first, last)
    chunk = text[offset : offset + max_chars]
    span = f"pages {first + 1}-{last + 1}" if doc.kind == "pdf" else f"sections {first}-{last}"
    head = f"[{doc.title} — {span}, characters {offset}-{offset + len(chunk)} of {len(text)}]\n\n"
    tail = ""
    if offset + max_chars < len(text):
        tail = f"\n\n[truncated — call again with offset={offset + max_chars} to continue]"
    return head + chunk + tail


@mcp.tool(annotations=READ_ONLY)
@tool_errors
def search_document(
    document: DocumentRef,
    query: Annotated[str, Field(description="Text to find, e.g. a register name 'RCC_CFGR', a parameter 'VDDA', or a phrase.")],
    max_results: Annotated[int, Field(ge=1, le=200)] = 25,
    format: Format = "auto",
) -> str:
    """Find where a term occurs inside a document; returns pages/sections with snippets to read next."""
    url = store.resolve(document, format)
    doc = store.load(url)
    hits = docs.search(doc, query, max_results)
    if not hits:
        return f"'{query}' not found in {doc.title}."
    out = [f"# '{query}' in {doc.title} ({len(hits)} hits{'+' if len(hits) >= max_results else ''})"]
    for h in hits:
        loc = f"p.{h['page']}" if "page" in h else f"section [{h['section_index']}]"
        sec = f" — {h['section']}" if h.get("section") else ""
        out.append(f"- {loc}{sec}: {h['snippet']}")
    return "\n".join(out)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, openWorldHint=True))
@tool_errors
def download_document(
    document: DocumentRef,
    directory: Annotated[
        str | None, Field(description="Directory to save the file into. Defaults to the server's cache directory.")
    ] = None,
    format: Annotated[str, Field(description="'pdf' (default) or 'markdown' (zip with Markdown files and figures).")] = "pdf",
) -> str:
    """Download a document file (PDF or Markdown zip) to the local disk and return its path."""
    url = store.resolve(document, format)
    path = client.fetch_file(url)
    if directory:
        dest_dir = Path(directory).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = Path(url.split("?")[0]).name
        if name.startswith("en.") or "/content/ccc/" in url:
            name = name.removeprefix("en.")
        dest = dest_dir / name
        shutil.copyfile(path, dest)
        path = dest
    return f"Saved {url}\n→ {path} ({path.stat().st_size // 1024} KB)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
