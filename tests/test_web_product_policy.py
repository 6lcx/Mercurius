import pytest

from app.application.usecases.web_product_policy import (
    is_product_query,
    prepare_product_page,
    requested_models,
)


def product(**overrides):
    return {"url": "https://shop.example/products/fenix-cl26r-pro-lantern",
            "title": "Fenix CL26R PRO Lantern - Fenix Lighting",
            "content": "Menu Login My account\nFenix CL26R PRO Lantern\n$79.95 USD\nAdd to cart\nUSB-C 5000mAh IPX4\nOut of stock\nCustomer reviews\nUnrelated review", **overrides}


@pytest.mark.parametrize("query", ["露营灯商品链接", "购买露营灯", "CL26R PRO 价格", "buy camping lantern", "product links for camping"])
def test_purchase_intent(query):
    assert is_product_query(query)


@pytest.mark.parametrize("query", ["露营灯的 IPX4 和 IPX7 有什么区别", "What is waterproofing?", "露营灯怎么保养"])
def test_knowledge_intent(query):
    assert not is_product_query(query)


def test_models_are_exact_editions_and_ignore_waterproof_ratings():
    assert requested_models("买CL26R PRO 或 CL28R，IPX4 USB3") == ["CL26R PRO", "CL28R"]
    assert prepare_product_page(product(), "购买 CL26R") is None
    assert prepare_product_page(product(), "购买 CL28R") is None
    assert prepare_product_page(product(), "购买 CL26R PRO 或 CL28R") is not None


@pytest.mark.parametrize("url,title", [
    ("https://example.com/blog/products/lamp", "Lamp"),
    ("https://example.com/products/lamp", "Lamp Review"),
    ("https://example.com/products/lamp", "露营灯测评"),
    ("https://example.com/category/lanterns", "Lanterns"),
    ("https://example.com/collections/lanterns", "Lanterns"),
])
def test_editorial_and_listing_pages_rejected(url, title):
    assert prepare_product_page(product(url=url, title=title), "商品链接") is None


@pytest.mark.parametrize("content", ["A useful camping lantern", "$79.95 USD technical specs", "Add to cart technical specs"])
def test_requires_price_and_purchase_evidence(content):
    assert prepare_product_page(product(content=content), "商品链接") is None


def test_cleans_navigation_and_reviews_preserves_stock_uncertainty():
    page = product()
    result = prepare_product_page(page, "CL26R PRO 商品链接")
    assert result
    assert "My account" not in result["content"]
    assert "Unrelated review" not in result["content"]
    assert "Out of stock" in result["content"]
    assert "Add to cart" in result["content"]
    assert result["content"].startswith(page["title"])
    assert page["content"].startswith("Menu")


def test_purchase_evidence_survives_compaction():
    page = product(content="Fenix CL26R PRO Lantern\n" + "technical description " * 500 + "\n$79.95 USD\nAdd to cart")
    result = prepare_product_page(page, "商品链接")
    assert result and len(result["content"]) <= 4000
    assert "$79.95" in result["content"][:512]
    assert "Add to cart" in result["content"][:512]


def test_related_product_price_does_not_qualify_main_product():
    page = product(content="Fenix CL26R PRO Lantern\nUseful specifications\nRelated products\nOther lamp $40 Add to cart")
    assert prepare_product_page(page, "商品链接") is None


def test_title_model_takes_priority_over_outdated_slug():
    page = product(url="https://example.com/products/cl26r")
    assert prepare_product_page(page, "购买 CL26R") is None
    assert prepare_product_page(page, "购买 CL26R PRO") is not None


def test_heading_not_first_navigation_mention():
    page = product(content="Link to Fenix CL26R PRO Lantern\nLogin\n# Fenix CL26R PRO Lantern\n$79.95\nAdd to cart")
    result = prepare_product_page(page, "商品链接")
    assert result and "Login" not in result["content"]


def test_chinese_price_without_latin_word_boundary():
    result = prepare_product_page(product(title="露营灯", content="露营灯\n199元包邮\n立即购买"), "商品链接")
    assert result is not None


@pytest.mark.parametrize("body", ["$79.95\nReturn within 30 days of purchase", "$79.95\nFor commercial orders and large purchases contact us", "Free shipping on orders over $25\nAdd to cart"])
def test_policy_or_shipping_text_not_product_commerce_evidence(body):
    assert prepare_product_page(product(content="Fenix CL26R PRO Lantern\n" + body), "商品链接") is None


def test_truncated_title_locates_shortest_heading_and_checkout_evidence():
    page = product(title="Fenix CL26R PRO High Performance ...", content="Free shipping over $25\nFenix CL26R PRO High Performance Lantern - Store\nImage captions\nFenix CL26R PRO High Performance Lantern\n$79.95\nShipping calculated at checkout.")
    result = prepare_product_page(page, "商品链接")
    assert result and "$25" not in result["content"]
    assert "Image captions" not in result["content"]
    assert "checkout" in result["content"][:512]


def test_no_product_heading_fails_closed():
    assert prepare_product_page(product(content="Menu\n$79.95\nAdd to cart"), "商品链接") is None
