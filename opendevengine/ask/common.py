"""Bounded validation and public persistence helpers for Ask."""
import base64
import json
from opendevengine.platform.errors import NotFound


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def text(value, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum or '\x00' in value:
        raise ValueError('Invalid text.')
    return value


def ids(value):
    if not isinstance(value, (list, tuple)) or len(value) > 50 or any(not isinstance(x, str) or not x or len(x)>200 for x in value) or len(set(value)) != len(value):
        raise ValueError('Invalid scope.')
    return list(value)


def one(db, table, ident):
    row = db.execute('SELECT * FROM '+table+' WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise NotFound('Record unavailable.')
    return dict(row)


def page(db, table, limit, cursor, where='1=1', params=()):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid limit.')
    if cursor is not None:
        if not isinstance(cursor,str) or len(cursor)>1024:
            raise ValueError('Invalid cursor.')
        try:
            stamp, ident = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
            if type(stamp) is not int or not isinstance(ident,str):
                raise ValueError()
        except Exception:
            raise ValueError('Invalid cursor.') from None
        where += ' AND (created_at,id)>(?,?)'
        params = (*params, stamp, ident)
    rows = [dict(r) for r in db.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY created_at,id LIMIT ?', (*params,limit+1))]
    next_cursor = None
    if len(rows)>limit:
        rows=rows[:limit]
        next_cursor=base64.urlsafe_b64encode(encode([rows[-1]['created_at'],rows[-1]['id']]).encode()).decode()
    return rows,next_cursor


def eligible(index, ident):
    try:
        state=index.get_state(ident)
        return state if state['eligible'] else None
    except Exception:
        return None


def citation(index, item):
    keys={'repository_id','snapshot_id','revision','path','start_line','end_line'}
    if not isinstance(item,dict) or not keys <= item.keys() or item.keys()-keys-{'id'}:
        raise ValueError('Invalid citation.')
    result=index.source(**{k:item[k] for k in keys-{'revision'}},context_lines=0)
    if result['revision'] != item['revision']:
        raise ValueError('Citation revision mismatch.')
    return {k:item[k] for k in keys}


def export_text(value):
    """Plain-text export: preserve identifiers while suppressing recognizable secrets/host paths."""
    import re
    from opendevengine.platform.credentials import redact_credentials
    value=redact_credentials(str(value), '[redacted credential]')
    value=re.sub(r'(?i)\b(?:authorization|password|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s,;]+','[redacted credential]',value)
    value=re.sub(r'(?:/Users/|/home/|/private/|[A-Z]:\\Users\\)[^\s"<>]+','[private host path]',value)
    return value


def retrieval_query(prompt):
    """A byte-bounded query independent of the complete persisted/model request."""
    text(prompt)
    return prompt.encode('utf-8')[:3000].decode('utf-8', errors='ignore')
