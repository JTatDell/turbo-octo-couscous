# jt-webcrawler

Crawls the public pages of one website, renders each HTML page to a PDF with headless Chromium, and downloads any linked PDFs as-is. The result is a folder of PDFs to load into a RAG corpus, plus `manifest.csv` mapping every file to its source URL.

Two arguments are required:

- `--url` is the start URL of the target site. The crawl stays on that host.
- `--email` is the contact address. It is sent in every request as `User-Agent: jt-webcrawler/<VERSION> (+mailto:<email>)` so site operators can reach you.

## Output layout

```
out-<host>/pdf/*.pdf      corpus: one PDF per page, footer = "Source: <url> | Retrieved <UTC time>"
out-<host>/manifest.csv   url, final_url, title, status, file, bytes, sha256, retrieved_at_utc, skip note
out-<host>/frontier.txt   links not yet visited (lets an interrupted run resume)
out-<host>/html/          only with --save-html
```

## Install (any Linux machine, x86_64 or ARM64 such as GB10)

Download the release zip, unpack it, and run the installer. Rerunning it is safe: it skips what is already in place and repairs what is broken.

```bash
VER=1.1.0
cd ~ && curl -fLO https://github.com/JTatDell/turbo-octo-couscous/releases/download/v$VER/jt-webcrawler-$VER.zip
unzip -o jt-webcrawler-$VER.zip && bash jt-webcrawler-$VER/install.sh
```

The installer puts the tool and a private virtualenv in `~/jt-webcrawler` and a `jt-webcrawler` command in `~/.local/bin`. It uses sudo only when OS packages are missing: `python3-venv`, or the libraries Chromium needs. Options: `--prefix DIR`, `--bin-dir DIR`, `--no-system-deps`.

To install on several machines from one workstation (`-t` lets the sudo prompt through):

```bash
for h in promaxgb10-faea promaxgb10-4ade; do
  scp jt-webcrawler-1.1.0.zip "$h":~ && ssh -t "$h" 'unzip -o jt-webcrawler-1.1.0.zip && bash jt-webcrawler-1.1.0/install.sh'
done
```

Uninstall: `rm -rf ~/jt-webcrawler ~/.local/bin/jt-webcrawler`. Crawl output lives wherever you ran the tool, not in the install folder.

## Run

```bash
cd ~/corpora      # any folder; output goes to ./out-<host>
jt-webcrawler --url https://www.irmi.com/ --email jeff.tang@dell.com --max-pages 300 --delay 1.5
```

In a Claude Code cloud session, the environment's network policy must allow the target host and `cdn.playwright.dev`. Docs: https://code.claude.com/docs/en/cloud-environments#network-access

## Build the zip (maintainers)

```bash
jt-webcrawler/build-zip.sh      # writes dist/jt-webcrawler-<VERSION>.zip and .sha256
```

Bump `VERSION` first. It feeds the zip name, the installer banner, and the User-Agent.

## Flags

| Flag | Purpose |
|---|---|
| `--url URL` | Required. Absolute http(s) start URL. |
| `--email ADDR` | Required. Contact address for the User-Agent. |
| `--out DIR` | Output folder. Default is `./out-<host>`. |
| `--max-pages N` | Stop after N saved documents. Default 300. |
| `--delay S` | Seconds between requests. Raised automatically to robots.txt Crawl-delay. Default 1.5. |
| `--include REGEX` | Save only matching URLs, still follow all links. Example: `--include "/(glossary\|articles\|learning-center)/"` |
| `--exclude REGEX` | Replace the default skip list for login, account, cart and search pages. |
| `--save-html` | Also keep rendered HTML. Cleaner for chunking than PDF if the RAG loader accepts HTML. |
| `--no-sitemap` | Link-following only. |
| `--chromium PATH` | Use a specific Chromium binary. |

Rerunning with the same `--out` resumes and skips anything already in the manifest.

## What it deliberately does not do

- It does not log in or bypass paywalls. Subscriber-only content appears only as whatever teaser the public page shows.
- It does not fetch paths that robots.txt disallows. If robots.txt returns 401, 403 or 5xx, it aborts.
- It does not leave the start host. A subdomain such as `store.irmi.com` needs its own run.

## Verifying a run

```bash
ls out-www.irmi.com/pdf | wc -l      # documents saved
python3 -c "import csv,collections;print(collections.Counter(r['note'] or 'saved' for r in csv.DictReader(open('out-www.irmi.com/manifest.csv'))))"
```

Open two or three PDFs and check that the body text is present and no cookie banner covers it.
