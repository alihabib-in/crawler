import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

try:
    from pypdf import PdfReader  # type: ignore[import-not-found]
except ImportError:
    PdfReader = None

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError  # type: ignore[import-not-found]
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None
    PlaywrightTimeoutError = Exception


@dataclass
class ScraperConfig:
    db_path: Path = Path("credit_cards.db")
    table_name: str = "credit_card_urls"
    url_column: str = "url"
    where_clause: str = ""
    output_path: Path = Path("credit_card_offers.json")
    screenshot_dir: Path = Path("screenshots")
    max_related_links: int = 20
    max_pdfs_per_card: int = 10
    max_compliance_pdfs: int = 8
    request_timeout_seconds: int = 45
    page_timeout_ms: int = 90000
    viewport_width: int = 1440
    viewport_height: int = 2200


OFFER_TERMS = [
    "offer",
    "welcome",
    "bonus",
    "cashback",
    "miles",
    "reward",
    "discount",
    "lounge",
    "points",
]

CONDITION_TERMS = [
    "terms",
    "conditions",
    "eligibility",
    "minimum salary",
    "minimum income",
    "annual fee",
    "apr",
    "interest rate",
]

RELATED_LINK_TERMS = {
    "credit card",
    "card",
    "offer",
    "bonus",
    "reward",
    "miles",
    "cashback",
    "benefit",
    "terms",
    "conditions",
    "fees",
    "charges",
    "kfs",
    "key facts",
    "booklet",
    "guide",
    "learn more",
    "click here",
}

NOISY_TEXT_TERMS = {
    "still got questions",
    "was this useful",
    "customer care",
    "about us",
    "bank with us",
    "you may also be interested",
    "sign in",
    "become a customer",
}

NUMERIC_DETAIL_RE = re.compile(
    r"(?:AED|USD|EUR|GBP)\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*%|\b\d+\s*(?:days?|months?|years?|visits?|tickets?|lounges?)\b|[\d,]+(?:\.\d+)?",
    re.IGNORECASE,
)

PDF_TERMS = {
    "terms",
    "condition",
    "kfs",
    "offer",
    "promotion",
    "reward",
    "mile",
    "benefit",
    "fees",
    "charges",
    "pricing",
    "guide",
    "booklet",
}

PDF_NEGATIVE_TERMS = {
    "priority-banking",
    "private-banking",
    "private/",
    "portfolio",
    "equities",
    "trade",
    "business-banking",
}

LEGAL_DOC_TERMS = {
    "general terms",
    "terms and conditions",
    "schedule of charges",
    "fees and charges",
    "kfs",
    "key facts",
    "consumer banking",
    "regulatory",
    "compliance",
}

FIELD_PATTERNS = {
    "annual_fee": re.compile(r"(?:annual\s+fee|yearly\s+fee)\s*[:\-]?\s*([^\n|]+)", re.IGNORECASE),
    "apr": re.compile(r"(?:apr|interest\s+rate)\s*[:\-]?\s*([^\n|]+)", re.IGNORECASE),
    "minimum_income": re.compile(r"(?:minimum\s+salary|minimum\s+income)\s*[:\-]?\s*([^\n|]+)", re.IGNORECASE),
    "joining_fee": re.compile(r"(?:joining\s+fee|membership\s+fee)\s*[:\-]?\s*([^\n|]+)", re.IGNORECASE),
    "welcome_bonus": re.compile(r"(?:welcome\s+bonus|sign[\s\-]?up\s+bonus)\s*[:\-]?\s*([^\n|]+)", re.IGNORECASE),
}


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/") or parsed.netloc
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", slug)
    return slug[:120] or "card_page"


def load_urls_from_sqlite(config: ScraperConfig) -> list[str]:
    if not config.db_path.exists():
        raise FileNotFoundError(f"Database not found at {config.db_path}")

    query = f"SELECT {config.url_column} FROM {config.table_name}"
    if config.where_clause.strip():
        query = f"{query} WHERE {config.where_clause}"

    with sqlite3.connect(config.db_path) as conn:
        rows = conn.execute(query).fetchall()

    urls = [str(row[0]).strip() for row in rows if row and str(row[0]).strip()]
    if not urls:
        raise ValueError("No URLs found in database table.")
    return urls


def ensure_example_database(config: ScraperConfig) -> None:
    if config.db_path.exists():
        return

    with sqlite3.connect(config.db_path) as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {config.table_name} (id INTEGER PRIMARY KEY AUTOINCREMENT, {config.url_column} TEXT NOT NULL)"
        )
        conn.execute(
            f"INSERT INTO {config.table_name} ({config.url_column}) VALUES (?)",
            ("https://www.mashreq.com/en/uae/neo/cards/credit-cards/solitaire-credit-card/",),
        )
        conn.commit()


def dedupe_dict_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def is_pdf_link(url: str) -> bool:
    parsed = urlparse(url.lower())
    return parsed.path.endswith(".pdf") or ".pdf" in parsed.query


def is_relevant_pdf(url: str, context_text: str = "") -> bool:
    lowered = f"{url} {context_text}".lower()
    return any(term in lowered for term in PDF_TERMS)


def download_pdf_bytes(url: str, timeout_seconds: int) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return ""

    from io import BytesIO

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""


def extract_structured_fields(text: str) -> dict[str, str | None]:
    def normalize_field_value(field: str, value: str | None) -> str | None:
        if not value:
            return None

        cleaned = normalize_whitespace(value)
        amount_pattern = re.compile(
            r"(nil|free|aed\s*[\d,]+|usd\s*[\d,]+|[\$€£]\s*[\d,]+|[\ue000-\uf8ff]?\s*[\d,]{3,})",
            re.IGNORECASE,
        )

        if field in {"annual_fee", "joining_fee", "minimum_income", "welcome_bonus"}:
            found = amount_pattern.findall(cleaned)
            if found:
                return normalize_whitespace("; ".join(found[:2]))

        return cleaned if len(cleaned) <= 180 else None

    extracted: dict[str, str | None] = {}
    for key, pattern in FIELD_PATTERNS.items():
        match = pattern.search(text)
        raw_value = normalize_whitespace(match.group(1)) if match else None
        extracted[key] = normalize_field_value(key, raw_value)
    return extracted


def extract_structured_fields_with_priority(page_text: str, docs_text: str) -> dict[str, str | None]:
    page_fields = extract_structured_fields(page_text)
    doc_fields = extract_structured_fields(docs_text)
    result: dict[str, str | None] = {}
    for key in FIELD_PATTERNS.keys():
        result[key] = page_fields.get(key) or doc_fields.get(key)
    return result


def infer_card_name(page_title: str, h1_text: str, url: str) -> str:
    if page_title and len(page_title) > 3:
        first_part = normalize_whitespace(re.split(r"\||-|/", page_title)[0])
        if "card" in first_part.lower() and len(first_part) <= 80:
            return first_part
    if h1_text and len(h1_text) > 3 and "card" in h1_text.lower():
        return normalize_whitespace(h1_text)
    slug = urlparse(url).path.strip("/").split("/")[-1]
    return slug.replace("-", " ").title() if slug else "Unknown Card"


def line_score(line: str, terms: list[str]) -> int:
    lower = line.lower()
    return sum(1 for term in terms if term in lower)


def extract_promotions(text: str, source: str) -> list[dict[str, str]]:
    lines = [normalize_whitespace(x) for x in text.splitlines()]
    lines = [x for x in lines if len(x) > 20]
    lines = sorted(lines, key=lambda ln: line_score(ln, OFFER_TERMS), reverse=True)

    results = []
    for line in lines:
        if line_score(line, OFFER_TERMS) == 0:
            continue
        results.append(
            {
                "title": line[:120],
                "details": line,
                "source": source,
            }
        )
        if len(results) >= 20:
            break
    return dedupe_dict_list(results)


def is_noisy_text(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in NOISY_TEXT_TERMS)


def make_crisp_text(text: str, max_len: int = 170) -> str:
    clean = normalize_whitespace(text)

    # Collapse duplicated patterns like "Heading: Heading ...".
    if ":" in clean:
        left, right = clean.split(":", 1)
        if right.strip().lower().startswith(left.strip().lower()):
            clean = right.strip()

    if "was this useful" in clean.lower():
        clean = re.split(r"(?i)was this useful", clean)[0].strip()

    if "for t&cs" in clean.lower():
        clean = re.split(r"(?i)for t&cs", clean)[0].strip()

    # Prefer first sentence-like chunk for concise output.
    sentence = re.split(r"[.!?]", clean)[0].strip()
    candidate = sentence if len(sentence) >= 25 else clean
    return candidate[:max_len].strip()


def extract_numeric_facts(text: str) -> list[str]:
    lines = [normalize_whitespace(x) for x in text.splitlines()]
    lines = [x for x in lines if len(x) > 20 and not is_noisy_text(x)]

    numeric_keywords = ["aed", "%", "points", "miles", "days", "months", "visits", "lounges", "per", "vat"]
    results: list[str] = []
    for line in lines:
        lower = line.lower()
        has_digits = bool(re.search(r"\d", line))
        has_num_context = any(k in lower for k in numeric_keywords)
        if has_digits and has_num_context:
            results.append(make_crisp_text(line, max_len=220))

    deduped = list(dict.fromkeys(results))
    return deduped[:20]


def extract_numeric_details(text: str, max_items: int = 14) -> list[str]:
    found = [normalize_whitespace(x) for x in NUMERIC_DETAIL_RE.findall(text or "")]
    deduped = list(dict.fromkeys(found))
    return deduped[:max_items]


def compact_fact(text: str, summary_len: int = 180) -> dict[str, Any]:
    return {
        "summary": make_crisp_text(text, max_len=summary_len),
        "numeric_details": extract_numeric_details(text),
    }


def extract_welcome_bonus_options(text: str) -> list[dict[str, Any]]:
    clean = normalize_whitespace(text)
    chunks = re.findall(
        r"For\s+(new|existing)\s+credit\s+card\s+customers\s*:\s*(.+?)(?=For\s+(?:new|existing)\s+credit\s+card\s+customers\s*:|$)",
        clean,
        flags=re.IGNORECASE,
    )

    options: list[dict[str, Any]] = []
    for audience_raw, details_raw in chunks:
        details = make_crisp_text(details_raw, max_len=180)
        options.append(
            {
                "audience": normalize_whitespace(audience_raw).lower(),
                "summary": details,
                "numeric_details": extract_numeric_details(details_raw),
            }
        )

    # Fallback if explicit new/existing split is not present.
    if not options:
        for line in clean.split(" "):
            _ = line
        if "welcome" in clean.lower() and "bonus" in clean.lower():
            options.append(
                {
                    "audience": "all",
                    "summary": make_crisp_text(clean, max_len=180),
                    "numeric_details": extract_numeric_details(clean),
                }
            )

    return options[:4]


def build_promotion_schema(text: str) -> dict[str, Any]:
    lines = [normalize_whitespace(x) for x in text.splitlines()]
    lines = [x for x in lines if len(x) > 20 and not is_noisy_text(x)]

    schema: dict[str, Any] = {
        "welcome_offer": [],
        "welcome_bonus_options": [],
        "rewards_earn_rate": [],
        "lounge_benefits": [],
        "travel_perks": [],
        "numeric_highlights": [],
    }

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in ["welcome", "sign up", "joining bonus", "bonus"]):
            schema["welcome_offer"].append(compact_fact(line, summary_len=190))
            options = extract_welcome_bonus_options(line)
            for option in options:
                schema["welcome_bonus_options"].append(option)
        if any(k in lower for k in ["earn", "miles", "points", "cashback", "per "]):
            schema["rewards_earn_rate"].append(compact_fact(line, summary_len=190))
        if any(k in lower for k in ["lounge", "airport lounge", "complimentary lounge"]):
            schema["lounge_benefits"].append(compact_fact(line, summary_len=190))
        if any(k in lower for k in ["travel", "airport", "visa", "hotel", "flight", "transfer"]):
            schema["travel_perks"].append(compact_fact(line, summary_len=190))

    schema["numeric_highlights"] = extract_numeric_facts(text)

    for key in ["welcome_offer", "rewards_earn_rate", "lounge_benefits", "travel_perks"]:
        deduped_facts: list[dict[str, Any]] = []
        seen_facts = set()
        for item in schema[key]:
            item_key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if item_key in seen_facts:
                continue
            seen_facts.add(item_key)
            deduped_facts.append(item)
        schema[key] = deduped_facts[:8]

    schema["numeric_highlights"] = list(dict.fromkeys(schema["numeric_highlights"]))[:10]

    deduped_opts: list[dict[str, str]] = []
    seen = set()
    for item in schema["welcome_bonus_options"]:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        deduped_opts.append(item)
    schema["welcome_bonus_options"] = deduped_opts[:4]

    return schema


def extract_conditions(text: str, source: str) -> list[dict[str, Any]]:
    lines = [normalize_whitespace(x) for x in text.splitlines()]
    lines = [x for x in lines if len(x) > 20 and not is_noisy_text(x)]
    lines = sorted(lines, key=lambda ln: line_score(ln, CONDITION_TERMS), reverse=True)

    results = []
    for line in lines:
        if line_score(line, CONDITION_TERMS) == 0:
            continue
        results.append(
            {
                "text": make_crisp_text(line),
                "numeric_details": extract_numeric_details(line),
                "source": source,
            }
        )
        if len(results) >= 25:
            break
    return dedupe_dict_list(results)


def rank_section(section: dict[str, str]) -> int:
    joined = f"{section.get('heading', '')} {section.get('text', '')}".lower()
    return sum(1 for term in OFFER_TERMS + CONDITION_TERMS if term in joined)


def relation_score(text_or_url: str, card_terms: list[str]) -> int:
    lowered = (text_or_url or "").lower()
    score = 0
    score += 2 * sum(1 for term in card_terms if term in lowered)
    score += sum(1 for term in RELATED_LINK_TERMS if term in lowered)
    if "credit-card" in lowered or "credit-cards" in lowered:
        score += 2
    if any(term in lowered for term in PDF_NEGATIVE_TERMS):
        score -= 2
    return score


async def extract_popup_candidate_links(page, base_url: str) -> list[dict[str, str]]:
    popup_script = r"""
    () => {
      const candidates = [];

      const onclickElements = Array.from(document.querySelectorAll('[onclick]'));
      for (const el of onclickElements) {
        const onclick = el.getAttribute('onclick') || '';
        const text = (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
        const m = onclick.match(/window\.open\(['\"]([^'\"]+)['\"]/i);
        if (m && m[1]) {
          candidates.push({ href: m[1], text });
        }
      }

      const attrElements = Array.from(document.querySelectorAll('[data-url], [data-href]'));
      for (const el of attrElements) {
        const href = el.getAttribute('data-url') || el.getAttribute('data-href') || '';
        const text = (el.innerText || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
        if (href) {
          candidates.push({ href, text });
        }
      }

      return candidates;
    }
    """

    raw = await page.evaluate(popup_script)
    links: list[dict[str, str]] = []
    for entry in raw:
        href = (entry.get("href") or "").strip()
        text = normalize_whitespace(entry.get("text") or "")
        if not href:
            continue
        links.append({"url": urljoin(base_url, href), "text": text})
    return links


async def scrape_related_links_from_pages(
    browser_context,
    base_url: str,
    links: list[dict[str, str]],
    card_terms: list[str],
    page_timeout_ms: int,
    max_pages: int = 4,
) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    candidates = []
    seen = set()
    for link in links:
        url = link.get("url", "")
        txt = link.get("text", "")
        if not url or is_pdf_link(url):
            continue

        canonical = canonicalize_link_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)

        score = relation_score(f"{url} {txt}", card_terms)
        has_same_card_term = any(term in f"{url} {txt}".lower() for term in card_terms)
        is_base_page = canonicalize_link_url(base_url) == canonical
        if score >= 2 and (has_same_card_term or is_base_page):
            candidates.append({"url": url, "text": txt, "score": score})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = candidates[:max_pages]

    text_chunks: list[str] = []
    discovered_links: list[dict[str, str]] = []
    visited_pages: list[dict[str, str]] = []

    for candidate in selected:
        page = await browser_context.new_page()
        page.set_default_timeout(page_timeout_ms)
        try:
            await page.goto(candidate["url"], wait_until="domcontentloaded", timeout=page_timeout_ms)
            await page.wait_for_timeout(1000)
            title = normalize_whitespace(await page.title())

            body_text = await page.evaluate(
                r"""
                () => {
                  const main = document.querySelector('main') || document.body;
                  return (main?.innerText || '').replace(/\s+/g, ' ').trim();
                }
                """
            )
            compact = normalize_whitespace(body_text)[:10000]
            if compact and relation_score(f"{title} {compact}", card_terms) >= 2:
                text_chunks.append(f"{title}: {compact}")
                visited_pages.append({"url": candidate["url"], "title": title})

            page_links = await page.evaluate(
                r"""
                () => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
                  href: a.getAttribute('href') || '',
                  text: (a.innerText || '').replace(/\s+/g, ' ').trim(),
                }))
                """
            )
            for entry in page_links:
                href = (entry.get("href") or "").strip()
                txt = normalize_whitespace(entry.get("text") or "")
                if href:
                    discovered_links.append({"url": urljoin(candidate["url"], href), "text": txt})
        except Exception:
            pass
        finally:
            await page.close()

    return "\n".join(text_chunks), discovered_links, visited_pages


async def capture_page_data(page, url: str, screenshot_dir: Path) -> dict[str, Any]:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    base_name = safe_filename_from_url(url)

    try:
        await page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1800)

    full_page_path = screenshot_dir / f"{base_name}_full.png"
    await page.screenshot(path=str(full_page_path), full_page=True)

    page_title = await page.title()
    h1_text = normalize_whitespace(await page.locator("h1").first.inner_text()) if await page.locator("h1").count() else ""

    section_script = r"""
    () => {
      const selectors = ['section', 'article', 'div.card', 'div[class*="offer"]', 'div[class*="benefit"]', 'div[class*="reward"]'];
      const sections = [];
      const seen = new Set();
      const limit = 140;

      for (const selector of selectors) {
        const nodes = Array.from(document.querySelectorAll(selector));
        for (const node of nodes) {
          const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
          if (!text || text.length < 35) continue;
          const headingNode = node.querySelector('h1,h2,h3,h4,strong,b');
          const heading = headingNode ? (headingNode.innerText || '').replace(/\s+/g, ' ').trim() : '';
          const key = `${heading}|${text.slice(0, 180)}`;
          if (seen.has(key)) continue;
          seen.add(key);
          sections.push({ heading, text });
          if (sections.length >= limit) return sections;
        }
      }
      return sections;
    }
    """
    sections = await page.evaluate(section_script)

    link_script = r"""
    () => Array.from(document.querySelectorAll('a[href]')).map((a) => ({
      href: a.getAttribute('href') || '',
      text: (a.innerText || '').replace(/\s+/g, ' ').trim(),
    }))
    """
    raw_links = await page.evaluate(link_script)
    links: list[dict[str, str]] = []
    for entry in raw_links:
        href = (entry.get("href") or "").strip()
        text = normalize_whitespace(entry.get("text") or "")
        if not href:
            continue
        absolute = urljoin(url, href)
        links.append({"url": absolute, "text": text})

    popup_links = await extract_popup_candidate_links(page, url)
    links.extend(popup_links)

    modal_text_script = r"""
    () => {
        const selectors = ['[role="dialog"]', '[aria-modal="true"]', '.modal', '.popup', '.pop-up'];
        const items = [];
        for (const sel of selectors) {
            for (const node of document.querySelectorAll(sel)) {
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const txt = (node.innerText || '').replace(/\s+/g, ' ').trim();
                if (txt.length > 25) items.push(txt);
            }
        }
        return items;
    }
    """
    modal_texts = [normalize_whitespace(x) for x in await page.evaluate(modal_text_script)]

    # Capture screenshots for the highest ranked relevant sections.
    top_sections = sorted(sections, key=rank_section, reverse=True)[:3]
    section_images = []
    for idx, section in enumerate(top_sections, start=1):
        snippet = (section.get("text") or "")[:80]
        escaped = snippet.replace("\\", "\\\\").replace("\"", '\\"')
        locator = page.get_by_text(escaped, exact=False).first
        section_path = screenshot_dir / f"{base_name}_section_{idx}.png"
        try:
            await locator.scroll_into_view_if_needed(timeout=2500)
            await locator.screenshot(path=str(section_path))
            section_images.append(str(section_path))
        except Exception:
            continue

    return {
        "title": normalize_whitespace(page_title),
        "h1": h1_text,
        "sections": sections,
        "links": links,
        "popup_links": popup_links,
        "modal_texts": modal_texts,
        "screenshot_full": str(full_page_path),
        "screenshot_sections": section_images,
    }


def build_card_terms(card_url: str, card_name: str) -> list[str]:
    slug_tokens = re.split(r"[^a-zA-Z0-9]+", urlparse(card_url).path.lower())
    name_tokens = re.split(r"[^a-zA-Z0-9]+", card_name.lower())
    generic = {"credit", "card", "cards", "en", "www", "com", "the", "and", "world"}
    terms = [x for x in slug_tokens + name_tokens if len(x) >= 4 and x not in generic]
    return list(dict.fromkeys(terms))


def canonicalize_link_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, ""))


def is_card_specific_pdf(url: str, link_text: str, card_terms: list[str]) -> bool:
    lower_text = (link_text or "").lower()
    filename = Path(urlparse(url).path).name.lower()
    has_text_match = any(term in lower_text for term in card_terms)
    has_filename_match = any(term in filename for term in card_terms)
    return has_text_match and has_filename_match


def is_compliance_pdf(url: str, link_text: str) -> bool:
    lowered = f"{url} {link_text}".lower()
    return any(term in lowered for term in LEGAL_DOC_TERMS)


def is_credit_card_related(url: str, link_text: str, card_terms: list[str], card_name: str) -> bool:
    lowered = f"{url} {link_text}".lower()
    return (
        "credit card" in lowered
        or any(term in lowered for term in card_terms)
        or any(term in lowered for term in card_name.lower().split())
    )


def pdf_link_score(url: str, text: str, card_terms: list[str]) -> int:
    lowered = f"{url} {text}".lower()
    score = 0
    score += sum(1 for term in PDF_TERMS if term in lowered)
    score += 3 * sum(1 for term in card_terms if term in lowered)
    if "credit-card" in lowered or "credit_cards" in lowered:
        score += 2
    if any(term in lowered for term in PDF_NEGATIVE_TERMS):
        score -= 3
    return score


def extract_related_pdf_documents(
    links: list[dict[str, str]],
    config: ScraperConfig,
    card_url: str,
    card_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    docs = []
    compliance_docs = []
    combined = []
    card_candidates = []
    compliance_candidates = []
    card_terms = build_card_terms(card_url, card_name)
    seen_urls = set()

    for link in links:
        url = link.get("url", "")
        txt = link.get("text", "")
        if not is_pdf_link(url):
            continue
        canonical_url = canonicalize_link_url(url)
        if canonical_url in seen_urls:
            continue
        seen_urls.add(canonical_url)

        score = pdf_link_score(url, txt, card_terms)
        if not is_relevant_pdf(url, txt):
            continue

        candidate = {"url": url, "text": txt, "score": score}
        if (is_card_specific_pdf(url, txt, card_terms) or is_credit_card_related(url, txt, card_terms, card_name)) and score > 1:
            card_candidates.append(candidate)
        elif is_compliance_pdf(url, txt):
            compliance_candidates.append(candidate)

    card_candidates.sort(key=lambda item: item.get("score", 0), reverse=True)
    compliance_candidates.sort(key=lambda item: item.get("score", 0), reverse=True)

    def process_pdf_links(selected_links: list[dict[str, Any]], target_docs: list[dict[str, Any]], include_text: bool) -> None:
        for link in selected_links:
            pdf_url = link["url"]
            text_hint = link.get("text", "")
            try:
                pdf_bytes = download_pdf_bytes(pdf_url, config.request_timeout_seconds)
                pdf_text = extract_pdf_text(pdf_bytes)
                cleaned = normalize_whitespace(pdf_text)
                target_docs.append(
                    {
                        "url": pdf_url,
                        "title_hint": text_hint,
                        "type": "pdf",
                        "extract_status": "ok" if cleaned else "empty_or_unparsed",
                        "excerpt": cleaned[:3000],
                    }
                )
                if include_text and cleaned:
                    combined.append(cleaned)
            except Exception as exc:
                target_docs.append(
                    {
                        "url": pdf_url,
                        "title_hint": text_hint,
                        "type": "pdf",
                        "extract_status": "failed",
                        "error": str(exc),
                        "excerpt": "",
                    }
                )

    process_pdf_links(card_candidates[: config.max_pdfs_per_card], docs, include_text=True)
    process_pdf_links(compliance_candidates[: config.max_compliance_pdfs], compliance_docs, include_text=False)

    return docs, compliance_docs, "\n\n".join(combined)


def collect_offer_focused_text(page_data: dict[str, Any]) -> str:
    sections = page_data.get("sections", [])
    ranked = sorted(sections, key=rank_section, reverse=True)
    focused_sections = [sec for sec in ranked if rank_section(sec) > 0][:30]

    lines = []
    for sec in focused_sections:
        heading = normalize_whitespace(sec.get("heading", ""))
        text = normalize_whitespace(sec.get("text", ""))
        if is_noisy_text(f"{heading} {text}"):
            continue
        if len(text) > 900:
            continue
        if heading:
            lines.append(f"{heading}: {text}")
        else:
            lines.append(text)

    for modal_text in page_data.get("modal_texts", []):
        if not is_noisy_text(modal_text) and any(term in modal_text.lower() for term in OFFER_TERMS + CONDITION_TERMS):
            lines.append(modal_text)

    return "\n".join(lines)


async def scrape_single_card(page, url: str, config: ScraperConfig) -> dict[str, Any]:
    logging.info("Scraping card page with Playwright: %s", url)

    try:
        page_data = await capture_page_data(page, url, config.screenshot_dir)
    except PlaywrightTimeoutError:
        return {
            "source_url": url,
            "scraped_at_utc": now_utc_iso(),
            "error": "page_timeout",
        }

    card_name = infer_card_name(page_data.get("title", ""), page_data.get("h1", ""), url)
    card_terms = build_card_terms(url, card_name)

    related_pages_text, related_page_links, related_page_visits = await scrape_related_links_from_pages(
        page.context,
        base_url=url,
        links=page_data.get("links", []),
        card_terms=card_terms,
        page_timeout_ms=config.page_timeout_ms,
        max_pages=4,
    )

    focused_text = collect_offer_focused_text(page_data)
    merged_focus_text = f"{focused_text}\n{related_pages_text}".strip()

    all_links = page_data.get("links", []) + related_page_links
    docs, compliance_docs, docs_text = extract_related_pdf_documents(
        all_links,
        config,
        card_url=url,
        card_name=card_name,
    )
    merged_text = f"{merged_focus_text}\n\n{docs_text}".strip()

    fields = extract_structured_fields_with_priority(merged_focus_text, docs_text)
    promotions_schema = build_promotion_schema(merged_focus_text)
    conditions = extract_conditions(merged_focus_text, source="page_focus") + extract_conditions(docs_text, source="pdf")

    item = {
        "source_url": url,
        "scraped_at_utc": now_utc_iso(),
        "card_name": card_name,
        "issuer_domain": urlparse(url).netloc,
        "screenshots": {
            "full_page": page_data.get("screenshot_full", ""),
            "section_focus": page_data.get("screenshot_sections", []),
        },
        "fields": fields,
        "promotions": promotions_schema,
        "conditions": dedupe_dict_list(conditions),
        "documents": docs,
        "compliance_documents": compliance_docs,
        "related_pages_scraped": related_page_visits,
        "raw": {
            "page_title": page_data.get("title", ""),
            "page_h1": page_data.get("h1", ""),
            "offer_focused_text": merged_focus_text[:20000],
            "related_pdf_text": docs_text[:30000],
        },
    }
    return item


async def run_scraper(config: ScraperConfig) -> list[dict[str, Any]]:
    if async_playwright is None:
        raise ImportError(
            "Playwright is required. Install dependencies and run: python -m playwright install chromium"
        )

    urls = load_urls_from_sqlite(config)
    output: list[dict[str, Any]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": config.viewport_width, "height": config.viewport_height}
        )
        page = await context.new_page()
        page.set_default_timeout(config.page_timeout_ms)

        for url in urls:
            try:
                result = await scrape_single_card(page, url, config)
                output.append(result)
            except Exception as exc:
                output.append(
                    {
                        "source_url": url,
                        "scraped_at_utc": now_utc_iso(),
                        "error": str(exc),
                    }
                )

        await context.close()
        await browser.close()

    config.output_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return output


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    base_dir = Path(__file__).resolve().parent

    config = ScraperConfig(
        db_path=base_dir / "credit_cards.db",
        table_name="credit_card_urls",
        url_column="url",
        where_clause="",
        output_path=base_dir / "credit_card_offers.json",
        screenshot_dir=base_dir / "screenshots",
    )

    ensure_example_database(config)
    results = await run_scraper(config)
    logging.info("Finished. Scraped %s card URLs. Output written to %s", len(results), config.output_path)


if __name__ == "__main__":
    asyncio.run(main())