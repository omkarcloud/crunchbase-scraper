"""The network helpers the crunchbase module and its browser need. Requests go out
from your own connection, or through the proxy config.py names; there is no
exit policy here, so every session is a plain curl_cffi session. The language
and timezone tables let a browser behind a proxy match the place the proxy
exits in (the place comes from config.exit_geo, never from the proxy URL)."""
from dataclasses import dataclass
from typing import Optional

SITE_LANGUAGE = "en-US,en;q=0.9"


def site_accept_language(site=None):
    return SITE_LANGUAGE


def accept_language(proxy=None, country=None):
    return SITE_LANGUAGE


def gateway_country(exit=None):
    return None


def exit_country(proxy=None):
    return None


def exit_proxy(exit=None):
    return exit if exit and "://" in str(exit) else None


def new_session(exit=None, impersonate="chrome", **kw):
    from curl_cffi import requests as curl
    sess = curl.Session(impersonate=impersonate, **kw)
    proxy = exit_proxy(exit)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    return sess


def exit_session(exit=None, impersonate="chrome", **kw):
    return new_session(exit, impersonate, **kw)


# ---- language and clock of the place a proxy exits in ----------------------------------------

ACCEPT_LANGUAGE = {
    "us": "en-US,en;q=0.9",
    "gb": "en-GB,en;q=0.9",
    "au": "en-AU,en;q=0.9",
    "ca": "en-CA,en;q=0.9,fr-CA;q=0.8",
    "nz": "en-NZ,en;q=0.9",
    "ie": "en-IE,en;q=0.9",
    "za": "en-ZA,en;q=0.9",
    "in": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "sg": "en-SG,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
    "ph": "en-PH,en;q=0.9,fil;q=0.8",
    "my": "ms-MY,ms;q=0.9,en-MY;q=0.8,en;q=0.7",
    "de": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "at": "de-AT,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "ch": "de-CH,de;q=0.9,en-US;q=0.8,en;q=0.7",
    "fr": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "be": "nl-BE,nl;q=0.9,fr;q=0.8,en-US;q=0.7,en;q=0.6",
    "nl": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "es": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "it": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "pt": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "pl": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "ru": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "cz": "cs-CZ,cs;q=0.9,en-US;q=0.8,en;q=0.7",
    "sk": "sk-SK,sk;q=0.9,cs;q=0.8,en-US;q=0.7,en;q=0.6",
    "se": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
    "br": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "mx": "es-MX,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "th": "th-TH,th;q=0.9,en-US;q=0.8,en;q=0.7",
    "vn": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "id": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "tw": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "jp": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "kr": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


TIMEZONE = {
    "gb": "Europe/London", "ie": "Europe/Dublin", "fr": "Europe/Paris", "de": "Europe/Berlin",
    "at": "Europe/Vienna", "ch": "Europe/Zurich", "nl": "Europe/Amsterdam", "be": "Europe/Brussels",
    "lu": "Europe/Luxembourg", "es": "Europe/Madrid", "pt": "Europe/Lisbon", "it": "Europe/Rome",
    "pl": "Europe/Warsaw", "cz": "Europe/Prague", "sk": "Europe/Bratislava", "hu": "Europe/Budapest",
    "ro": "Europe/Bucharest", "bg": "Europe/Sofia", "gr": "Europe/Athens", "se": "Europe/Stockholm",
    "no": "Europe/Oslo", "dk": "Europe/Copenhagen", "fi": "Europe/Helsinki", "ee": "Europe/Tallinn",
    "lv": "Europe/Riga", "lt": "Europe/Vilnius", "hr": "Europe/Zagreb", "si": "Europe/Ljubljana",
    "rs": "Europe/Belgrade", "tr": "Europe/Istanbul", "il": "Asia/Jerusalem",
    "ae": "Asia/Dubai", "sa": "Asia/Riyadh", "qa": "Asia/Qatar", "kw": "Asia/Kuwait", "bh": "Asia/Bahrain",
    "om": "Asia/Muscat", "lb": "Asia/Beirut", "jo": "Asia/Amman", "eg": "Africa/Cairo",
    "ma": "Africa/Casablanca", "za": "Africa/Johannesburg", "ng": "Africa/Lagos", "ke": "Africa/Nairobi",
    "in": "Asia/Kolkata", "pk": "Asia/Karachi", "bd": "Asia/Dhaka", "lk": "Asia/Colombo",
    "sg": "Asia/Singapore", "my": "Asia/Kuala_Lumpur", "th": "Asia/Bangkok", "vn": "Asia/Ho_Chi_Minh",
    "ph": "Asia/Manila", "hk": "Asia/Hong_Kong", "tw": "Asia/Taipei", "cn": "Asia/Shanghai",
    "jp": "Asia/Tokyo", "kr": "Asia/Seoul", "nz": "Pacific/Auckland",
    "ar": "America/Argentina/Buenos_Aires", "cl": "America/Santiago", "co": "America/Bogota",
    "pe": "America/Lima", "uy": "America/Montevideo", "ve": "America/Caracas",
}


def _strip_q(accept: str) -> str:
    return ",".join(t.split(";")[0].strip() for t in accept.split(",") if t.strip())


def accept_header(languages) -> Optional[str]:
    """The Accept-Language Chrome builds from navigator.languages: the first
    tag bare, then q=0.9, 0.8, ... down to 0.1 ("en-US,en;q=0.9").
    `languages` is a list or a comma string."""
    tags = [t.strip() for t in (languages.split(",") if isinstance(languages, str) else languages or []) if t and t.strip()]
    if not tags:
        return None
    return ",".join(t if i == 0 else f"{t};q={max(1, 10 - i) / 10:.1f}" for i, t in enumerate(tags))


def languages_for(locale: Optional[str], accept: Optional[str] = None) -> Optional[str]:
    """navigator.languages / Chrome's --accept-lang for `locale`: the full
    q-stripped list ("pl-PL,pl,en-US,en", what Chrome reports for a machine
    set to pl-PL), never a one-tag list. From `accept` when given; else the
    table row the locale heads; a bare language ("de") takes the row of the
    first country browsing in it; a regioned tag heading no row ("zh-Hant-TW")
    keeps itself in front of its language and English. ONE comma string:
    browserforge's tuple form leaves a leading space on every extra entry
    (["pl-PL", " en"], seen 2026-10-28)."""
    if accept:
        return _strip_q(accept)
    if not locale:
        return None
    parts = locale.split("-")
    lang = parts[0].lower()
    if len(parts) == 1:
        for row in ACCEPT_LANGUAGE.values():
            if row.split(",")[0].split("-")[0].lower() == lang:
                return _strip_q(row)
        return f"{lang},en-US,en" if lang != "en" else "en-US,en"
    row = ACCEPT_LANGUAGE.get(parts[-1].lower(), "")
    if row and row.split(",")[0].lower() == locale.lower():
        return _strip_q(row)
    tags = [locale, lang, "en-US", "en"]
    return ",".join(dict.fromkeys(t for t in tags if t))


@dataclass(frozen=True)
class ExitProfile:
    """What a browser or session leaving through one exit should announce.
    `country` None = nothing is known about the exit (direct egress, or a
    proxy URL without a country tag): callers keep their own defaults."""
    country: Optional[str]
    accept_language: Optional[str]
    languages: Optional[str]     # q-stripped, "fr-FR,fr,en-US,en"
    locale: Optional[str]        # "fr-FR"
    timezone: Optional[str]      # IANA zone, or None when only a lookup can tell
    source: Optional[str]        # "geo" (exit IP looked up) | "url" (proxy tag) | None


def exit_profile(proxy: Optional[str] = None, *, geo: Optional[dict] = None,
                 country: Optional[str] = None) -> ExitProfile:
    """The language and clock of the exit behind `proxy`. A successful
    `geo` lookup of the exit (config.exit_geo: countryCode + timezone) is the
    truth and wins over the URL's country tag; an explicit `country` is
    next; the tag last. The zone is the lookup's, else TIMEZONE's for a
    single-zone country, else None."""
    cc, source, zone = None, None, None
    if geo and geo.get("countryCode"):
        cc, source, zone = geo["countryCode"].lower(), "geo", geo.get("timezone")
    elif country:
        cc, source = country.lower(), "url"
    else:
        cc = exit_country(proxy)
        source = "url" if cc else None
    if not cc:
        return ExitProfile(None, None, None, None, None, None)
    accept = ACCEPT_LANGUAGE.get(cc) or f"en-{cc.upper()},en;q=0.9"
    return ExitProfile(country=cc, accept_language=accept, languages=_strip_q(accept),
                       locale=accept.split(",")[0], timezone=zone or TIMEZONE.get(cc),
                       source=source)
