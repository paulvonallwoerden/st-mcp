"""Scraping and JSON endpoints of st.com: search, product pages, document lists."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from .http import StClient, StError, absolute_url

SEARCH_URL = "/bin/st/search-cx/searchsite"


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _md(node: Tag | str) -> str:
    out = markdownify(str(node), strip=["a", "img"], heading_style="ATX", bullets="-")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


@dataclass
class DocumentEntry:
    code: str
    title: str
    category: str
    version: str = ""
    date: str = ""
    formats: dict[str, str] = field(default_factory=dict)  # pdf/markdown/html -> url

    @property
    def url(self) -> str:
        return next(iter(self.formats.values()), "")


@dataclass
class Product:
    part: str
    url: str
    status: str
    tagline: str
    description: str
    features: str
    product_id: str | None
    spring_endpoint: str | None
    fragments: dict[str, str]
    page_documents: list[DocumentEntry]
    attribute_labels: dict[str, str]


class StSite:
    def __init__(self, client: StClient):
        self.client = client
        self._product_cache: dict[str, tuple[float, Product]] = {}

    # ------------------------------------------------------------------ search

    def _search(self, query: str, category: str, length: int) -> dict:
        params = {
            "q": query,
            "category": category,
            "lang": "en",
            "start": 0,
            "length": length,
            "rand": int(time.time() * 1000),
            "sessionId": str(uuid.uuid4()),
        }
        return self.client.get_json(SEARCH_URL, params=params)

    def search_products(self, query: str, limit: int = 10) -> list[dict]:
        data = self._search(query, "products", limit)
        return [
            {
                "part": d.get("rpn"),
                "title": _clean(d.get("title")),
                "status": d.get("availability"),
                "url": absolute_url(d["link"]),
            }
            for d in data.get("data", [])
            if d.get("link")
        ]

    def search_documents(self, query: str, doc_type: str | None = None, limit: int = 20) -> list[dict]:
        fetch = limit if not doc_type else max(limit * 4, 100)
        data = self._search(query, "resources", fetch)
        results = []
        for d in data.get("data", []):
            kind = d.get("taxo_class") or ""
            if doc_type and doc_type.lower().replace("_", " ") not in kind.lower():
                continue
            title = _clean(d.get("title_man"))
            code, _, rest = title.partition(": ")
            results.append(
                {
                    "code": code if rest else "",
                    "title": rest or title,
                    "type": kind,
                    "version": d.get("version"),
                    "format": d.get("file_extension"),
                    "size_kb": round((d.get("size") or 0) / 1024),
                    "url": absolute_url(d["link"]) if d.get("link") else None,
                    "parts": _clean(d.get("rpn_list"))[:300],
                }
            )
            if len(results) >= limit:
                break
        return results

    def doc_type_facets(self, query: str) -> list[str]:
        data = self._search(query, "resources", 1)
        facets = data.get("facets") or {}
        return [f["name"] for f in facets.get("documentType", [])]

    # ---------------------------------------------------------------- products

    def resolve_product_url(self, part_or_url: str) -> str:
        ref = part_or_url.strip()
        if ref.startswith(("http", "/")) or "st.com" in ref:
            return absolute_url(ref)
        results = self.search_products(ref, limit=10)
        if not results:
            raise StError(f"No st.com product found for '{ref}'. Try search_products with a shorter query.")
        wanted = ref.upper()
        for r in results:
            if (r["part"] or "").upper() == wanted:
                return r["url"]
        # Orderable codes (STM32G431KBT6TR) belong to the commercial product (STM32G431KB).
        prefixes = [r for r in results if r["part"] and wanted.startswith(r["part"].upper())]
        if prefixes:
            return max(prefixes, key=lambda r: len(r["part"]))["url"]
        return results[0]["url"]

    def get_product(self, part_or_url: str) -> Product:
        url = self.resolve_product_url(part_or_url)
        cached = self._product_cache.get(url)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        html = self.client.get(url).text
        product = self._parse_product(url, html)
        self._product_cache[url] = (time.time(), product)
        return product

    def _parse_product(self, url: str, html: str) -> Product:
        fragments = {}
        for path in re.findall(r'jQuery\.get\("([^"]+_jcr_content[^"]+)"', html):
            path = path.replace("\\/", "/")
            name = path.split("_jcr_content/", 1)[1].split(".nocache", 1)[0]
            fragments[name] = path

        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "select"]):
            t.decompose()

        title_el = soup.select_one(".st-stage-product__title [data-page-title-en]")
        part = _clean(title_el.get_text()) if title_el else url.rsplit("/", 1)[-1].removesuffix(".html").upper()
        badge = soup.select_one(".st-stage-product__tags .st-badge")
        status = ""
        if badge:
            tip = badge.select_one(".st-tooltip--content")
            tip_text = _clean(tip.get_text()) if tip else ""
            if tip:
                tip.extract()
            status = _clean(badge.get_text()) + (f" ({tip_text})" if tip_text else "")
        tagline = ""
        headline = soup.select_one(".st-stage-product__headline")
        if headline:
            for el in headline.find_all(["p", "span", "div", "h2"], recursive=True):
                text = _clean(el.get_text())
                if len(text) > 30 and not el.find(["div", "p"]):
                    tagline = text
                    break
        if not tagline and (meta := soup.find("meta", attrs={"name": "description"})):
            tagline = _clean(meta.get("content"))

        desc_el = soup.select_one(".st-rpn-description-container")
        description = ""
        if desc_el:
            h = desc_el.find(["h2", "h3"])
            if h:
                h.extract()
            description = _md(desc_el)

        features = ""
        feat_h = soup.find(id="st_all-features_sec-nav-tab")
        feat_el = soup.find(id="section-1") or (feat_h.find_parent("div") if feat_h else None)
        if feat_el:
            features = _md(feat_el)
            features = re.sub(r"^#+\s*All features\s*", "", features.replace("Read more", "").replace("Read less", "")).strip()
            features = re.sub(r"^-\s*#+\s*All features\s*", "", features).strip()

        sb = soup.find(id="DataTables_Table_sample_and_buy")
        product_id = sb.get("data-product-id") if sb else None
        spring = sb.get("spring-app-endpoint") if sb else None

        page_docs = _parse_document_tables(soup)
        return Product(
            attribute_labels=_attribute_labels(sb) if sb else {},
            part=part,
            url=url,
            status=status,
            tagline=tagline,
            description=description,
            features=features,
            product_id=product_id,
            spring_endpoint=spring,
            fragments=fragments,
            page_documents=page_docs,
        )

    def list_documents(self, product: Product) -> list[DocumentEntry]:
        docs: list[DocumentEntry] = []
        path = product.fragments.get("design-resources")
        if path:
            html = self.client.get(path).text
            docs.extend(_parse_document_tables(BeautifulSoup(html, "html.parser")))
        seen = {d.url for d in docs}
        docs.extend(d for d in product.page_documents if d.url not in seen)
        return docs

    def orderable_parts(self, product: Product) -> list[dict]:
        if not (product.product_id and product.spring_endpoint):
            return []
        base = product.spring_endpoint.rstrip("/")
        parts = self.client.get_json(f"{base}/products/productsByParentId", params={"parentId": product.product_id})
        if not parts:
            return []
        labels = product.attribute_labels
        rows = []
        for p in parts:
            attrs = self.client.get_json(f"{base}/viewAttributes/getListOfViewAttributes", params={"prmisId": p["prmis_id"]})
            row = {"Part Number": p.get("item_name"), "Marketing Status": p.get("p_163")}
            if p.get("p_1387"):
                qty = p.get("p_4418")
                row["Budgetary Price (US$)"] = p["p_1387"] + (f" @ {qty} pcs" if qty else "")
            values = {str(a.get("attribute_name")): a.get("attribute_value") for a in attrs or []}
            for attr, label in labels.items():
                if values.get(attr) not in (None, ""):
                    row[label] = values[attr]
            rows.append(row)
        return rows


def _attribute_labels(table: Tag) -> dict[str, str]:
    """Map the ids of the Sample & Buy attribute API to the column headers."""
    if not table.find("thead") or not table.find("tbody"):
        return {}
    head_rows = table.find("thead").find_all("tr")
    labels: dict[str, str] = {}
    unlabeled: list[str] = []
    for th in head_rows[0].find_all("th"):
        text = _clean(th.get_text(" "))
        attr = th.get("data-attribute-id")
        if not text:
            continue
        if attr in ("1", "1387"):
            continue
        if th.get("colspan") == "2" and attr:
            labels[attr] = f"{text} min"
            continue
        if attr:
            labels.setdefault(attr, text)
        else:
            unlabeled.append(text)
    # A "min/max" pair: the max column carries its own id after the min one.
    for attr, text in list(labels.items()):
        if text.endswith(" min"):
            base = text[:-4]
            for a2, t2 in labels.items():
                if a2 != attr and t2.lower().startswith(base.lower()[:20]) and "max" in t2.lower():
                    labels[a2] = f"{base} max"
    # Columns without an id in the header: align with ids of the body cells.
    row = table.find("tbody").find("tr")
    cell_ids = [td.get("data-attribute-id") for td in row.find_all("td", recursive=False)] if row else []
    missing = [c for c in cell_ids if c and c not in labels and c not in ("1", "1387")]
    for attr, text in zip(missing, unlabeled, strict=False):
        labels[attr] = text
    # Keep table column order and make repeated headers distinguishable.
    order = {c: i for i, c in enumerate(cell_ids)}
    labels = dict(sorted(labels.items(), key=lambda kv: order.get(kv[0], len(order))))
    seen: dict[str, int] = {}
    for attr, text in labels.items():
        seen[text] = seen.get(text, 0) + 1
        if seen[text] > 1:
            labels[attr] = f"{text} ({seen[text]})"
    return labels


def _parse_document_tables(soup: BeautifulSoup) -> list[DocumentEntry]:
    docs: list[DocumentEntry] = []
    for h3 in soup.find_all("h3"):
        table = h3.find_next("table")
        if not table or not table.find("tbody"):
            continue
        # Only tables that belong to this heading.
        if table.find_previous("h3") is not h3:
            continue
        category = re.sub(r"\s*\(.*?\)\s*$", "", _clean(h3.get_text()))
        for tr in table.find("tbody").find_all("tr", recursive=False):
            entry = _parse_document_row(tr, category)
            if entry:
                docs.append(entry)
    return docs


def _parse_document_row(tr: Tag, category: str) -> DocumentEntry | None:
    links = [a for a in tr.find_all("a", href=True) if "/resource/" in a["href"] or "/content/ccc/" in a["href"]]
    if not links:
        return None
    formats: dict[str, str] = {}
    for a in links:
        href = absolute_url(a["href"])
        classes = a.get("class") or []
        name = a.get("data-file-name") or ""
        if "file-type--md" in classes or name.endswith("Markdown"):
            formats.setdefault("markdown", href)
        elif "file-type--html" in classes or href.endswith(".html"):
            formats.setdefault("html", href)
        elif href.endswith(".pdf"):
            formats.setdefault("pdf", href)
        else:
            formats.setdefault(href.rsplit(".", 1)[-1].lower(), href)
    code_el = tr.select_one(".st-title") or tr.select_one(".description .st-font--bold")
    desc_el = tr.select_one(".st-desc")
    if not desc_el:
        spans = tr.select(".description .value")
        desc_el = spans[1] if len(spans) > 1 else None
    title = _clean(desc_el.get_text()) if desc_el else _clean(links[0].get("data-file-name"))
    code = _clean(code_el.get_text()) if code_el else ""
    version = ""
    date = ""
    for td in tr.find_all("td", recursive=False):
        if td.get("data-custom-data-attribute"):
            date = _clean(td["data-custom-data-attribute"])
        elif re.fullmatch(r"\d+(\.\d+)?", _clean(td.get_text())):
            version = _clean(td.get_text())
    if not version:
        v = tr.select_one(".description .st-font-size--xs-smallest")
        version = _clean(v.get_text()) if v else ""
    return DocumentEntry(code=code, title=title, category=category, version=version, date=date, formats=formats)
