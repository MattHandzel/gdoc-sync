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
