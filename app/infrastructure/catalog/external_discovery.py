"""Turn public merchant pages into product candidates without LLM calls.

Evidence is a timestamped merchant claim, never a verification of authenticity.
Unknown shipping, tax, stock and origin are deliberately not manufactured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import asyncio
import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit, urljoin
import httpx

from app.application.usecases.web_product_policy import prepare_product_page, requested_models
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.infrastructure.rag.web_source import TavilyWebSource, is_public_url


_PRICE = re.compile(r"(?:(USD|EUR|GBP|CNY|RMB|HKD|AUD|CAD|SGD|JPY)\s*[$€£¥￥]?\s*|([$€£¥￥])\s*)(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*(USD|EUR|GBP|CNY|RMB|HKD|AUD|CAD|SGD|JPY))?", re.I)
_EXCLUDE_PRICE = re.compile(r"shipping|orders?\s+over|save\b|discount|coupon|was\b|运费|滿|满|原价", re.I)
_CATEGORY_WORDS = {
    "户外运动": r"camp|lantern|tent|hiking|outdoor|露营|帐篷|户外",
    "户外装备": r"camp|lantern|tent|hiking|outdoor|露营|帐篷|户外",
    "旅行装备": r"luggage|suitcase|travel pillow|行李箱|旅行|颈枕",
    "数码配件": r"power bank|charger|usb hub|充电宝|充电器|扩展坞",
    "家居生活": r"bedding|towel|家居|床品|毛巾",
}


def _claim(text: str, pattern: str) -> str:
    return next((line.strip()[:350] for line in text.splitlines()
                 if re.search(pattern, line, re.I)), "")


def _price(text: str, currency_context: str = "") -> tuple[Money, str] | None:
    lines = text.splitlines()
    lines.sort(key=lambda line: 0 if re.search(r"sale price|现价|售价", line, re.I) else 1)
    for line in lines:
        if _EXCLUDE_PRICE.search(line):
            continue
        match = _PRICE.search(line)
        if not match:
            # Chinese price notation has an unambiguous currency.
            chinese = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:人民币|元)(?:\s|$)", line)
            if chinese and float(chinese[1]) > 0:
                return Money.from_major_units(float(chinese[1]), "CNY"), line.strip()[:350]
            continue
        currency = (match[1] or match[4] or "").upper()
        if not currency:
            currency = {"€": "EUR", "£": "GBP", "￥": "CNY"}.get(match[2], "")
        if not currency:
            # A bare dollar/yen is ambiguous; require page-level ISO currency.
            currencies = {item.upper() for item in re.findall(r"\b(?:USD|HKD|AUD|CAD|SGD)\b" if match[2] == "$" else r"\b(?:JPY|CNY)\b", currency_context or text, re.I)}
            if len(currencies) == 1:
                currency = currencies.pop().upper()
        if currency == "RMB":
            currency = "CNY"
        amount = float(match[3].replace(",", ""))
        if currency and amount > 0:
            return Money.from_major_units(amount, currency), line.strip()[:350]
    return None


def product_from_page(page: dict, spec: ProductSearchSpec) -> Product | None:
    if not is_public_url(str(page.get("url", ""))):
        return None
    prepared = prepare_product_page(page, spec.normalized_query)
    if not prepared and page.get("_offer") and _merchant_candidate(page, spec):
        content = str(page.get("content", ""))
        title = str(page.get("title", ""))
        heading = title.removesuffix("...").strip()
        start = content.find(heading)
        prepared = {**page, "content": content[start if start >= 0 else 0:][:4000]}
    if not prepared:
        return None
    # Product-route search can still return unrelated products. Do not infer
    # requested type from menus/body text that mention the entire catalogue.
    if re.search(r"露营灯|营地灯|camping lantern", spec.normalized_query, re.I):
        identity = prepared["title"] + " " + urlsplit(prepared["url"]).path
        if not re.search(r"lantern|camping.?light|露营灯|营地灯", identity, re.I):
            return None
    text = prepared["content"]
    offer = page.get("_offer", {})
    if offer:
        prepared["title"] = offer.get("product_name") or prepared["title"]
        prepared["url"] = offer.get("url") or prepared["url"]
        if offer.get("description"):
            text = str(offer["description"])[:4000]
    parsed_price = _price(text, str(page.get("content", "")))
    if offer:
        try:
            amount = float(offer["price"])
            if amount > 0:
                parsed_price = (Money.from_major_units(amount, str(offer["priceCurrency"]).upper()), json.dumps(offer, ensure_ascii=False)[:350])
        except (ValueError, TypeError, KeyError):
            pass
    if parsed_price is None:
        return None
    money, price_evidence = parsed_price
    url = urlunsplit(urlsplit(prepared["url"])._replace(fragment=""))
    product_id = "EXT" + sha256(url.encode()).hexdigest()[:20]
    brand_evidence = _claim(text, r"^\s*(?:brand|品牌)\s*[:：]")
    brand = re.split(r"[:：]", brand_evidence, maxsplit=1)[-1].strip() if brand_evidence else ""
    if offer.get("brand"):
        brand = str(offer["brand"])
        brand_evidence = "Merchant Product JSON-LD brand: " + brand
    category = ""
    if spec.category:
        pattern = _CATEGORY_WORDS.get(spec.category, re.escape(spec.category))
        if re.search(pattern, prepared["title"] + " " + text, re.I):
            category = spec.category
    evidence = {
        "source_url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "price_evidence": price_evidence,
        "currency_basis": "explicit currency in merchant text",
        "warranty_evidence": _claim(text, r"warranty|质保|保修"),
        "sales_evidence": _claim(text, r"\d[\d,]*\s*(?:sold|sales|reviews)|销量|已售|评价"),
        "brand_evidence": brand_evidence,
        "source_confidence": "unverified",
        "source_verification": "unknown",
        "price_verified": True,
        "availability": "out_of_stock" if re.search(r"sold\s+out|out\s+of\s+stock|售罄|缺货", text, re.I) else "unknown",
        "raw_evidence": text[:4000],
    }
    if offer:
        evidence["currency_basis"] = "exact-page Product JSON-LD offer"
        evidence["offer_evidence"] = offer
        if str(offer.get("availability", "")).endswith(("OutOfStock", "SoldOut", "Discontinued")):
            evidence["availability"] = "out_of_stock"
    warranty = re.search(r"(\d+)\s*[- ]?\s*(year|month|年|个月)[^\n]{0,20}(?:warranty|质保|保修)", text, re.I)
    if warranty:
        evidence["warranty_months"] = int(warranty[1]) * (12 if warranty[2].lower() in ("year", "年") else 1)
    sales = re.search(r"(?:已售|销量\s*[:：]?)\s*([\d,]+)|([\d,]+)\s+sold\b", text, re.I)
    if sales:
        evidence["sales_count"] = int((sales[1] or sales[2]).replace(",", ""))
    # Only destination-specific, explicitly quoted numeric costs can support
    # landed-price comparison. Generic free-shipping advertising cannot.
    destination = re.search(r"(?im)^\s*(?:ships to|配送至)\s*[:：]\s*([A-Z]{2})\s*$", text)
    if destination:
        for name, label in (("shipping_major", r"shipping cost|运费"), ("tax_major", r"tax|import duty|税费")):
            cost = re.search(r"(?im)^\s*(?:" + label + r")\s*[:：]\s*(USD|EUR|GBP|CNY|HKD|AUD|CAD|SGD|JPY)\s*([$€£¥￥]?\s*\d+(?:\.\d{1,2})?)\s*$", text)
            if cost and cost[1].upper() == money.currency:
                evidence[name] = float(re.sub(r"[^\d.]", "", cost[2]))
                evidence[name + "_evidence"] = cost[0].strip()
                evidence["cost_currency"] = money.currency
                evidence["ship_to"] = destination[1]
    return Product(product_id=product_id, title=prepared["title"], brand=brand,
                   category=category, origin_country="", description=text[:4000],
                   ships_to=[destination[1]] if destination else [], skus=[Sku(product_id + "-S1", "See linked merchant for variant", money, 0)],
                   purchase_url=url, source_type="external", evidence=evidence)


class _LinkedDataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
            self.current = ""

    def handle_data(self, data):
        if self.current is not None:
            self.current += data

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.blocks.append(self.current)
            self.current = None


def _same_page(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    return (a.hostname, a.path.rstrip("/")) == (b.hostname, b.path.rstrip("/"))


def _merchant_candidate(page: dict, spec: ProductSearchSpec) -> bool:
    url = str(page.get("url", ""))
    title = str(page.get("title", ""))
    if not is_public_url(url) or urlsplit(url).path in ("", "/"):
        return False
    if re.search(r"\b(?:review|guide|comparison|article)\b|测评|评测", title, re.I) or re.search(r"/(?:blogs?|articles?|reviews?|learn|category|collections?)/", urlsplit(url).path, re.I):
        return False
    if re.search(r"露营灯|营地灯|camping lantern", spec.normalized_query, re.I) and not re.search(r"lantern|camping.?light|露营灯|营地灯", title + url, re.I):
        return False
    wanted = requested_models(spec.normalized_query)
    return not wanted or bool(set(wanted).intersection(requested_models(title + " " + urlsplit(url).path.replace("-", " "))))


def offer_from_html(html: str, url: str) -> dict:
    """Select an exact-page Product offer, excluding recommendation widgets."""
    parser = _LinkedDataParser()
    parser.feed(html)
    nodes = []
    for block in parser.blocks:
        try:
            data = json.loads(block)
            nodes.extend(data if isinstance(data, list) else [data])
        except (ValueError, TypeError):
            continue
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if isinstance(node.get("@graph"), list):
            nodes.extend(node["@graph"])
        types = node.get("@type", [])
        if "ProductGroup" in (types if isinstance(types, list) else [types]):
            identity = node.get("url") or node.get("@id") or ""
            if isinstance(identity, str) and _same_page(urljoin(url, identity), url):
                variants = node.get("hasVariant", [])
                variants = sorted((v for v in variants if isinstance(v, dict)), key=lambda v: 0 if "InStock" in json.dumps(v.get("offers", {})) else 1)
                for variant in variants:
                    if isinstance(variant, dict):
                        nodes.append({**variant, "brand": variant.get("brand", node.get("brand")), "description": variant.get("description", node.get("description"))})
        if "Product" not in (types if isinstance(types, list) else [types]):
            continue
        identity = node.get("url") or node.get("@id") or ""
        if not isinstance(identity, str) or not _same_page(urljoin(url, identity), url):
            continue
        offers = node.get("offers", [])
        offers = offers if isinstance(offers, list) else [offers]
        # Variant ranges require a selected variant; never guess the lowest.
        prices = {(str(o.get("price")), str(o.get("priceCurrency"))) for o in offers if isinstance(o, dict)}
        if len(prices) != 1:
            continue
        for offer in offers:
            if isinstance(offer, dict) and "price" in offer and "priceCurrency" in offer:
                result = {key: offer[key] for key in ("price", "priceCurrency", "availability") if key in offer}
                result["url"] = urljoin(url, offer.get("url") or identity)
                if not _same_page(result["url"], url):
                    continue
                result["product_name"] = node.get("name", "")
                result["description"] = node.get("description", "")
                brand = node.get("brand", {})
                result["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
                return result
    return {}


async def _public_host(url: str) -> bool:
    if not is_public_url(url):
        return False
    try:
        parsed = urlsplit(url)
        records = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        return bool(records) and all(ipaddress.ip_address(row[4][0]).is_global for row in records)
    except (OSError, ValueError):
        return False


async def _merchant_offer(url: str) -> dict:
    """Read bounded public HTML; redirects are never followed implicitly."""
    if not await _public_host(url):
        return {}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            async with client.stream("GET", url, headers={"User-Agent": "MercuriusDemo/1.0"}) as response:
                if response.status_code != 200:
                    return {}
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        return {}
                    chunks.append(chunk)
                return offer_from_html(b"".join(chunks).decode("utf-8", errors="replace"), url)
    except httpx.HTTPError:
        return {}


def discovery_query(spec: ProductSearchSpec) -> str:
    """Translate common product attributes, not a guessed brand/model or budget.

    Original constraints remain in spec for scoring; provider search only needs
    discriminative product keywords instead of a conversational paragraph.
    """
    query = spec.normalized_query
    families = ((r"露营灯|营地灯|camping lantern", "camping lantern"),
                (r"帐篷|tent", "camping tent"), (r"睡袋|sleeping bag", "sleeping bag"),
                (r"充电宝|power bank", "power bank"), (r"行李箱|suitcase", "suitcase"))
    family = next((english for pattern, english in families if re.search(pattern, query, re.I)), "")
    if not family:
        return query + " buy product price"
    parts = ['"' + family + '"', *requested_models(query)]
    for pattern, english in ((r"充电|rechargeable", "rechargeable"),
                             (r"USB.?C|Type.?C", "USB-C"),
                             (r"轻便|便携|portable", "portable")):
        if re.search(pattern, query, re.I):
            parts.append(english)
    return " ".join(parts[:3]) + " buy online price"


class TavilyExternalProductDiscovery:
    def __init__(self, settings) -> None:
        self._source = TavilyWebSource(settings, rewrite_product_queries=False)
        self.last_pages = []

    async def discover(self, spec: ProductSearchSpec) -> list[Product]:
        # Search from demand; a model number is optional and never invented.
        query = discovery_query(spec)
        pages = await self._source.search(query, max_results=5)
        self.last_pages = pages
        products = {}
        semaphore = asyncio.Semaphore(3)
        parsed_urls = {}
        async def parse(page):
            url = page.get("url", "")
            if url in parsed_urls:
                return parsed_urls[url]
            product = product_from_page(page, spec)
            if product is None and _merchant_candidate(page, spec):
                identity = str(page.get("title", "")) + " " + url
                wrong_kind = re.search(r"露营灯|营地灯|camping lantern", spec.normalized_query, re.I) and not re.search(r"lantern|camping.?light|露营灯|营地灯", identity, re.I)
                if not wrong_kind:
                    async with semaphore:
                        offer = await _merchant_offer(url)
                    if offer:
                        product = product_from_page({**page, "_offer": offer}, spec)
            parsed_urls[url] = product
            return product
        for attempt in range(2):
            unique = {page.get("url"): page for page in pages}
            for product in await asyncio.gather(*(parse(page) for page in unique.values())):
                if product is not None:
                    products.setdefault(product.product_id, product)
            if any(p.evidence.get("availability") != "out_of_stock" for p in products.values()) or attempt == 1:
                break
            # One different query is permitted after an empty discovery, not
            # unlimited retries or a requirement that the buyer name a model.
            domains = sorted({urlsplit(str(page.get("url", ""))).hostname for page in pages} - {None})
            fallback = query + ' official store ' + " ".join("-site:" + host for host in domains)
            pages = await self._source.search(fallback, max_results=5)
            self.last_pages.extend(pages)
        return list(products.values())


ExternalProductDiscovery = TavilyExternalProductDiscovery
