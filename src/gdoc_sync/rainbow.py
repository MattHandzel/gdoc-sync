#!/usr/bin/env python3
"""
Apply rainbow colors to alternating characters of the first paragraph in a Google Doc.

Usage: python3 gdoc_rainbow.py <doc_url_or_id> [--tab <tab_id>] [--words] [--dry-run]

--tab:      tab id (with or without the "t." prefix from URL). If omitted, uses the
            first tab with content.
--words:    cycle colors per whitespace-separated word instead of per character.
--dry-run:  print what would change; don't apply.
"""
import argparse
import re
import sys

from googleapiclient.discovery import build

from .auth import get_credentials
from .services import NUM_RETRIES

RAINBOW = [
    (0.898, 0.224, 0.208),  # red
    (0.984, 0.549, 0.000),  # orange
    (0.945, 0.768, 0.059),  # yellow (darkened for readability)
    (0.263, 0.627, 0.278),  # green
    (0.118, 0.533, 0.898),  # blue
    (0.224, 0.286, 0.671),  # indigo
    (0.557, 0.141, 0.667),  # violet
]


def extract_doc_id(s: str) -> str:
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else s


def find_tab(tabs: list, wanted: str | None):
    def walk(ts):
        for t in ts:
            yield t
            for child in t.get("childTabs", []) or []:
                yield from walk([child])

    if not tabs:
        return None
    if not wanted:
        return tabs[0]
    wanted = wanted.removeprefix("t.")
    for t in walk(tabs):
        tid = (t.get("tabProperties") or {}).get("tabId", "")
        if tid.removeprefix("t.") == wanted:
            return t
    return None


def first_text_paragraph(body_content: list):
    """Find first paragraph element with actual printable characters; return (start, end, text)."""
    for el in body_content:
        para = el.get("paragraph")
        if not para:
            continue
        segments = []
        p_start = None
        p_end = None
        for run_el in para.get("elements", []):
            run = run_el.get("textRun")
            if not run:
                continue
            start = run_el.get("startIndex")
            end = run_el.get("endIndex")
            text = run.get("content", "")
            segments.append((start, end, text))
            if p_start is None:
                p_start = start
            p_end = end
        if not segments:
            continue
        combined = "".join(t for _, _, t in segments)
        if combined.strip():
            return p_start, p_end, combined, segments
    return None


def build_requests(segments, tab_id: str | None, by_words: bool):
    """Generate UpdateTextStyle batchUpdate requests covering each char/word."""
    reqs = []
    color_i = 0

    if by_words:
        for seg_start, _seg_end, text in segments:
            i = 0
            while i < len(text):
                while i < len(text) and text[i].isspace():
                    i += 1
                word_start = i
                while i < len(text) and not text[i].isspace():
                    i += 1
                word_end = i
                if word_end <= word_start:
                    continue
                r, g, b = RAINBOW[color_i % len(RAINBOW)]
                color_i += 1
                reqs.append(_mk_style_req(seg_start + word_start, seg_start + word_end, r, g, b, tab_id))
        return reqs

    # Per-character
    for seg_start, _seg_end, text in segments:
        for j, ch in enumerate(text):
            if ch in ("\n", "\r"):
                continue
            r, g, b = RAINBOW[color_i % len(RAINBOW)]
            if not ch.isspace():
                color_i += 1
            reqs.append(_mk_style_req(seg_start + j, seg_start + j + 1, r, g, b, tab_id))
    return reqs


def _mk_style_req(start: int, end: int, r: float, g: float, b: float, tab_id: str | None):
    rng = {"startIndex": start, "endIndex": end}
    if tab_id:
        rng["tabId"] = tab_id
    return {
        "updateTextStyle": {
            "range": rng,
            "textStyle": {
                "foregroundColor": {"color": {"rgbColor": {"red": r, "green": g, "blue": b}}}
            },
            "fields": "foregroundColor",
        }
    }


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(prog="gdoc-sync rainbow")
    ap.add_argument("doc")
    ap.add_argument("--tab", default=None)
    ap.add_argument("--words", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    doc_id = extract_doc_id(args.doc)
    creds = get_credentials()
    docs = build("docs", "v1", credentials=creds)

    doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute(num_retries=NUM_RETRIES)

    tabs = doc.get("tabs") or []
    tab_id = None
    if tabs:
        tab = find_tab(tabs, args.tab)
        if not tab:
            print(f"ERROR: tab id {args.tab!r} not found", file=sys.stderr)
            sys.exit(1)
        tab_id = (tab.get("tabProperties") or {}).get("tabId")
        body_content = (tab.get("documentTab") or {}).get("body", {}).get("content", [])
        print(f"Tab: {tab.get('tabProperties', {}).get('title', '(untitled)')} (id={tab_id})")
    else:
        body_content = doc.get("body", {}).get("content", [])
        print("Tab: (no tabs; using top-level body)")

    found = first_text_paragraph(body_content)
    if not found:
        print("ERROR: no paragraph with text found", file=sys.stderr)
        sys.exit(1)

    p_start, p_end, combined, segments = found
    print(f"First paragraph ({p_start}..{p_end}):")
    print(f"  {combined.rstrip()!r}")

    reqs = build_requests(segments, tab_id, args.words)
    mode = "words" if args.words else "chars"
    print(f"Generated {len(reqs)} UpdateTextStyle requests ({mode}-alternating, {len(RAINBOW)}-color rainbow)")

    if args.dry_run:
        print("--dry-run: not applying.")
        return

    docs.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute(num_retries=NUM_RETRIES)
    print("Applied.")


if __name__ == "__main__":
    main()
