"""Remote desktop session: approved view + mouse/keyboard control through a private link.

Modules:
    gate     one-time link secret, session cookie, lock-out and expiry
    wire     minimal HTTP request parsing and RFC 6455 WebSocket framing (stdlib only)
    stream   capture, JPEG encoding, latest-frame broker and adaptive quality
    inputs   strict input-event validation and OS input injection (Windows)
    tunnel   public HTTPS address via a Cloudflare quick tunnel or localhost.run
    banner   always-on-top red banner (separate process) with a Stop button and hotkey
    server   loopback HTTP + WebSocket server serving the single-page viewer
    session  the approved session lifecycle (start, watchdog, stop, teardown)
    runtime  the real Windows implementations wired into the session
"""
