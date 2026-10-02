"""Conservative, deterministic product-page admission for the shopping demo.

This identifies purchase pages, not merchant trust, inventory or price validity.
Unknown storefront routes are intentionally rejected rather than guessed.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_MODEL = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,6}\d{1,5}[A-Za-z]{0,4})(?:[\s_-]+(PRO|PLUS|MAX|MINI|ULTRA))?(?![A-Za-z0-9])", re.I)
_PRICE = re.compile(r"(?:[$€£¥￥]\s*\d[\d,.]*|\b(?:USD|EUR|GBP|CNY|RMB|HKD)\s*\d[\d,.]*|\d[\d,.]*\s*(?:美元|人民币|元|(?:USD|EUR|GBP)\b))", re.I)
_PURCHASE = re.compile(r"add\s+to\s+(?:cart|bag|basket)|buy\s+now|shipping[^\n]{0,60}\bcheckout\b|sold\s+out|out\s+of\s+stock|加入购物车|加入購物車|立即购买|立即購買|立即下单|立即下單|缺货|售罄", re.I)
_EDITORIAL = re.compile(r"(?:^|/)(?:blogs?|reviews?|articles?|guides?|news|wiki|post|shopping-guide)(?:/|$)|测评|评测|評測|选购指南|購買指南|体验文章|使用心得", re.I)
_DETAIL = re.compile(r"/(?:products?|item|goods|detail|dp|gp/product)/[^/]+|/(?:[^/]+/)*\d+\.html$", re.I)


def requested_models(query: str) -> list[str]:
    """Extract explicit alphanumeric models, preserving edition suffixes."""
    models = []
    for match in _MODEL.finditer(query):
        base = match.group(1).upper()
        if re.match(r"^(?:IPX?\d|USB\d|TYPE\d)", base):
            continue
        model = base + (" " + match.group(2).upper() if match.group(2) else "")
        if model not in models:
            models.append(model)
    return models


def is_product_query(query: str) -> bool:
    """Only shopping intent activates product filtering; factual questions stay RAG."""
    return bool(re.search(r"商品|购买|購買|买一|买个|想买|想買|价格|價格|链接|連結|购物|购物车|商城|多少钱|\b(?:buy|buying|price|purchase|shopping|shop|product\s+links?)\b", query, re.I))


def prepare_product_page(page: dict, query: str) -> dict | None:
    """Return compact product evidence, or reject editorial/ambiguous/model-mismatched pages."""
    url = str(page.get("url") or page.get("source") or "")
    title = str(page.get("title") or "").strip()
    content = str(page.get("content") or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    path = unquote(parsed.path)
    if parsed.scheme not in ("http", "https") or not _DETAIL.search(path):
        return None
    if _EDITORIAL.search(path + "/" + title) or re.search(r"\b(?:review|reviewed|comparison|buying guide)\b", title, re.I):
        return None
    wanted = requested_models(query)
    offered = requested_models(title) or requested_models(path.replace("-", " ").replace("_", " "))
    if wanted and not set(wanted).intersection(offered):
        return None
    # Search headings before removing menus. Provider titles often append store names.
    heading = re.split(r"\s+[|–—-]\s+", title)[0].strip()
    truncated = bool(re.search(r"(?:\.\.\.|…)$", heading))
    heading = re.sub(r"(?:\.\.\.|…)$", "", heading).strip()
    heading_match = None
    if heading:
        candidates = list(re.finditer(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?" + re.escape(heading) + (r"[^\n]{0,160}" if truncated else "") + r"[ \t]*$", content))
        if candidates:
            # Shortest matching line avoids image captions appended with store/variant text.
            heading_match = min(candidates, key=lambda match: len(match.group()))
    if not heading_match:
        return None
    content = content[heading_match.start():].strip()
    footer = re.search(r"\n\s*(?:#{1,6}\s*)?(?:customer reviews|you may also like|related products|recently viewed|newsletter|顾客评价|用户评价|猜你喜欢)(?:\s|$|[:：])", content, re.I)
    if footer:
        content = content[:footer.start()]
    # Keep commerce evidence close to the title even when variants precede the button.
    price = next((match for match in _PRICE.finditer(content) if not re.search(
        r"shipping|orders?\s+over|save\b|discount|coupon|运费|運費|满\d|滿\d",
        content[content.rfind("\n", 0, match.start()) + 1:content.find("\n", match.end()) if content.find("\n", match.end()) >= 0 else len(content)], re.I)), None)
    purchase = _PURCHASE.search(content)
    if not price or not purchase:
        return None
    evidence = []
    for match in (price, purchase):
        start = max(content.rfind("\n", 0, match.start()) + 1, match.start() - 70)
        end = content.find("\n", match.end())
        end = min(end if end >= 0 else len(content), match.end() + 120)
        evidence.append(content[start:end].strip())
    cleaned = "\n".join(dict.fromkeys([title, *evidence, content]))[:4000]
    return {**page, "url": url, "title": title, "content": cleaned}
