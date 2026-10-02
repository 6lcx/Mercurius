"""Merchant evidence parsing: no guessed logistics or authentication."""
from types import SimpleNamespace

from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.infrastructure.catalog.external_discovery import ExternalProductDiscovery, product_from_page, offer_from_html, discovery_query


def page(body="USD $39.95\nAdd to cart\n2 year warranty\nBrand: Example\n123 sold"):
    return {"url": "https://example.com/products/camping-lantern", "title": "Rechargeable Camping Lantern", "content": "Menu\nRechargeable Camping Lantern\n" + body}


def test_generic_demand_creates_real_product_without_model_or_invented_logistics():
    product = product_from_page(page(), ProductSearchSpec("露营灯 长续航", category="户外运动"))
    assert product.purchase_url == page()["url"]
    assert product.source_type == "external"
    assert product.primary_sku().price.to_major_units() == 39.95
    assert product.primary_sku().stock == 0
    assert product.origin_country == "" and product.ships_to == []
    assert product.category == "户外运动"
    assert product.evidence["source_verification"] == "unknown"
    assert product.evidence["warranty_months"] == 24
    assert product.evidence["sales_count"] == 123
    assert "shipping_major" not in product.evidence
    assert "tax_major" not in product.evidence
    assert "Menu" not in product.description


def test_ambiguous_currency_does_not_become_usd():
    assert product_from_page(page("$39.95\nAdd to cart"), ProductSearchSpec("lantern")) is None


def test_shipping_threshold_not_product_price_and_claim_not_authentication():
    product = product_from_page(page("Free shipping orders over USD $100\nUSD $39.95\nAdd to cart\nOfficial genuine store"), ProductSearchSpec("lantern"))
    assert product.primary_sku().price.to_major_units() == 39.95
    assert product.evidence["source_verification"] == "unknown"
    assert product.evidence["warranty_evidence"] == ""


def test_editorial_and_private_links_rejected():
    for url in ("https://example.com/blog/camping-lantern", "http://127.0.0.1/products/lantern"):
        assert product_from_page({**page(), "url": url}, ProductSearchSpec("lantern")) is None


def test_unknown_category_not_copied_from_user_request():
    assert product_from_page(page(), ProductSearchSpec("lantern", category="护肤美容")).category == ""


def test_explicit_destination_costs_retained_without_turning_free_shipping_into_zero_tax():
    product = product_from_page(page("USD $39.95\nAdd to cart\nShips to: CN\nShipping cost: USD 5\nTax: USD 0"), ProductSearchSpec("lantern"))
    assert product.evidence["shipping_major"] == 5
    assert product.evidence["tax_major"] == 0
    assert product.evidence["ship_to"] == "CN"
    assert product.ships_to == ["CN"]


async def test_discovery_deduplicates_and_queries_need_not_model(monkeypatch):
    discovery = ExternalProductDiscovery(SimpleNamespace(tavily_api_key="fixture-only"))
    queries = []
    async def search(query, max_results):
        queries.append(query)
        return [page(), page()]
    monkeypatch.setattr(discovery._source, "search", search)
    result = await discovery.discover(ProductSearchSpec("露营灯 续航长"))
    assert len(result) == 1
    assert "camping lantern" in queries[0]


def test_sale_price_preferred_but_regular_price_valid():
    spec = ProductSearchSpec("lantern")
    assert product_from_page(page("Regular price USD $39.95\nAdd to cart"), spec).primary_sku().price.to_major_units() == 39.95
    assert product_from_page(page("Regular price USD $39.95\nSale price USD $29.95\nAdd to cart"), spec).primary_sku().price.to_major_units() == 29.95


def test_conversational_demand_becomes_product_keywords_without_invented_model():
    query = discovery_query(ProductSearchSpec("我不知道买哪个型号，双人雨天露营灯，希望充电方便续航长"))
    assert "camping lantern" in query and "rechargeable" in query
    assert '"camping lantern"' in query and "buy online price" in query
    assert " OR " not in query
    assert "我不知道" not in query and "Fenix" not in query


def test_captured_unrelated_provider_products_do_not_pass_requested_kind():
    for url, title in (("https://pebblebee.com/products/card-5", "Card 5"),
                       ("https://www.ti.com/product-category/battery-management-ics/battery-fuel-gauges/overview.html", "Battery fuel gauges | TI.com")):
        assert product_from_page({"url": url, "title": title, "content": title + "\nUSD $20\nAdd to cart"}, ProductSearchSpec("露营灯")) is None


async def test_empty_results_have_only_one_distinct_fallback(monkeypatch):
    discovery = ExternalProductDiscovery(SimpleNamespace(tavily_api_key="fixture"))
    calls = []
    async def search(query, max_results):
        calls.append(query)
        return []
    monkeypatch.setattr(discovery._source, "search", search)
    assert await discovery.discover(ProductSearchSpec("露营灯")) == []
    assert len(calls) == 2 and calls[0] != calls[1]


def test_jsonld_offer_must_be_for_exact_product_not_related():
    import json
    node = {"@type": "Product", "url": page()["url"], "offers": {"price": "39.95", "priceCurrency": "USD"}}
    html = '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    assert offer_from_html(html, page()["url"])["priceCurrency"] == "USD"
    assert offer_from_html(html, "https://example.com/products/other") == {}
    node["offers"] = [{"price": "39.95", "priceCurrency": "USD"}, {"price": "59.95", "priceCurrency": "USD"}]
    assert offer_from_html('<script type="application/ld+json">' + json.dumps(node) + '</script>', page()["url"]) == {}


def test_productgroup_missing_text_price_uses_explicit_variant_and_out_of_stock():
    import json
    url = page()["url"]
    node = {"@type": "ProductGroup", "url": url, "brand": {"name": "Example"}, "hasVariant": [
        {"@type": "Product", "@id": "/products/camping-lantern?variant=1#variant", "name": "Camping Lantern - Black", "offers": {"price": "39.99", "priceCurrency": "USD", "availability": "https://schema.org/OutOfStock", "url": url + "?variant=1"}}]}
    offer = offer_from_html('<script type="application/ld+json">' + json.dumps(node) + '</script>', url)
    result = product_from_page({**page("Rechargeable outdoor lighting"), "_offer": offer}, ProductSearchSpec("露营灯"))
    assert result.primary_sku().price.to_major_units() == 39.99
    assert result.purchase_url.endswith("?variant=1")
    assert result.evidence["availability"] == "out_of_stock"


async def test_jsonld_resolves_currency_without_assuming_dollar(monkeypatch):
    from app.infrastructure.catalog import external_discovery as module
    discovery = ExternalProductDiscovery(SimpleNamespace(tavily_api_key="fixture-only"))
    async def search(query, max_results):
        return [page("$39.95\nAdd to cart")]
    async def offer(url):
        return {"price": "39.95", "priceCurrency": "CAD"}
    monkeypatch.setattr(discovery._source, "search", search)
    monkeypatch.setattr(module, "_merchant_offer", offer)
    products = await discovery.discover(ProductSearchSpec("lantern"))
    assert products[0].primary_sku().price.currency == "CAD"
    assert products[0].evidence["currency_basis"] == "exact-page Product JSON-LD offer"
