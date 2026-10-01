# guns.lol username checker (hosted browser, no installs)

Checks guns.lol username availability by loading each profile in ScrapingAnt's
hosted headless Chrome over a simple HTTP API. Nothing browser-related runs on
your machine/host - only `requests` and `colorama` are needed.

## Modes (config.json -> "mode")
- "direct" (default): plain HTTP requests, free, no key. Try this first.
- "scrapingant": loads pages in ScrapingAnt's hosted Chrome (needs a free key, ~10 credits/check).
The startup self-test tells you if direct mode can't reliably tell free from taken names,
and prints what the site actually returned.

## Setup (scrapingant mode only)
1. Make a free account at https://scrapingant.com (10,000 credits/month, no card).
2. Copy your API key into config.json -> "scrapingant_key"
   (or set the environment variable SCRAPINGANT_KEY).
3. pip install -r requirements.txt   (or install.bat on Windows)
4. python 67.py

## Credits
Each check uses ~10 credits (JavaScript rendering), so the free plan is roughly
1,000 checks per month. "max_requests" (default 900) stops the script before you
burn the whole allowance. The 2 self-test requests count too.
Random 5-character names have low hit rates - a customlist.txt of names you
actually care about makes much better use of the quota.

## Config
- letter_count, delay, use_customlist, filter_premium, save_to_file, webhook_url
- known_taken: a username you know is claimed (full startup self-test)
- scrapingant_key / max_requests: see above

## Verify detection
    python 67.py --probe zzqxkvbwnrtpl12345 somerealname
