"""Comment embedding (CriticMarkup), stripping, and anchor fallback."""

from gdoc_sync.comments import _format_comment, embed_comments, strip_comments


def _comment(quoted, author, content, replies=()):
    return {
        "quotedFileContent": {"value": quoted},
        "author": {"displayName": author},
        "content": content,
        "replies": [
            {"author": {"displayName": a}, "content": c} for a, c in replies
        ],
    }


def test_embed_anchors_after_quoted_text():
    md = "Intro line.\nThe quick brown fox jumps.\nOutro.\n"
    out = embed_comments(md, [_comment("quick brown fox", "Ada", "nice phrase")])
    assert "fox{>>Ada: nice phrase<<}" in out


def test_embed_includes_replies():
    md = "Some text here.\n"
    out = embed_comments(md, [_comment("text", "Ada", "hm", replies=[("Bob", "agreed")])])
    assert "{>>Ada: hm | Bob: agreed<<}" in out


def test_embed_orphan_falls_back_to_end():
    md = "Nothing matches.\n"
    out = embed_comments(md, [_comment("absent phrase zz", "Ada", "lost")])
    assert out.startswith("Nothing matches.")
    assert "<!-- orphaned comment -->{>>Ada: lost<<}" in out


def test_multiple_insertions_do_not_shift_each_other():
    md = "alpha beta gamma delta\n"
    out = embed_comments(md, [
        _comment("alpha", "A", "first"),
        _comment("delta", "B", "last"),
    ])
    assert "alpha{>>A: first<<}" in out
    assert "delta{>>B: last<<}" in out


def test_strip_comments_removes_criticmarkup_and_html():
    md = "keep{>>Ada: gone<<} this\n<!-- orphaned comment -->{>>Bob: bye<<}\n"
    out = strip_comments(md)
    assert "Ada" not in out and "Bob" not in out and "<!--" not in out
    assert "keep this" in out


def test_format_comment_sanitizes_delimiters_and_newlines():
    cm = _format_comment("Ada", "line1\nline2 {>>evil<<}", [])
    assert "\n" not in cm
    assert cm.count("{>>") == 1 and cm.count("<<}") == 1


# ---------------------------------------------------------------------------
# Comment actions (reply / resolve / comment markers)
# ---------------------------------------------------------------------------

from gdoc_sync.comments import match_comment, parse_comment_actions  # noqa: E402


def test_parse_actions_none_in_plain_pulled_comments():
    md = "hello {>>Alice: tighten this<<} world"
    assert parse_comment_actions(md) == []


def test_parse_reply_binds_to_preceding_comment():
    md = "x {>>Alice: tighten this<<} y {>>reply: done, see rev 2<<} z"
    actions = parse_comment_actions(md)
    assert len(actions) == 1
    assert actions[0]["type"] == "reply"
    assert actions[0]["text"] == "done, see rev 2"
    assert actions[0]["target"] == "Alice: tighten this"


def test_parse_resolve_with_and_without_text():
    md = ("a {>>Bob: fix typo<<}{>>resolve<<} b "
          "{>>Cara: cite this<<} c {>>resolve: added citation<<}")
    actions = parse_comment_actions(md)
    assert [a["type"] for a in actions] == ["resolve", "resolve"]
    assert actions[0]["target"] == "Bob: fix typo"
    assert actions[0]["text"] == ""
    assert actions[1]["target"] == "Cara: cite this"
    assert actions[1]["text"] == "added citation"


def test_parse_new_comment_captures_context_line():
    md = "Intro paragraph.\nThe key claim here.{>>comment: needs a source<<}\nMore."
    actions = parse_comment_actions(md)
    assert actions[0]["type"] == "comment"
    assert actions[0]["text"] == "needs a source"
    assert actions[0]["context"] == "The key claim here."
    assert actions[0]["target"] is None


def test_match_comment_exact_and_prefix():
    remote = [
        {"id": "c1", "author": {"displayName": "Alice"},
         "content": "tighten this", "replies": []},
        {"id": "c2", "author": {"displayName": "Bob"},
         "content": "fix typo",
         "replies": [{"author": {"displayName": "Matt"}, "content": "ok"}]},
    ]
    assert match_comment("Alice: tighten this", remote)["id"] == "c1"
    # local target lacks the reply that exists remotely → prefix match
    assert match_comment("Bob: fix typo", remote)["id"] == "c2"
    # whitespace/case insensitive
    assert match_comment("  alice:   TIGHTEN this ", remote)["id"] == "c1"
    assert match_comment("Zed: unknown", remote) is None
