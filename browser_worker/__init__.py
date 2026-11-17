"""The browser worker: the one service that runs on the Contabo boxes.

A dumb browser helper. It holds no parsing, no database and no proxy
credentials. GKE calls one of three actions per site:

    POST /<site>/mint   {proxy?, variant}                   -> cookies, user_agent, client_hints, tokens, exit_ip, expires_at, chrome_major
    POST /<site>/token  {proxy?, variant, input}            -> token, expires_at
    POST /<site>/fetch  {proxy?, variant, session?, url | urls, method, headers, body} -> raw {status, headers, body, final_url} (one or many)
    GET  /health, GET /status

`proxy` absent means the box's own IP. Every site's "how to get past it" is
a recipe under scrapers/recipes/<site>.py, built on one of four types
(browser_worker.types). The worker starts with zero browsers; a mint browser
closes in the same call; token and fetch browsers park between requests and
close after an idle timeout. See scrapers/browser_worker/README.md.
"""
