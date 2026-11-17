"""Crunchbase browser mode (crunchbase/fetch.py): an identity whose minted
cookie does not replay over curl (exits that issue no cf_clearance) is
served by in-page fetches from kept browsers on that exit, parked on
robots.txt between calls. GKE keys each browser by the identity (session =
"<identity key>#<slot>", INPAGE_BROWSER_SLOTS x BROWSER_BURST_FACTOR slots
per identity since 2026-11-04) so a retired identity never reuses a
browser whose cookie has spent its organization quota. A refused answer tells GKE which
Cloudflare verdict it was: "rate-limited" (error 1015, cool the egress)
or a challenge (retire the identity), so the pod's rotation stays as it
was with the in-pod tab."""
from recipes._cloudflare import CloudflareInPage
from recipes.crunchbase import SEED_PATH, SITE


def is_rate_limited(out) -> bool:
    text = (out.get("body") or "")[:600].lower()
    return out.get("status") == 429 and ("1015" in text or "rate limited" in text or "rate-limited" in text)


class CrunchbaseFetch(CloudflareInPage):
    name = "crunchbase-fetch"
    variants = {"": SITE + SEED_PATH}
    home = SITE + SEED_PATH
    park_url = SITE + "/robots.txt"
    max_browsers = 6                  # the share: CRUNCHBASE_IDENTITIES (3) x INPAGE_BROWSER_SLOTS (2); the pod
                                      # offers share x BROWSER_BURST_FACTOR sessions per identity

    def refusal(self, out):
        verdict = "rate-limited (error 1015)" if is_rate_limited(out) else "challenged"
        return f"{self.name}: refused in the browser (HTTP {out['status']}, {verdict}); session dropped"


RECIPE = CrunchbaseFetch
