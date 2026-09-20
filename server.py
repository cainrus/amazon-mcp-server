# This is amazon products scraper mcp server
# Build a scraper that can scrape amazon products
# The scraper should be able to scrape the product name, price, and image
import asyncio
import httpx
import os
from mcp.server.fastmcp import FastMCP
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote_plus

# Create a Trello MCP server
mcp = FastMCP(
    "Amazon Scraper", 
    instructions="""
    # Amazon Scraper Server
    
    This server provides access to Amazon products through various tools.
    For search products, identify the keywords and number of results you want to get from the user input
    
    ## Available Tools
    - `scrape_product(product_url)` - Scrape a product from Amazon
    - `search_products(query, max_results)` - Search for products on Amazon
    
    ## When to use what
    - For getting product details: Use `scrape_product(product_url)`
    - For searching products: Use `search_products(query, max_results)`
    
    ## Notes
    - No API key required
        """
)

# Constants
# BASE_URL = "https://api.trello.com/1"
# API_KEY = os.getenv("TRELLO_API_KEY")
# API_TOKEN = os.getenv("TRELLO_API_TOKEN")
BASE_URL = "https://www.amazon.com"

# Amazon's storefronts do NOT share a catalogue. A European brand can be absent
# from .com entirely, and the search then answers with unrelated products rather
# than an empty result -- so the domain is a correctness knob, not a preference
# (finding #2529).
DEFAULT_DOMAIN = "com"
# Suffix only: letters and dots, 2..6 chars ("com", "de", "co.uk", "com.br").
_DOMAIN_SUFFIX_RE = re.compile(r'^[a-z]{2,3}(\.[a-z]{2,3})?$')


def resolve_domain(domain: str | None = None) -> str:
    """Turn a domain hint into an Amazon base URL.

    Accepts "de", "amazon.de", "www.amazon.de" or a full URL; falls back to
    $AMAZON_DOMAIN and then to .com. Only ever returns an amazon.<suffix> host:
    the suffix is validated and the URL rebuilt from scratch, so a hostile
    value cannot point the fetcher at an unrelated host.
    """
    raw = (domain or os.environ.get("AMAZON_DOMAIN") or DEFAULT_DOMAIN).strip().lower()

    # Strip scheme and path, keeping only the host. A path is tolerated only
    # when it is empty ("https://www.amazon.de/"): anything else is a sign the
    # caller meant something we would have to guess at, and guessing is how a
    # wrong storefront gets used silently.
    if "//" in raw:
        parsed = urlparse(raw)
        if parsed.path not in ("", "/") or parsed.query or parsed.params:
            raise ValueError(f"Pass a domain, not a URL with a path: {domain!r}")
        raw = parsed.netloc or ""
    elif "/" in raw:
        host, _, rest = raw.partition("/")
        if rest:
            raise ValueError(f"Pass a domain, not a path: {domain!r}")
        raw = host
    raw = raw.strip().rstrip(".")

    if raw.startswith("www."):
        raw = raw[4:]
    if raw.startswith("amazon."):
        suffix = raw[len("amazon."):]
    elif "." in raw and not _DOMAIN_SUFFIX_RE.match(raw):
        # Something like "evil.com" -- a host, but not Amazon's.
        raise ValueError(f"Not an Amazon domain: {domain!r}")
    else:
        suffix = raw

    if not _DOMAIN_SUFFIX_RE.match(suffix):
        raise ValueError(
            f"Unsupported Amazon domain {domain!r}. Pass a suffix like "
            f"'com', 'de' or 'co.uk'."
        )

    return f"https://www.amazon.{suffix}"


def build_search_url(query: str, base_url: str) -> str:
    """Search URL for `query` on the given storefront."""
    return f"{base_url}/s?k={quote_plus(query)}"


_TOKEN_RE = re.compile(r'[a-z0-9]+')


def _tokens(text: str) -> set:
    """Comparable tokens: lowercase alphanumerics of 2+ chars.

    Two chars, not three, because model numbers matter here -- "4G" and "X6"
    are exactly the tokens that separate the product asked for from a lookalike.
    """
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2}


# Words that mark a listing as something you put ON a device, not the device.
# An accessory repeats the product's full name ("screen protector for Elari
# KidPhone 4G"), so it scores a perfect match while the device itself may be
# absent from the results entirely.
_ACCESSORY_RE = re.compile(
    r'\b('
    r'case|cover|protector|protectors|screen guard|tempered glass|glass film|'
    r'film|skin|sleeve|pouch|holster|strap|straps|band|bands|wristband|'
    r'charger|charging cable|cable|adapter|dock|stand|mount|lanyard|'
    r'h[üu]lle|schutzfolie|schutzglas|panzerglas|displayschutz|armband|tasche|'
    r'ladeger[äa]t|ladekabel'
    r')\b',
    re.IGNORECASE,
)


def looks_like_accessory(name: str) -> bool:
    """True if the listing name reads like an accessory rather than a device."""
    return bool(_ACCESSORY_RE.search(name or ""))


def best_matching_product(query: str, products: list) -> dict | None:
    """The single result that covers most of the query's tokens."""
    wanted = _tokens(query)
    if not wanted or not products:
        return None

    return max(
        products,
        key=lambda p: len(wanted & _tokens(p.get("name") or "")),
        default=None,
    )


def best_query_coverage(query: str, products: list) -> float:
    """Largest share of the query's tokens covered by any single result name.

    1.0 means some result contains every word of the query; 0.0 means no result
    contains any of them -- which is what a substituted search looks like.
    """
    wanted = _tokens(query)
    if not wanted or not products:
        return 0.0

    best = 0.0
    for product in products:
        name = product.get("name") or ""
        hits = len(wanted & _tokens(name))
        best = max(best, hits / len(wanted))
    return best


# Helper functions

class AmazonBlockedError(Exception):
    """Amazon answered with a bot-mitigation challenge instead of the page."""


def is_bot_challenge(html: str) -> bool:
    """True if `html` is an Akamai interstitial challenge, not real content.

    Without this check, a temporary bot challenge and a genuine zero-result
    search both surface as "No products found" -- indistinguishable, and
    silently wrong (finding #2514). This is detection only: it does not
    attempt to solve the challenge or otherwise defeat the bot mitigation.
    """
    markers = ('bm-verify', 'triggerInterstitialChallenge', 'validateCaptcha')
    return any(marker in html for marker in markers)


async def fetch_amazon_page(url: str, retries: int = 1) -> str:
    """Fetch an Amazon page, retrying once if Amazon answers with a bot
    challenge -- this has been observed to be transient (finding #2514)."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    async with httpx.AsyncClient() as client:
        for attempt in range(retries + 1):
            response = await client.get(url, headers=headers, timeout=15.0)
            response.raise_for_status()
            html = response.text
            if not is_bot_challenge(html):
                return html
            if attempt < retries:
                await asyncio.sleep(2)

    raise AmazonBlockedError(
        "Amazon ответил анти-бот проверкой (JS-челлендж, не картиночная капча) "
        "вместо страницы — это не пустая выдача, попробуйте запрос ещё раз "
        "через минуту-другую."
    )

CURRENCY_SYMBOLS = '$€£¥₹₩'

def clean_price(price_text: str) -> str:
    """Clean and extract price from text, preserving whatever currency Amazon
    reported instead of assuming USD -- amazon.com itself geolocates by IP and
    quotes EUR to a European visitor (finding #2512)."""
    if not price_text:
        return "Price not available"

    text = price_text.strip()
    if not text:
        return "Price not available"

    amount_match = re.search(r'\d[\d.,]*', text)
    if not amount_match:
        return "Price not available"
    amount = amount_match.group(0)

    symbol_match = re.search(f'[{CURRENCY_SYMBOLS}]', text)
    if symbol_match:
        return f"{symbol_match.group(0)}{amount}"

    # Not \b[A-Z]{3}\b: Amazon runs the code straight into the amount with no
    # separator ("EUR60.93"), and \b never fires between a letter and a digit
    # -- both count as "word" characters, so there is no boundary there.
    code_match = re.search(r'(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])', text)
    if code_match:
        return f"{code_match.group(0)} {amount}"

    return amount

def extract_product_data(html_content: str, url: str) -> dict:
    """Extract product information from Amazon page HTML"""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Initialize product data
    product_data = {
        'name': 'Product name not found',
        'price': 'Price not available',
        'image_url': 'Image not found',
        'rating': 'Rating not available',
        'reviews_count': 'Reviews not available',
        'availability': 'Availability not found',
        'description': 'Description not available',
        'url': url
    }
    
    try:
        # Extract product name
        name_selectors = [
            '#productTitle',
            'h1.a-size-large',
            '.a-size-large.product-title-word-break',
            'h1[data-automation-id="product-title"]'
        ]
        
        for selector in name_selectors:
            name_elem = soup.select_one(selector)
            if name_elem:
                product_data['name'] = name_elem.get_text().strip()
                break
        
        # Extract price. `.a-offscreen` variants carry the FULL formatted
        # price (currency + cents, e.g. "$19.99" / "EUR 61.04") and are tried
        # first; `.a-price-whole` is checked earlier than them because it
        # matches most often, but it's Amazon's whole-dollar-only display
        # fragment (no cents, no symbol) so it goes last as a fallback.
        price_selectors = [
            '.a-price .a-offscreen',
            '.a-price-range .a-price-range-min .a-offscreen',
            '.a-price .a-price-symbol + span',
            '[data-a-color="price"] .a-offscreen',
            '.a-price-whole',
        ]
        
        for selector in price_selectors:
            price_elem = soup.select_one(selector)
            if price_elem:
                product_data['price'] = clean_price(price_elem.get_text())
                break
        
        # Extract image URL
        image_selectors = [
            '#landingImage',
            '#imgBlkFront',
            '.a-dynamic-image',
            '[data-old-hires]'
        ]
        
        for selector in image_selectors:
            img_elem = soup.select_one(selector)
            if img_elem:
                img_url = img_elem.get('src') or img_elem.get('data-old-hires')
                if img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    product_data['image_url'] = img_url
                    break
        
        # Extract rating
        rating_selectors = [
            '.a-icon-alt',
            '[data-hook="rating-out-of-text"]',
            '.a-icon-star-small .a-icon-alt'
        ]
        
        for selector in rating_selectors:
            rating_elem = soup.select_one(selector)
            if rating_elem:
                rating_text = rating_elem.get_text()
                rating_match = re.search(r'(\d+\.?\d*)', rating_text)
                if rating_match:
                    product_data['rating'] = f"{rating_match.group(1)} out of 5"
                    break
        
        # Extract reviews count
        reviews_selectors = [
            '#acrCustomerReviewText',
            '[data-hook="total-review-count"]',
            '.a-size-base.s-underline-text'
        ]
        
        for selector in reviews_selectors:
            reviews_elem = soup.select_one(selector)
            if reviews_elem:
                reviews_text = reviews_elem.get_text()
                reviews_match = re.search(r'(\d+(?:,\d+)*)', reviews_text)
                if reviews_match:
                    product_data['reviews_count'] = f"{reviews_match.group(1)} reviews"
                    break
        
        # Extract availability
        availability_selectors = [
            '#availability .a-size-medium',
            '#availability span',
            '.a-size-medium.a-color-success'
        ]
        
        for selector in availability_selectors:
            avail_elem = soup.select_one(selector)
            if avail_elem:
                product_data['availability'] = avail_elem.get_text().strip()
                break
        
        # Extract description
        desc_selectors = [
            '#productDescription p',
            '#feature-bullets .a-list-item',
            '.a-expander-content p'
        ]
        
        for selector in desc_selectors:
            desc_elem = soup.select_one(selector)
            if desc_elem:
                product_data['description'] = desc_elem.get_text().strip()
                break
                
    except Exception as e:
        product_data['error'] = f"Error parsing product data: {str(e)}"
    
    return product_data

# Helper functions for search results

def extract_search_results(html_content: str, max_results: int, base_url: str = BASE_URL) -> list:
    """Extract product information from Amazon search results.

    `base_url` is the storefront the HTML came from: relative links must be
    resolved against it, or a .de result gets a .com URL that may 404.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    products = []
    
    # Find product containers
    product_containers = soup.select('[data-component-type="s-search-result"]')
    
    for container in product_containers[:max_results]:
        try:
            product = {
                'name': 'Product name not found',
                'price': 'Price not available',
                'image_url': 'Image not found',
                'rating': 'Rating not available',
                'url': 'URL not found'
            }
            
            # Extract product name
            name_elem = container.select_one('a h2 span')
            if name_elem:
                product['name'] = name_elem.get_text().strip()
            
            # Extract product URL
            url_elem = container.select_one('a')
            if url_elem:
                product_url = url_elem.get('href')
                if product_url:
                    if product_url.startswith('/'):
                        product_url = base_url + product_url
                    product['url'] = product_url
            
            # Extract price. Same reasoning as extract_product_data: prefer
            # the full formatted price (`.a-offscreen`) over the whole-dollar
            # display fragment, which carries neither cents nor currency.
            price_elem = container.select_one('.a-price .a-offscreen') or container.select_one('.a-price-whole')
            if price_elem:
                product['price'] = clean_price(price_elem.get_text())
            
            # Extract image
            img_elem = container.select_one('img.s-image')
            if img_elem:
                img_url = img_elem.get('src')
                if img_url:
                    product['image_url'] = img_url
            
            # Extract rating
            rating_elem = container.select_one('.a-icon-alt')
            if rating_elem:
                rating_text = rating_elem.get_text()
                rating_match = re.search(r'(\d+\.?\d*)', rating_text)
                if rating_match:
                    product['rating'] = f"{rating_match.group(1)} out of 5"
            
            products.append(product)
            
        except Exception as e:
            print(f"Error extracting product data: {str(e)}")
    
    return products

# Formatting functions

def format_search_results(products: list, query: str) -> str:
    """Format search results for display"""
    if not products:
        return f"No products found for '{query}'"
    
    result = f"# Search Results for '{query}'\n\n"

    # Amazon answers a query it has no match for with loosely related products
    # instead of nothing. The caller reads only this text, so the mismatch has
    # to be stated in it -- otherwise five plausible rows read as five hits
    # (finding #2529).
    coverage = best_query_coverage(query, products)
    if coverage == 0.0:
        result += (
            f"⚠️ ВНИМАНИЕ: ни один результат НЕ содержит слов запроса "
            f"'{query}'. Amazon подставил похожие товары вместо искомого — "
            f"скорее всего, на этой витрине его нет. Проверьте другой домен "
            f"(параметр domain, например 'de') прежде чем считать это ценами "
            f"на запрошенный товар.\n\n"
        )
    elif coverage < 1.0:
        result += (
            f"ℹ️ Точного совпадения нет: лучшее частичное покрытие запроса — "
            f"{coverage:.0%}. Сверьте названия ниже с тем, что искали.\n\n"
        )

    # A full-coverage hit can still be the wrong kind of thing: accessories
    # quote the device's whole name. Only worth saying when the caller did not
    # ask for an accessory in the first place.
    best = best_matching_product(query, products)
    if best is not None and not looks_like_accessory(query):
        if looks_like_accessory(best.get("name") or ""):
            result += (
                "⚠️ Лучшее совпадение похоже на АКСЕССУАР (чехол, плёнка, "
                "ремешок), а не на само устройство — возможно, товара на этой "
                "витрине нет, а совпали слова из названия аксессуара.\n\n"
            )

    for i, product in enumerate(products):
        result += f"## {i+1}. {product['name']}\n"
        result += f"Price: {product['price']}\n"
        result += f"Rating: {product['rating']}\n"
        result += f"URL: {product['url']}\n\n"

    return result

def format_product_details(product: dict) -> str:
    """Format product details for display"""
    result = f"# {product['name']}\n\n"
    result += f"Price: {product['price']}\n"
    result += f"Rating: {product['rating']}\n"
    result += f"Reviews: {product['reviews_count']}\n"
    result += f"Availability: {product['availability']}\n"
    result += f"Description: {product['description']}\n"
    result += f"URL: {product['url']}\n"
    
    return result

# Tools

@mcp.tool()
async def scrape_product(product_url: str) -> str:
    """Scrape product information from an Amazon product URL"""
    try:
        # Validate URL
        parsed_url = urlparse(product_url)
        if 'amazon' not in parsed_url.netloc.lower():
            return "Error: Please provide a valid Amazon product URL"
        
        # Fetch the page
        html_content = await fetch_amazon_page(product_url)
        
        # Extract product data
        product_data = extract_product_data(html_content, product_url)
        
        # Format the result
        return format_product_details(product_data)
        
    except httpx.HTTPStatusError as e:
        return f"HTTP Error: {e.response.status_code} - {e.response.reason_phrase}"
    except httpx.RequestError as e:
        return f"Request Error: {str(e)}"
    except Exception as e:
        return f"Error scraping product: {str(e)}"

@mcp.tool()
async def search_products(query: str, max_results: int = 5, domain: str | None = None) -> str:
    """Search for products on Amazon and return results.

    Args:
        query: what to search for.
        max_results: how many results to return.
        domain: which Amazon storefront to search -- "com" (default), "de",
            "co.uk", "fr" and so on. Storefronts carry DIFFERENT catalogues:
            a European product missing from .com will come back as unrelated
            lookalikes there and as the real thing on .de. Defaults to
            $AMAZON_DOMAIN, then "com".
    """
    try:
        base_url = resolve_domain(domain)
        search_url = build_search_url(query, base_url)

        # Fetch search results page
        html_content = await fetch_amazon_page(search_url)

        # Extract search results
        products = extract_search_results(html_content, max_results, base_url=base_url)

        # Format the results
        return format_search_results(products, query)

    except ValueError as e:
        return f"Error: {str(e)}"
    except AmazonBlockedError as e:
        return f"Error: {str(e)}"
    except Exception as e:
        return f"Error searching products: {str(e)}"


if __name__ == "__main__":
    print("Starting Amazon Products MCP server...")
    mcp.run(transport = "stdio") 