"""
Unified scraper service: one web app, one shared Chromium.

  GET       /scrape?url=<modpro page>          episode + download links        (no browser)
  GET       /getlink?url=<shortener link>      resolve shortener -> file links (browser)
  GET|POST  /api/link  (url)                   driveseed file -> google drive  (browser)
  POST      /admin/session                     upload a fresh driveseed login session
  GET       /health   /ping   /                status, liveness, small test page

Auth: set API_KEY and send it as header  X-API-Key.
  /getlink, /api/link, /admin/session are protected when API_KEY is set
  (/api/link and /admin/session REQUIRE it, as before). /scrape is open unless
  SCRAPE_REQUIRES_KEY=1.
"""
import asyncio
import hmac
import logging
import time

from quart import Quart, Response, jsonify, request
from quart_cors import cors

import browser
import config
from common import validate_url
from services import driveseed, linkpilot, modpro

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

app = Quart(__name__)
app = cors(app, allow_origin=config.CORS_ORIGIN)

driveseed.load_session()


# ----------------------------------------------------------------- auth
def _key_error(required, allow_query=False):
    """Return a (response, status) tuple if the request must be rejected, else None."""
    if not config.API_KEY:
        if required:
            return jsonify(error="API_KEY is not configured on the server"), 500
        return None
    supplied = request.headers.get("X-API-Key") or (request.args.get("key", "") if allow_query else "")
    if not hmac.compare_digest(supplied.encode(), config.API_KEY.encode()):
        return jsonify(error="Unauthorized"), 401
    return None


# ----------------------------------------------------------------- lifecycle
@app.before_serving
async def startup():
    browser.init()
    try:
        await browser.ensure_browser()
    except Exception as e:  # /scrape still works; browser retries on the next request
        log.warning("browser warm-up failed (will retry on demand): %s", e)
    await linkpilot.startup_check()


@app.after_serving
async def shutdown():
    await browser.shutdown()


# ----------------------------------------------------------------- misc routes
@app.route("/ping")
async def ping():
    return "pong", 200


@app.route("/health")
async def health():
    return jsonify(status="ok", browser=browser.is_up(), driveseed_session_loaded=driveseed.session_loaded())


# ----------------------------------------------------------------- service 1: /scrape
@app.route("/scrape", methods=["GET"])
async def scrape_api():
    err = _key_error(required=False) if config.SCRAPE_REQUIRES_KEY else None
    if err:
        return err
    url = (request.args.get("url") or "").strip()
    problem = validate_url(url, config.SCRAPE_ALLOWED_HOSTS)
    if problem:
        return jsonify(error=problem), 400
    return jsonify(await modpro.scrape(url))


# ----------------------------------------------------------------- service 2: /getlink
@app.route("/getlink")
async def get_link():
    err = _key_error(required=False)
    if err:
        return err

    start_url = (request.args.get("url") or "").strip()
    problem = validate_url(start_url, config.GETLINK_ALLOWED_HOSTS)
    if problem:
        return jsonify(error=problem), 400

    cached = linkpilot.cache_get(start_url)
    if cached:
        log.info("getlink cache hit")
        return jsonify({**cached, "cached": True}), 200

    t0 = time.time()
    try:
        body, status = await asyncio.wait_for(linkpilot.resolve(start_url), timeout=config.REQUEST_TIMEOUT)
        log.info("getlink resolved in %.1fs (status %s)", time.time() - t0, status)
        if status == 200:
            linkpilot.cache_put(start_url, body)
        return jsonify(body), status
    except asyncio.TimeoutError:
        return jsonify(error="Timed out"), 504
    except Exception as e:
        log.exception("getlink failed")
        return jsonify(error="Internal error", detail=str(e).splitlines()[0]), 500


# ----------------------------------------------------------------- service 3: /api/link
@app.route("/api/link", methods=["GET", "POST"])
async def api_link():
    err = _key_error(required=True, allow_query=True)
    if err:
        return err

    url = request.args.get("url") or ((await request.get_json(silent=True)) or {}).get("url")
    if not driveseed.url_allowed(url):
        return jsonify(error=f"url must be https and on one of: {', '.join(sorted(config.DRIVESEED_ALLOWED_HOSTS))}"), 400

    try:
        return jsonify(await driveseed.get_link(url))
    except driveseed.SessionInvalid as e:
        return jsonify(error="session_invalid", detail=str(e),
                       hint="log in on a PC, then POST the new state.json to /admin/session"), 503
    except driveseed.Busy as e:
        return jsonify(error="busy", detail=str(e)), 503
    except Exception as e:
        return jsonify(error="failed", detail=str(e)[:300]), 502


@app.route("/admin/session", methods=["POST"])
async def admin_session():
    err = _key_error(required=True, allow_query=True)
    if err:
        return err
    body = (await request.get_json(silent=True)) or {}
    try:
        driveseed.write_session(body.get("state"), body.get("user_agent"))
    except Exception as e:
        return jsonify(error=str(e)), 400
    return jsonify(status="session saved")


# ----------------------------------------------------------------- test page
PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Scraper service</title>
<style>body{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px}
input,select,button{width:100%;padding:10px;margin:6px 0;font-size:16px;box-sizing:border-box}
pre{white-space:pre-wrap;word-break:break-all;background:#f3f3f3;padding:12px;border-radius:6px}</style>
<h2>Scraper service</h2>
<select id=svc>
  <option value="scrape">/scrape &mdash; modpro episode page</option>
  <option value="getlink">/getlink &mdash; shortener link</option>
  <option value="api/link">/api/link &mdash; driveseed file &rarr; Google Drive</option>
</select>
<input id=url placeholder="https://...">
<input id=key type=password placeholder="API key (if required)">
<button onclick="go()">Run</button>
<pre id=out></pre>
<script>
async function go(){
  const out=document.getElementById('out'); out.textContent='Working... (browser jobs can take up to a minute)';
  const u='/'+document.getElementById('svc').value+'?url='+encodeURIComponent(document.getElementById('url').value);
  try{
    const r=await fetch(u,{headers:{'X-API-Key':document.getElementById('key').value}});
    out.textContent=JSON.stringify(await r.json(),null,2);
  }catch(e){out.textContent='Error: '+e}
}
</script>"""


@app.route("/")
async def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=config.PORT)
