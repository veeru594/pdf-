"""
checker.py — DNS resolution + company online presence verification.

Online presence uses a layered approach (highest → lowest confidence):
  1. Direct website fetch  — if the email domain exists, try https://{domain}.
     A live HTTPS site with real content is the strongest possible signal.
  2. Wikipedia REST API    — clean JSON, no bot-blocking. High-quality positive.
  3. DuckDuckGo HTML       — friendlier to bots than Google.
  4. Bing search fallback  — secondary search engine.
  5. DNS domain guessing   — last resort for companies whose email domain is
                             generic (gmail.com, etc.) and we couldn't pin down
                             their official site.
"""
import re
import requests
import dns.resolver
from typing import Optional
from app.models import DnsResult, CompanyOnlineResult


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
}

# Domains we treat as generic — finding the company website here proves nothing.
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
    "live.com", "rediffmail.com", "protonmail.com", "icloud.com", "aol.com",
}


# ── DNS ──────────────────────────────────────────────────────────

def check_domain(domain: Optional[str]) -> DnsResult:
    """
    Check whether the company domain extracted from the letter is real.
    Two independent checks:
      dns_valid       = A record resolves (domain EXISTS)
      mx_records_exist = MX record found (domain has its OWN mail server)
    Many legitimate companies use Gmail/Office365 — no MX on own domain is normal.
    dns_valid is the primary fraud signal; mx_records_exist is informational.
    """
    if not domain or domain.strip() == "":
        return DnsResult(
            domain=None,
            dns_valid=False,
            mx_records_exist=False,
            note="No domain provided"
        )

    # Clean domain: remove www., http://, etc.
    domain = domain.strip().lower()
    domain = re.sub(r'^https?://', '', domain)
    domain = re.sub(r'^www\.', '', domain)
    domain = domain.rstrip('/')
    
    # Validate domain format
    if not re.match(r'^[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}$', domain):
        return DnsResult(
            domain=domain,
            dns_valid=False,
            mx_records_exist=False,
            note=f"Invalid domain format: {domain}"
        )

    notes = []
    a_valid = False
    mx_valid = False
    a_lookup_error = False

    # ── CHECK 1: A record — does the domain exist at all? ───────────
    try:
        dns.resolver.resolve(domain, 'A')
        a_valid = True
        notes.append("domain resolves (A record OK)")
    except dns.resolver.NXDOMAIN:
        notes.append("domain does not exist (NXDOMAIN)")
    except dns.resolver.NoAnswer:
        a_valid = True
        notes.append("domain exists (no A record, but not NXDOMAIN)")
    except Exception as e:
        # Timeout / resolver failure / no nameservers — this is a SYSTEM error,
        # NOT evidence the domain is fake. Flag it so scoring won't treat the
        # unresolved result as a genuine "domain does not exist" negative.
        a_lookup_error = True
        notes.append(f"A record lookup failed (system error): {str(e)[:80]}")

    # ── CHECK 2: MX record — does domain host its own email? ────────
    try:
        records = list(dns.resolver.resolve(domain, 'MX'))
        mx_count = len(records)
        mx_valid = mx_count > 0
        if mx_valid:
            notes.append(f"{mx_count} MX record(s) found")
        else:
            notes.append("no MX records")
    except dns.resolver.NXDOMAIN:
        notes.append("domain does not exist (MX NXDOMAIN)")
    except dns.resolver.NoAnswer:
        notes.append("no MX records — company likely uses Gmail/Office365")
    except Exception:
        notes.append("MX lookup failed (may use Gmail/Office365)")

    return DnsResult(
        domain=domain,
        dns_valid=a_valid,
        mx_records_exist=mx_valid,
        note=" | ".join(notes),
        lookup_error=a_lookup_error,
    )


# ── ONLINE PRESENCE ──────────────────────────────────────────────

def check_company_online(
    company_name: Optional[str],
    domain: Optional[str] = None,
) -> CompanyOnlineResult:
    """
    Layered online presence check.  Stops at the first method that confirms.
    Passing the email domain (when available) lets us go straight to the
    company's actual website — by far the strongest signal.
    """
    # Need at least one of name/domain to verify anything. A valid domain alone
    # is enough for Method 1 (direct website fetch) — don't short-circuit when
    # field extraction missed the name but we still have a letterhead domain.
    if not company_name and not domain:
        return CompanyOnlineResult(
            found=False, score=0, note="No company name or domain to check"
        )

    company_name = company_name.strip() if company_name else None
    attempted = []
    errors = []          # real methods that hit a network/system error
    verify_attempts = 0  # count of REAL verification methods that ran (excludes DNS-guess)

    # ── METHOD 1: Direct website fetch (strongest signal) ──────────
    # Needs only the domain — runs even when company_name is missing.
    if domain and domain.strip().lower() not in GENERIC_EMAIL_DOMAINS:
        verify_attempts += 1
        result = _try_direct_website(domain.strip(), errors)
        if result:
            return result
        attempted.append(f"direct fetch of {domain}")

    # ── METHODS 2-5: name-based lookups (skipped when name is missing) ──
    if company_name:
        # ── METHOD 2: Wikipedia REST API ──────────────────────────
        verify_attempts += 1
        result = _try_wikipedia(company_name, errors)
        if result:
            return result
        attempted.append("Wikipedia")

        # ── METHOD 3: DuckDuckGo HTML ─────────────────────────────
        verify_attempts += 1
        result = _try_duckduckgo(company_name, errors)
        if result:
            return result
        attempted.append("DuckDuckGo")

        # ── METHOD 4 (Bing) REMOVED ──────────────────────────────
        # Bing static scraping is unreliable: it returns a JS/consent page
        # (~76 KB, 0 organic result blocks) identical for real and fake names,
        # so _try_bing confirmed ANY company via the echoed query — a false
        # "online presence" that over-credited fakes and slipped past Gate 2.
        # Direct-website + Wikipedia + DuckDuckGo remain as reliable signals.

        # ── METHOD 5: DNS domain guessing (last resort, heuristic) ──
        # A guess miss tells us nothing about the real company, so it does NOT
        # count toward verify_attempts / the "did we genuinely check" decision.
        result = _try_dns_guess(company_name)
        if result:
            return result
        attempted.append("DNS-guess")

    # ── Decide: did we genuinely check, or were we blocked by errors? ──
    # If every attempt raised a network/system error (zero attempts ran cleanly
    # to a negative), we could NOT actually verify presence. Mark checked=False
    # so scoring treats this as "unverified", NOT as a real "not found" signal.
    clean_runs = verify_attempts - len(errors)
    checked = clean_runs >= 1
    label = company_name or domain or "unknown"
    if not checked and attempted:
        note = (
            f"Online check could NOT complete for '{label}' — "
            f"all {len(attempted)} attempt(s) hit network/system errors: "
            f"{'; '.join(errors[:3])}"
        )
    else:
        note = f"No online presence for '{label}' — tried: {', '.join(attempted) or 'nothing'}"
    return CompanyOnlineResult(found=False, score=0, checked=checked, note=note)


# ── Method implementations ───────────────────────────────────────

def _try_direct_website(domain: str, errors: list) -> Optional[CompanyOnlineResult]:
    """Try https://{domain} and https://www.{domain}.  Live page = found."""
    bare = domain[4:] if domain.lower().startswith("www.") else domain
    candidates = [f"https://{bare}", f"https://www.{bare}", f"http://{bare}"]
    reached = False   # did any candidate URL actually return an HTTP response?
    for url in candidates:
        try:
            r = requests.get(
                url, headers=HEADERS, timeout=8, allow_redirects=True, verify=True
            )
            reached = True
            # 200 + at least 1.5 KB of content = a real site, not a parking page
            if r.status_code == 200 and len(r.text) > 1500:
                return CompanyOnlineResult(
                    found=True,
                    score=7,
                    checked=True,
                    note=f"Live website at {url} (HTTP 200, {len(r.text)//1024} KB)"
                )
        except requests.exceptions.SSLError:
            reached = True   # server answered the TLS handshake — it exists
            continue          # try next URL (http://)
        except Exception:
            continue
    # If no candidate URL was reachable at all, this was a network failure —
    # not evidence the company has no site. Record it as a system error.
    if not reached:
        errors.append(f"{domain} unreachable")
    return None


def _try_wikipedia(company_name: str, errors: list) -> Optional[CompanyOnlineResult]:
    """Wikipedia REST API — fast, no bot-blocking, structured response."""
    try:
        # First try direct page lookup
        slug = re.sub(r"\s+", "_", company_name.strip())
        r = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}",
            headers={**HEADERS, "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("type") == "standard":
                return CompanyOnlineResult(
                    found=True,
                    score=7,
                    note=f"Wikipedia entry: {data.get('title')}"
                )
        # Fallback: search API
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query", "list": "search", "format": "json",
                "srsearch": company_name, "srlimit": 3,
            },
            headers=HEADERS, timeout=6,
        )
        if r.status_code == 200:
            hits = r.json().get("query", {}).get("search", [])
            for hit in hits:
                title = (hit.get("title") or "").lower()
                if company_name.lower() in title or title in company_name.lower():
                    return CompanyOnlineResult(
                        found=True,
                        score=7,
                        note=f"Wikipedia search match: {hit.get('title')}"
                    )
    except Exception:
        errors.append("Wikipedia request error")
    return None


def _try_duckduckgo(company_name: str, errors: list) -> Optional[CompanyOnlineResult]:
    """DuckDuckGo HTML — bot-friendlier than Google.  Count organic results."""
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": f'"{company_name}"'},
            headers=HEADERS, timeout=8,
        )
        if r.status_code == 200 and len(r.text) > 3000:
            text_lc = r.text.lower()
            link_count = text_lc.count('class="result__a"') + text_lc.count('result__url')
            name_in_results = company_name.lower() in text_lc
            if name_in_results and link_count >= 2:
                return CompanyOnlineResult(
                    found=True,
                    score=7,
                    note=f"DuckDuckGo: {link_count} organic result(s)"
                )
    except Exception:
        errors.append("DuckDuckGo request error")
    return None


# NOTE: _try_bing was removed — Bing static scraping returns a JS/consent page
# with no organic results in the HTML, so it confirmed any name (real or fake)
# via the echoed query. Direct-website + Wikipedia + DuckDuckGo cover presence.


def _try_dns_guess(company_name: str) -> Optional[CompanyOnlineResult]:
    """Last-resort: guess likely domains and see if any resolve."""
    clean = company_name.lower()
    clean = re.sub(
        r'\b(pvt|ltd|private|limited|solutions|software|technologies|tech|'
        r'services|india|llp|inc|global|foundation|trust|corp|company|co)\b',
        '', clean,
    )
    clean = re.sub(r'[^a-z0-9]', '', clean).strip()
    if not clean:
        return None

    candidates = [
        f"{clean}.com", f"{clean}.in", f"{clean}.co.in",
        f"{clean}.org", f"{clean}.net",
    ]
    for d in candidates:
        try:
            dns.resolver.resolve(d, 'A')
            return CompanyOnlineResult(
                found=True,
                score=4,
                note=f"DNS-guess hit: {d} resolves"
            )
        except Exception:
            continue
    return None
