"""Bounded removal of retained session files without following symbolic links."""
import os
import stat

from .errors import SessionError


def remove_session_files(data_dir, session_id):
    # IDs are opaque database identities, never request-supplied relative paths.
    if session_id in ('.', '..') or '/' in session_id or '\\' in session_id:
        raise SessionError('validation')
    budget = [10000]

    def remove(parent, name, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 64:
            raise SessionError('purge_cleanup_limit')
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                for child in os.listdir(fd):
                    remove(fd, child, depth + 1)
            finally:
                os.close(fd)
            os.rmdir(name, dir_fd=parent)
        else:
            os.unlink(name, dir_fd=parent)

    root = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            sessions = os.open('sessions', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        except FileNotFoundError:
            return
        try:
            remove(sessions, session_id)
        finally:
            os.close(sessions)
    finally:
        os.close(root)
