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
