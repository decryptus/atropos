"""Private, bounded regular files for rule service and client secrets."""
import os
import stat


def read_private(path, limit):
    if os.getuid() != os.geteuid():
        raise ValueError('invalid_private_file')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in (0, os.getuid())
                or stat.S_IMODE(metadata.st_mode) & 0o077):
            raise ValueError('invalid_private_file')
        content = stream.read(limit + 1)
    if not 0 < len(content) <= limit:
        raise ValueError('invalid_private_file')
    return content
