# GIF purpose: the README hero — the full loop in ~35s: markdown → styled
# Google Doc → editor comments → pull them back as CriticMarkup → answer
# them from the terminal (reply + resolve) with a push.
#
# This records the REAL CLI against the REAL Google API (a scratch config +
# a throwaway doc that gets trashed after). Rendering it needs an
# authenticated token; see demo/render.sh.

Output docs/assets/demo.gif

Require gdoc-sync

Set Shell "bash"
Set Theme "Catppuccin Mocha"
Set FontFamily "JetBrainsMono Nerd Font"
Set FontSize 17
Set Width 1280
Set Height 640
Set Padding 12
Set TypingSpeed 45ms

# Scratch environment comes in via render.sh (GDOC_SYNC_CONFIG, XDG_STATE_HOME,
# PATH with the built binary, and DEMO_DIR with draft.md seeded).
Hide
Type "cd $DEMO_DIR && clear"
Enter
Sleep 500ms
Show

# Beat 1 — the markdown we start from
Type "cat draft.md"
Enter
Sleep 2500ms

# Beat 2 — one command: styled Google Doc, shared for commenting
Type "gdoc-sync create draft.md"
Enter
Sleep 12s

# Beat 3 (hidden) — an "editor" drops two comments via the API
Hide
Type "bash $DEMO_DIR/seed-comments.sh && clear"
Enter
Sleep 6s
Show

# Beat 4 — pull brings the doc back WITH the comments, anchored in the prose
Type "gdoc-sync pull draft.md"
Enter
Sleep 8s
Type "grep -n '{>>' draft.md"
Enter
Sleep 4s

# Beat 5 (hidden) — answer them in the file: a reply marker + a resolve marker
Hide
Type "sed -i 's/wording here<<}/wording here<<}{>>reply: tightened, take another look<<}/; s/by Wednesday<<}/by Wednesday<<}{>>resolve: done<<}/' draft.md && clear"
Enter
Sleep 800ms
Show

# Beat 6 — push syncs content AND answers the comment threads
Type "gdoc-sync push draft.md"
Enter
Sleep 12s

# Beat 7 — closing frame
Type "gdoc-sync status"
Enter
Sleep 4s
