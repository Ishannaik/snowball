#!/usr/bin/env python3
"""Forum search helpers for the research agent. Each subcommand prints compact text
with a source URL and a date on every item, so the agent can cite without guessing.

  forum.py reddit-search --sub LocalLLaMA --title "exl3" [--after 2026-01-01] [--limit 15]
  forum.py reddit-comments --sub LocalLLaMA --body "exl3" [--after 2026-01-01]
  forum.py reddit-thread <post_id>            # post body + top comments, flattened
  forum.py hn-search "query" [--days 365] [--comments]
  forum.py hn-item <id>                        # story + top comments
  forum.py gh-search "query" [--discussions]
  forum.py discourse <host> "query"           # e.g. discuss.huggingface.co
  forum.py fetch <url> [--camoufox]           # page text; Camoufox only for Cloudflare walls

Stdlib only. Every network call has a timeout. Errors print one line and exit 1.
"""

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0 snowball/0.1"
ARCTIC = "https://arctic-shift.photon-reddit.com"
MAX_BODY = 600  # chars per post/comment body shown to the agent


def get_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def day(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(int(float(ts)), dt.timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(ts)[:10]


def clip(text: str, n: int = MAX_BODY) -> str:
    text = re.sub(r"\s+", " ", html.unescape(text or "")).strip()
    return text if len(text) <= n else text[:n] + " ..."


def reddit_search(a):
    q = {"subreddit": a.sub, "limit": a.limit, "sort": "desc"}
    if a.title:
        q["title"] = a.title
    if a.after:
        q["after"] = a.after
    d = get_json(f"{ARCTIC}/api/posts/search?{urllib.parse.urlencode(q)}")
    if d.get("error"):
        sys.exit(f"arctic-shift error: {d['error']}")
    for p in d.get("data") or []:
        print(f"- [{day(p.get('created_utc'))}] r/{p.get('subreddit')} id={p.get('id')} "
              f"score={p.get('score')} comments={p.get('num_comments')}\n  {p.get('title')}\n"
              f"  https://www.reddit.com{p.get('permalink', '')}\n  {clip(p.get('selftext', ''), 300)}")


def reddit_comments(a):
    q = {"subreddit": a.sub, "body": a.body, "limit": a.limit, "sort": "desc"}
    if a.after:
        q["after"] = a.after
    d = get_json(f"{ARCTIC}/api/comments/search?{urllib.parse.urlencode(q)}", timeout=90)
    if d.get("error"):
        sys.exit(f"arctic-shift error: {d['error']} (try reddit-search --title instead)")
    for c in d.get("data") or []:
        link = (c.get("link_id") or "").replace("t3_", "")
        print(f"- [{day(c.get('created_utc'))}] u/{c.get('author')} score={c.get('score')} thread={link}\n"
              f"  https://www.reddit.com/r/{c.get('subreddit')}/comments/{link}/_/{c.get('id')}/\n"
              f"  {clip(c.get('body', ''))}")


def _walk(nodes, depth, out):
    for n in nodes or []:
        c = n.get("data", n)
        if c.get("body"):
            out.append((depth, c))
        rep = c.get("replies")
        if isinstance(rep, dict):
            _walk(rep.get("data", {}).get("children"), depth + 1, out)
        elif isinstance(rep, list):
            _walk(rep, depth + 1, out)


def reddit_thread(a):
    pid = a.id.replace("t3_", "")
    post = (get_json(f"{ARCTIC}/api/posts/ids?ids={pid}").get("data") or [{}])[0]
    if post:
        print(f"POST [{day(post.get('created_utc'))}] r/{post.get('subreddit')} score={post.get('score')}: "
              f"{post.get('title')}\nhttps://www.reddit.com{post.get('permalink', '')}\n"
              f"{clip(post.get('selftext', ''), 1500)}\n")
    d = get_json(f"{ARCTIC}/api/comments/tree?link_id=t3_{pid}&limit={a.limit}", timeout=90)
    out = []
    _walk(d.get("data"), 0, out)
    out.sort(key=lambda x: (x[0] > 1, -(x[1].get("score") or 0)))  # top-level and replies first, by score
    for depth, c in out[: a.limit]:
        print(f"{'  ' * min(depth, 2)}- [{day(c.get('created_utc'))}] u/{c.get('author')} score={c.get('score')} "
              f"https://www.reddit.com/comments/{pid}/_/{c.get('id')}/\n{'  ' * min(depth, 2)}  {clip(c.get('body', ''))}")


def hn_search(a):
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).timestamp())
    tags = "comment" if a.comments else "story"
    q = {"query": a.query, "tags": tags, "hitsPerPage": a.limit, "numericFilters": f"created_at_i>{since}"}
    d = get_json(f"https://hn.algolia.com/api/v1/search?{urllib.parse.urlencode(q)}")
    for h in d.get("hits", []):
        url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        title = h.get("title") or h.get("story_title") or ""
        body = clip(h.get("comment_text") or h.get("story_text") or "", 400)
        print(f"- [{day(h.get('created_at_i'))}] points={h.get('points')} comments={h.get('num_comments')} "
              f"{title}\n  {url}  link={h.get('url') or ''}\n  {body}")


def hn_item(a):
    d = get_json(f"https://hn.algolia.com/api/v1/items/{a.id}")
    print(f"STORY [{day(d.get('created_at_i'))}] {d.get('title')}\nhttps://news.ycombinator.com/item?id={a.id}\n"
          f"{clip(d.get('text') or '', 1000)}\n")
    shown = 0
    for c in d.get("children", []):
        if shown >= a.limit:
            break
        if c.get("text"):
            shown += 1
            print(f"- [{day(c.get('created_at_i'))}] {c.get('author')} https://news.ycombinator.com/item?id={c['id']}\n"
                  f"  {clip(c['text'])}")


def gh_search(a):
    if a.discussions:
        gql = ("query($q:String!){search(query:$q,type:DISCUSSION,first:%d){nodes{... on Discussion"
               "{title url createdAt comments{totalCount} bodyText}}}}" % a.limit)
        r = subprocess.run(["gh", "api", "graphql", "-f", f"query={gql}", "-f", f"q={a.query}"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode:
            sys.exit(f"gh error: {r.stderr.strip()[:300]}")
        for n in json.loads(r.stdout)["data"]["search"]["nodes"]:
            print(f"- [{n['createdAt'][:10]}] comments={n['comments']['totalCount']} {n['title']}\n  {n['url']}\n"
                  f"  {clip(n.get('bodyText', ''), 400)}")
        return
    r = subprocess.run(["gh", "search", "issues", a.query, "--limit", str(a.limit), "--sort", "updated",
                        "--json", "title,url,createdAt,commentsCount,state,body"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        sys.exit(f"gh error: {r.stderr.strip()[:300]}")
    for i in json.loads(r.stdout):
        print(f"- [{i['createdAt'][:10]}] {i['state']} comments={i['commentsCount']} {i['title']}\n  {i['url']}\n"
              f"  {clip(i.get('body', ''), 400)}")


def discourse(a):
    host = a.host.rstrip("/")
    if not host.startswith("http"):
        host = "https://" + host
    d = get_json(f"{host}/search.json?{urllib.parse.urlencode({'q': a.query})}")
    topics = {t["id"]: t for t in d.get("topics", [])}
    for p in d.get("posts", [])[: a.limit]:
        t = topics.get(p.get("topic_id"), {})
        print(f"- [{(p.get('created_at') or '')[:10]}] {t.get('title', '')}\n"
              f"  {host}/t/{t.get('slug', 'topic')}/{p.get('topic_id')}/{p.get('post_number', 1)}\n  {clip(p.get('blurb', ''), 400)}")


def fetch(a):
    if not a.camoufox:
        try:
            req = urllib.request.Request(a.url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read(3_000_000).decode("utf-8", "replace")
            raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
            print(clip(re.sub(r"<[^>]+>", " ", raw), a.chars))
            return
        except urllib.error.HTTPError as e:
            if e.code not in (403, 503):
                sys.exit(f"fetch error: HTTP {e.code}")
            print(f"(HTTP {e.code}, retrying with Camoufox)", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - one-line error for the agent
            sys.exit(f"fetch error: {e}")
    # Camoufox for Cloudflare walls only. The context manager closes the browser on every path.
    code = (
        "import sys\nfrom camoufox.sync_api import Camoufox\n"
        "with Camoufox(headless=True, geoip=True) as b:\n"
        "    p = b.new_page(); p.goto(sys.argv[1], wait_until='domcontentloaded', timeout=60000)\n"
        "    p.wait_for_timeout(6000); print(p.inner_text('body')[:int(sys.argv[2])])\n"
    )
    r = subprocess.run(["/home/ubuntu/.venv-camoufox/bin/python", "-c", code, a.url, str(a.chars)],
                       capture_output=True, text=True, timeout=180)
    print(r.stdout if r.returncode == 0 else f"camoufox error: {r.stderr.strip()[-300:]}")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("reddit-search"); s.add_argument("--sub", required=True); s.add_argument("--title")
    s.add_argument("--after"); s.add_argument("--limit", type=int, default=15); s.set_defaults(f=reddit_search)
    s = sp.add_parser("reddit-comments"); s.add_argument("--sub", required=True); s.add_argument("--body", required=True)
    s.add_argument("--after"); s.add_argument("--limit", type=int, default=15); s.set_defaults(f=reddit_comments)
    s = sp.add_parser("reddit-thread"); s.add_argument("id"); s.add_argument("--limit", type=int, default=25)
    s.set_defaults(f=reddit_thread)
    s = sp.add_parser("hn-search"); s.add_argument("query"); s.add_argument("--days", type=int, default=365)
    s.add_argument("--comments", action="store_true"); s.add_argument("--limit", type=int, default=10)
    s.set_defaults(f=hn_search)
    s = sp.add_parser("hn-item"); s.add_argument("id"); s.add_argument("--limit", type=int, default=15)
    s.set_defaults(f=hn_item)
    s = sp.add_parser("gh-search"); s.add_argument("query"); s.add_argument("--discussions", action="store_true")
    s.add_argument("--limit", type=int, default=10); s.set_defaults(f=gh_search)
    s = sp.add_parser("discourse"); s.add_argument("host"); s.add_argument("query")
    s.add_argument("--limit", type=int, default=10); s.set_defaults(f=discourse)
    s = sp.add_parser("fetch"); s.add_argument("url"); s.add_argument("--camoufox", action="store_true")
    s.add_argument("--chars", type=int, default=6000); s.set_defaults(f=fetch)
    a = ap.parse_args()
    try:
        a.f(a)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        sys.exit(f"{a.cmd} error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
