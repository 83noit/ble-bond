# ble-bond

The Bluetooth LE link code shared by grundfos-ble and mira-ble: bonding in a button window,
recording which adapter or proxy each attempt used, the BlueZ pairing agent, and one request
at a time with replies matched by the caller. Protocol framing and device models stay in the
device libraries.

## Attribution and identity

This is a public repository, so the global "Public repositories" rules apply in full:

- No model signature anywhere: no `#claude-…` hashtag or other model name, no `Co-Authored-By`,
  no Claude Code attribution in commits, tags, releases or pull requests.
- Author, committer and tagger are the GitHub identity from `~/.config/git/user.github.inc`
  (already in this repo's jj config); never a real name or personal email, in commits or files.

## Compatibility

Released as 1.x: every 1.x release stays backward compatible, because grundfos-ble and
mira-ble can be installed in the same Home Assistant and share one copy of this package.
Deprecate rather than remove; a break needs 2.0 and a release of both device libraries.

## Notes

Project knowledge lives in `notes/_agents/` (start at `index.md`). `notes` is an untracked
symlink to `~/projects/notes/library/ble-bond/`. The design is
`~/projects/notes/library/_agents/ble-shared-link-design.md`.
