"""Comment embedding (CriticMarkup), stripping, and anchor fallback."""

from gdoc_sync.comments import (
    POINT_KEY,
    _format_comment,
    embed_comments,
    parse_comment_actions,
    strip_comments,
)


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
    assert "The {==quick brown fox==}{>>Ada: nice phrase<<} jumps." in out


def test_embed_includes_replies():
    md = "Some text here.\n"
    out = embed_comments(md, [_comment("text", "Ada", "hm", replies=[("Bob", "agreed")])])
    assert "{>>Ada: hm | Bob: agreed<<}" in out


def test_embed_orphan_falls_back_to_end():
    md = "Nothing matches.\n"
    out = embed_comments(md, [_comment("absent phrase zz", "Ada", "lost")])
    assert out.startswith("Nothing matches.")
    assert "<!-- orphaned comment, was on: “absent phrase zz” -->{>>Ada: lost<<}" in out


def test_multiple_insertions_do_not_shift_each_other():
    md = "alpha beta gamma delta\n"
    out = embed_comments(md, [
        _comment("alpha", "A", "first"),
        _comment("delta", "B", "last"),
    ])
    assert "{==alpha==}{>>A: first<<}" in out
    assert "{==delta==}{>>B: last<<}" in out


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

from gdoc_sync.comments import match_comment  # noqa: E402


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


def test_parse_reports_the_span_of_each_action_marker():
    md = "a {>>Alice: hi<<} b {>>reply: ok<<} c"
    (action,) = parse_comment_actions(md)
    start, end = action["span"]
    assert md[start:end] == "{>>reply: ok<<}"


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


# ---------------------------------------------------------------------------
# A fake Drive service, shared by the safety tests below
# ---------------------------------------------------------------------------

from gdoc_sync.comments import (  # noqa: E402
    CommentActionResult,
    _sanitize_author,
    anchored_push_warning,
    apply_comment_actions,
    consume_action_markers,
    count_anchored_comments,
)


def _http_error():
    from googleapiclient.errors import HttpError

    class _Resp:
        status = 500
        reason = "Server Error"

    return HttpError(_Resp(), b'{"error": {"message": "boom"}}', uri="http://x")


class _Exec:
    def __init__(self, fail, payload=None):
        self.fail, self.payload = fail, payload

    def execute(self, **_kw):
        if self.fail:
            raise _http_error()
        return self.payload if self.payload is not None else {"id": "new"}


class _FakeReplies:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    def create(self, **kw):
        self.log.append(("replies.create", kw))
        return _Exec(self.fail)


class _FakeComments:
    def __init__(self, log, comments, fail=False):
        self.log, self.comments, self.fail = log, comments, fail

    def list(self, **kw):
        self.log.append(("comments.list", kw))
        return _Exec(False, {"comments": self.comments})

    def create(self, **kw):
        self.log.append(("comments.create", kw))
        return _Exec(self.fail)


class FakeDrive:
    """Just enough Drive to drive apply_comment_actions, with a write log."""

    def __init__(self, comments=(), fail=False):
        self.log: list[tuple] = []
        self._comments = [dict(c, id=c.get("id", f"c{i}"))
                          for i, c in enumerate(comments)]
        self._fail = fail

    def comments(self):
        return _FakeComments(self.log, self._comments, self._fail)

    def replies(self):
        return _FakeReplies(self.log, self._fail)

    @property
    def writes(self):
        return [c for c in self.log if c[0] != "comments.list"]


def _remote(author, content, quoted="the quoted words", replies=()):
    return {
        "author": {"displayName": author},
        "content": content,
        "quotedFileContent": {"value": quoted},
        "replies": [{"author": {"displayName": a}, "content": c}
                    for a, c in replies],
    }


# ---------------------------------------------------------------------------
# P0-1: a collaborator's display name must not be executable
# ---------------------------------------------------------------------------

HOSTILE_NAMES = ["resolve", "RESOLVE ", " reply", "comment", "Comment"]


def test_sanitize_author_quotes_action_lookalikes():
    for name in HOSTILE_NAMES:
        assert _sanitize_author(name).startswith('"'), name
    assert _sanitize_author("Alice") == "Alice"
    assert _sanitize_author("  ") == "Unknown"
    assert _sanitize_author(None) == "Unknown"
    assert _sanitize_author("Ada\nLovelace {>>x<<}") == "Ada Lovelace x"


def test_hostile_display_name_parses_as_no_action():
    md = "The quoted words are here.\n"
    for name in HOSTILE_NAMES:
        out = embed_comments(md, [_comment("quoted words", name, "ok")])
        assert parse_comment_actions(out) == [], name


def test_hostile_reply_author_parses_as_no_action():
    md = "The quoted words are here.\n"
    out = embed_comments(md, [
        _comment("quoted words", "Alice", "look", replies=[("resolve", "ok")])
    ])
    assert parse_comment_actions(out) == []
    assert '"resolve": ok' in out


def test_hostile_display_name_makes_no_api_writes():
    for name in HOSTILE_NAMES:
        pulled = embed_comments("The quoted words are here.\n",
                                [_comment("quoted words", name, "ok")])
        drive = FakeDrive([_remote("Victim", "please fix", "quoted words")])
        result = apply_comment_actions(drive, "doc-1", pulled)
        assert drive.writes == [], name
        assert result.applied == []


def test_normal_author_round_trips_and_still_matches():
    pulled = embed_comments("The quoted words are here.\n",
                            [_comment("quoted words", "Alice", "tighten this")])
    assert "{>>Alice: tighten this<<}" in pulled
    remote = [_remote("Alice", "tighten this", "quoted words")]
    assert match_comment("Alice: tighten this", remote) is not None


def test_sanitized_author_still_matches_its_remote_comment():
    remote = [_remote("resolve", "ok", "quoted words")]
    assert match_comment('"resolve": ok', remote) is not None


def test_legitimate_resolve_marker_after_a_pulled_comment_still_fires():
    pulled = embed_comments("The quoted words are here.\n",
                            [_comment("quoted words", "Alice", "tighten this")])
    md = pulled.replace("<<}", "<<}{>>resolve: done<<}", 1)
    drive = FakeDrive([_remote("Alice", "tighten this", "quoted words")])
    result = apply_comment_actions(drive, "doc-1", md)
    assert [c[0] for c in drive.writes] == ["replies.create"]
    assert drive.writes[0][1]["body"]["action"] == "resolve"
    assert len(result.applied) == 1


# ---------------------------------------------------------------------------
# P0-11: applied action markers must be consumed
# ---------------------------------------------------------------------------

def authored_file() -> str:
    """A pulled comment the user answered, plus a brand-new comment."""
    pulled = embed_comments("The quoted words are here.\n",
                            [_comment("quoted words", "Alice", "tighten this")])
    return (pulled.rstrip("\n")
            + "{>>reply: thanks<<}\n\n{>>comment: needs source<<}\n")


def test_apply_reports_spans_of_applied_markers():
    md = authored_file()
    drive = FakeDrive([_remote("Alice", "tighten this", "quoted words")])
    result = apply_comment_actions(drive, "doc-1", md)
    assert isinstance(result, CommentActionResult)
    assert len(result.applied) == 2
    consumed = consume_action_markers(md, result.applied)
    assert "{>>reply:" not in consumed and "{>>comment:" not in consumed
    assert "{>>Alice: tighten this<<}" in consumed


def test_consumed_file_makes_no_further_api_writes():
    md = authored_file()
    remote = [_remote("Alice", "tighten this", "quoted words")]
    drive = FakeDrive(remote)
    consumed = consume_action_markers(
        md, apply_comment_actions(drive, "d", md).applied)
    again = FakeDrive(remote)
    result = apply_comment_actions(again, "d", consumed)
    assert again.writes == []
    assert result.applied == []


def test_failed_action_keeps_its_marker():
    md = authored_file()
    drive = FakeDrive([_remote("Alice", "tighten this", "quoted words")], fail=True)
    result = apply_comment_actions(drive, "doc-1", md)
    assert result.applied == []
    assert all("failed" in line for line in result.lines)
    assert consume_action_markers(md, result.applied) == md


def test_skipped_action_keeps_its_marker():
    # An unmatchable target and an empty reply are both skipped, not applied.
    md = "{>>Nobody: unmatched<<}{>>reply: hi<<} {>>reply:<<}"
    drive = FakeDrive([])
    result = apply_comment_actions(drive, "doc-1", md)
    assert result.applied == []
    assert drive.writes == []


def test_only_one_comments_list_per_apply():
    md = authored_file()
    drive = FakeDrive([_remote("Alice", "tighten this", "quoted words")])
    apply_comment_actions(drive, "doc-1", md)
    assert [c[0] for c in drive.log].count("comments.list") == 1


def test_no_actions_does_not_fetch_comments():
    drive = FakeDrive([_remote("Alice", "hi", "quoted words")])
    result = apply_comment_actions(drive, "doc-1", "plain text, no markers")
    assert drive.log == []
    assert result.remote is None


# ---------------------------------------------------------------------------
# P0-4 mitigation: warn that a push detaches anchored comments
# ---------------------------------------------------------------------------

def test_anchored_comment_warning_counts_only_anchored():
    comments = [
        _remote("Alice", "a", "quoted words"),
        _remote("Bob", "b", "more words"),
        {"author": {"displayName": "Cara"}, "content": "c", "replies": []},
    ]
    assert count_anchored_comments(comments) == 2
    warning = anchored_push_warning(comments)
    assert warning is not None
    assert "2 anchored comment(s) will lose their anchor" in warning
    assert "re-attach on the next pull" in warning


def test_no_anchored_comments_means_no_warning():
    assert anchored_push_warning([]) is None
    assert anchored_push_warning(
        [{"author": {"displayName": "C"}, "content": "c", "replies": []}]) is None


# ---------------------------------------------------------------------------
# Anchoring a quote that crosses markdown syntax (the orphaned-comment bug)
# ---------------------------------------------------------------------------

from gdoc_sync.anchors import plain_quote  # noqa: E402
from gdoc_sync.comments import fetch_comments  # noqa: E402


def _html_comment(quoted_html, author="Ada", content="note"):
    c = _comment(quoted_html, author, content)
    c["quotedFileContent"]["mimeType"] = "text/html"
    return c


def test_html_escaped_quote_is_unescaped_before_matching():
    # Drive sends the selection as text/html: `wasn't` arrives as `wasn&#39;t`.
    md = 'It wasn\'t out and "done" meant registered.\n'
    out = embed_comments(md, [_html_comment(
        "It wasn&#39;t out and &quot;done&quot; meant registered.")])
    assert "orphaned" not in out
    assert 'meant registered.==}{>>Ada: note<<}' in out


def test_plain_quote_leaves_plain_text_alone():
    assert plain_quote({"mimeType": "text/plain", "value": "a &amp; b"}) == "a &amp; b"
    assert plain_quote({"mimeType": "text/html", "value": "a &amp; b<br>c"}) == "a & b\nc"


def test_fetch_comments_hands_back_plain_quotes():
    drive = FakeDrive([{
        "id": "c1", "author": {"displayName": "Ada"}, "content": "x",
        "quotedFileContent": {"mimeType": "text/html", "value": "I&#39;d go"},
        "replies": [],
    }])
    [c] = fetch_comments(drive, "doc-1")
    assert c["quotedFileContent"]["value"] == "I'd go"


def test_quote_across_bold_and_link_anchors_after_the_link():
    md = "I read **Who** and [the post](https://x.test/p) twice.\n"
    out = embed_comments(md, [_comment("I read Who and the post", "Ada", "cite")])
    assert "{==I read **Who** and [the post](https://x.test/p)==}{>>Ada: cite<<} twice." in out


def test_quote_ending_inside_bold_anchors_after_the_closing_marker():
    md = "Plain **bold words** and more.\n"
    out = embed_comments(md, [_comment("Plain bold words", "Ada", "hm")])
    assert "{==Plain **bold words**==}{>>Ada: hm<<} and more." in out


def test_quote_spanning_heading_and_list_anchors_after_the_last_item():
    md = ("### Mistakes\n\n- I designed interviews first.\n"
          "- Nobody was in the pipeline.\n\nNext paragraph.\n")
    quote = "Mistakes\nI designed interviews first.\nNobody was in the pipeline."
    out = embed_comments(md, [_comment(quote, "Matt", "paragraphs please")])
    assert "orphaned" not in out
    assert "### {==Mistakes==}\n\n- {==I designed interviews first.==}\n- {==Nobody was in the pipeline.==}{>>Matt: paragraphs please<<}\n" in out


def test_quote_over_a_footnote_reference_and_curly_quotes():
    md = "The club was analogous.[^1] It’s fine.\n\n[^1]: A note.\n"
    out = embed_comments(md, [_comment("The club was analogous. It's fine.", "Ada", "ok")])
    assert "{==The club was analogous.[^1] It’s fine.==}{>>Ada: ok<<}" in out


def test_quote_across_table_cells():
    md = "| a | b |\n|---|---|\n| cell one | cell two |\n\nAfter.\n"
    out = embed_comments(md, [_comment("a\nb\ncell one\ncell two", "Ada", "t")])
    assert "| {==a==} | {==b==} |\n|---|---|\n| {==cell one==} | {==cell two==}{>>Ada: t<<} |" in out


def test_edited_middle_of_a_multiparagraph_quote_still_anchors():
    md = ("First paragraph that the reviewer selected.\n\n"
          "A middle paragraph rewritten since.\n\n"
          "Last paragraph of the selection here.\n")
    quote = ("First paragraph that the reviewer selected.\n"
             "The middle paragraph as it used to read.\n"
             "Last paragraph of the selection here.")
    out = embed_comments(md, [_comment(quote, "Ada", "x")])
    assert "{==First paragraph that the reviewer selected.==}\n\nA middle paragraph rewritten since.\n\n{==Last paragraph of the selection here.==}{>>Ada: x<<}" in out


def test_comment_never_lands_inside_code_or_a_link_target():
    md = "Run `make test` now, see [docs](https://d.test).\n"
    out = embed_comments(md, [_comment("Run make", "Ada", "a"),
                              _comment("see do", "Bob", "b")])
    assert "{==Run `make test`==}{>>Ada: a<<}" in out
    assert "{==see [docs](https://d.test)==}{>>Bob: b<<}" in out


def test_existing_comment_between_words_does_not_break_the_match():
    md = "keep this{>>Ada: old<<} sentence whole.\n"
    out = embed_comments(md, [_comment("keep this sentence whole.", "Bob", "new")])
    assert "sentence whole.==}{>>Bob: new<<}" in out


# A comment left at a cursor, with no text selected ("add something here")

def _docx(body_xml, comments_xml):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document><w:body>{body_xml}</w:body></w:document>")
        z.writestr("word/comments.xml", f"<w:comments>{comments_xml}</w:comments>")
    return buf.getvalue()


def _note(cid, author, text):
    return (f'<w:comment w:id="{cid}" w:author="{author}">'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>")


def test_point_comment_covers_the_end_of_the_line_before_it():
    from gdoc_sync.anchors import docx_comment_ranges
    body = ('<w:p><w:r><w:t>Ask for referrals from the network.</w:t></w:r>'
            '<w:commentRangeStart w:id="3"/><w:r><w:t xml:space="preserve"> </w:t></w:r>'
            '<w:commentRangeEnd w:id="3"/></w:p>'
            '<w:p><w:r><w:t>Written by founders.</w:t></w:r>'
            '<w:commentRangeStart w:id="5"/></w:p><w:p>'
            '<w:commentRangeEnd w:id="5"/><w:r><w:t>Next.</w:t></w:r></w:p>')
    data = _docx(body, _note(3, "Matt", "add something here") + _note(5, "Matt", "ai here"))
    assert docx_comment_ranges(data) == [
        ("Matt", "add something here", "Ask for referrals from the network.", True),
        ("Matt", "ai here", "Written by founders.", True),
    ]


def test_point_comment_is_placed_not_orphaned():
    from gdoc_sync.anchors import docx_comment_ranges
    body = ('<w:p><w:r><w:t>Ask for referrals from the network.</w:t></w:r>'
            '<w:commentRangeStart w:id="3"/><w:commentRangeEnd w:id="3"/></w:p>')
    (_, _, covered, point), = docx_comment_ranges(_docx(body, _note(3, "Matt", "more?")))
    md = "Intro.\n\nAsk for referrals from the network.\n\nOutro.\n"
    comment = _comment(covered, "Matt", "more?")
    comment[POINT_KEY] = point
    out = embed_comments(md, [comment])
    assert "orphaned" not in out
    assert "network.{>>Matt: more?<<}\n" in out

    assert "{==" not in out  # nothing was selected, so nothing is highlighted


def test_selected_text_is_a_range_not_the_line_before():
    from gdoc_sync.anchors import docx_comment_ranges
    body = ('<w:p><w:r><w:t xml:space="preserve">Keep </w:t></w:r>'
            '<w:commentRangeStart w:id="1"/><w:r><w:t>these words</w:t></w:r>'
            '<w:commentRangeEnd w:id="1"/><w:r><w:t xml:space="preserve"> only.</w:t></w:r></w:p>')
    assert docx_comment_ranges(_docx(body, _note(1, "Ada", "x"))) == [
        ("Ada", "x", "these words", False)]


# Highlighting the selected text, contiguous or not

def test_partly_rewritten_selection_highlights_each_surviving_part():
    md = ("We decided it made more sense to first lock in funding before we put "
          "anyone through the hiring process we had designed.\n")
    quote = ("We decided it made more sense to first lock in funding before putting "
             "people through our hiring process we had designed.")
    out = embed_comments(md, [_comment(quote, "Matt", "weird")])
    assert out.startswith("{==We decided it made more sense to first lock in funding before")
    assert "hiring process we had designed.==}{>>Matt: weird<<}" in out
    assert out.count("{==") >= 2  # the rewritten words in between are not highlighted


def test_overlapping_selections_never_nest():
    md = "One two three four five six.\n"
    out = embed_comments(md, [_comment("One two three four five six.", "Ada", "all"),
                              _comment("three four", "Bob", "some")])
    assert out == "{==One two three four==}{>>Bob: some<<}{== five six.==}{>>Ada: all<<}\n"
    assert strip_comments(out) == md


def test_doc_level_note_is_placed_without_a_highlight():
    md = "A line we talked about.\n"
    note = {"author": {"displayName": "Matt"}, "replies": [],
            "content": "Re: “A line we talked about.”\n\nsource?"}
    out = embed_comments(md, [note])
    assert "{==" not in out
    assert "about.{>>Matt: Re:" in out


def test_highlights_never_reach_a_pushed_doc():
    md = "{==Some **bold** text==}{>>Ada: hm<<} and {==more==}{>>Bob: ok<<}.\n"
    assert strip_comments(md) == "Some **bold** text and more.\n"


def test_highlighted_file_still_anchors_the_same_quote():
    from gdoc_sync.anchors import find_anchor, project
    md = "The {==quick brown fox==}{>>Ada: hm<<} jumps.\n"
    pos = find_anchor(project(md), "quick brown fox jumps")
    assert md[:pos].endswith("jumps")


def test_new_comment_after_a_highlight_quotes_exactly_the_highlight():
    md = "Intro line.\nWe {==took hiring==}{>>comment: say why<<} first.\n"
    (action,) = parse_comment_actions(md)
    assert action["type"] == "comment"
    assert action["context"] == "took hiring"


def test_new_comment_without_a_highlight_still_quotes_the_line():
    md = "{==Earlier==}{>>Ada: x<<}\nWe took hiring first.{>>comment: say why<<}\n"
    actions = [a for a in parse_comment_actions(md) if a["type"] == "comment"]
    assert actions[0]["context"] == "We took hiring first."


# Live anchors: where the comment is in the doc now, not where it was made

class _Req:
    def __init__(self, result):
        self.result = result

    def execute(self, **_):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _FakeDrive:
    """Drive with a comment list and a .docx export."""

    def __init__(self, comments, docx):
        self._comments, self._docx = comments, docx

    def comments(self):
        drive = self

        class _C:
            def list(self, **_):
                return _Req({"comments": drive._comments})
        return _C()

    def files(self):
        drive = self

        class _F:
            def export(self, **_):
                return _Req(drive._docx)
        return _F()


def _drive_comment(author, content, quote, anchor="kix.a"):
    return {"author": {"displayName": author}, "content": content, "anchor": anchor,
            "quotedFileContent": {"mimeType": "text/html", "value": quote}}


def _ranged(cid, text):
    return (f'<w:commentRangeStart w:id="{cid}"/><w:r><w:t>{text}</w:t></w:r>'
            f'<w:commentRangeEnd w:id="{cid}"/>')


def test_a_comment_takes_its_live_range_not_the_snapshot():
    from gdoc_sync.comments import fetch_comments
    body = ("<w:p><w:r><w:t xml:space=\"preserve\">Then </w:t></w:r>"
            + _ranged(1, "I handled the event pages, invitations, and logistics.") + "</w:p>")
    drive = _FakeDrive(
        [_drive_comment("Matt", "can be more detailed",
                        "I handled the event pages, the invitations, and the logistics.")],
        _docx(body, _note(1, "Matt", "can be more detailed")))
    comments = fetch_comments(drive, "doc")
    md = "Then I handled the event pages, invitations, and logistics.\n"
    out = embed_comments(md, comments)
    assert out == ("Then {==I handled the event pages, invitations, and logistics.==}"
                   "{>>Matt: can be more detailed<<}\n")


def test_a_comment_with_no_live_range_is_orphaned_even_if_its_words_remain():
    from gdoc_sync.comments import ORPHAN_KEY, fetch_comments
    body = "<w:p><w:r><w:t>A sentence. Another one.</w:t></w:r></w:p>"
    drive = _FakeDrive([_drive_comment("Aris", "why?", "A sentence.")],
                       _docx(body, ""))
    (comment,) = fetch_comments(drive, "doc")
    assert comment[ORPHAN_KEY]
    out = embed_comments("A sentence. Another one.\n", [comment])
    assert out.startswith("A sentence. Another one.\n")
    assert "<!-- orphaned comment, was on: “A sentence.” -->{>>Aris: why?<<}" in out


def test_identical_comments_each_get_their_own_range():
    from gdoc_sync.comments import fetch_comments
    body = ("<w:p>" + _ranged(1, "first long passage here") + "</w:p>"
            "<w:p>" + _ranged(2, "second long passage there") + "</w:p>")
    notes = _note(1, "Matt", "too long") + _note(2, "Matt", "too long")
    drive = _FakeDrive([_drive_comment("Matt", "too long", "second long passage there"),
                        _drive_comment("Matt", "too long", "first long passage here")],
                       _docx(body, notes))
    quotes = [c["quotedFileContent"]["value"] for c in fetch_comments(drive, "doc")]
    assert quotes == ["second long passage there", "first long passage here"]


def test_a_failed_export_keeps_the_snapshots():
    from gdoc_sync.comments import ORPHAN_KEY, fetch_comments
    drive = _FakeDrive([_drive_comment("Ada", "x", "It wasn&#39;t there")],
                       RuntimeError("export failed"))
    (comment,) = fetch_comments(drive, "doc")
    assert comment["quotedFileContent"]["value"] == "It wasn't there"
    assert ORPHAN_KEY not in comment


def test_literal_footnote_reference_in_the_doc_still_matches_across_paragraphs():
    md = "Opening line that is long enough.[^7]\n\nSecond paragraph goes on.\n\n[^7]: note\n"
    quote = "Opening line that is long enough.[^7]\nSecond paragraph goes on."
    out = embed_comments(md, [_comment(quote, "Ada", "x")])
    assert out.startswith("{==Opening line that is long enough.[^7]==}\n\n"
                          "{==Second paragraph goes on.==}{>>Ada: x<<}")
