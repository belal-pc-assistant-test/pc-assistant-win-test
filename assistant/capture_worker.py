"""Short-lived screen photo worker. Writes JPEG to stdout, never to a file."""
import sys
from .actions.core import capture_desktop_jpeg


def main() -> int:
    try:
        width, quality = int(sys.argv[1]), int(sys.argv[2])
        if not 1 <= width <= 2560 or not 1 <= quality <= 90:
            return 1
        sys.stdout.buffer.write(capture_desktop_jpeg(width, quality))
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
