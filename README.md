# st-mcp

An MCP server that lets MCP clients look up STMicroelectronics
parts and read their technical documentation on st.com: datasheets, reference manuals,
application notes, errata sheets, and user and programming manuals.

## Why this exists

st.com is behind Akamai bot protection. Akamai fingerprints the TLS/HTTP2 handshake, so plain
`curl`/`requests`/WebFetch get their connection reset. This server uses
[curl_cffi](https://github.com/lexiforest/curl_cffi) to present a real browser handshake, then
uses the same JSON endpoints and page fragments the site's own frontend uses. It doesn't need a
headless browser.

Requests are serialized and rate limited, and downloaded documents are cached on disk
(`~/.cache/st-mcp`, override with `ST_MCP_CACHE_DIR`). It is meant for one person reading
documentation, not for crawling the site.

## Tools

| Tool | What it does |
|---|---|
| `search_products` | Find parts by number, prefix or keywords |
| `get_product` | Status, description, full feature list, and orderable part numbers (package, temperature range, budgetary price, ECCN…) |
| `list_product_documents` | All documents linked from a product page, grouped by category, with PDF and Markdown links |
| `search_documents` | Full-text search over ST's document library, optionally filtered by type |
| `get_document_outline` | Table of contents with page numbers (PDF) or section indices (Markdown) |
| `read_document` | Read a section (by title, number or register name) or a page range, paginated by characters |
| `search_document` | Find a term (register, parameter, phrase) inside a document, with snippets |
| `download_document` | Save the PDF (or Markdown zip) locally |

You can reference a document by URL, by code (`RM0440`, `AN2606`, `ES0431`, `DS12589`), or, for
datasheets, by part number (`STM32G431KB`).

Reading documents:

- **Markdown edition** (`format="auto"`, the default when ST publishes one): ST's newer zip of
  Markdown chapters. The embedded HTML tables, such as register bit maps and field descriptions,
  are converted to compact Markdown tables. It is split into sections, so `read_document(section="RCC_CFGR")` works.
- **PDF**: the full text is indexed for search. Pages you read are rendered with
  layout analysis (pymupdf4llm), so electrical-characteristics tables come out as Markdown
  tables.

## Install

Requires [uv](https://docs.astral.sh/uv/).

```json
{
  "mcpServers": {
    "st-docs": {
      "command": "uv",
      "args": ["run", "--quiet", "--directory", "/path/to/st-mcp", "st-mcp"]
    }
  }
}
```

## Development

```sh
uv run python tests/smoke_stdio.py   # starts the server over stdio and calls tools against live st.com
uv run mcp dev src/st_mcp/server.py  # MCP inspector
```

Code layout:

- `http.py`: browser-impersonating client, retries/fingerprint rotation, file cache
- `site.py`: search API, product page parsing, document tables, Sample & Buy data
- `documents.py`: PDF / Markdown-zip loading, outline, section/page slicing, search
- `server.py`: MCP tool definitions

If st.com changes its markup, the parsers in `site.py` are where things will break first.
