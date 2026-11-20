"""Canonical recognizable credential formats for source guards and redaction.

Private-key headers are sufficient to reject a file; redaction consumes the block
through its footer (or end of input for incomplete blocks). Token boundaries avoid
matching credential-like suffixes inside ordinary identifiers.
"""
import re

_CREDENTIAL_RES = (
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)', re.S),
    # Keep the broad github_pat suffix accepted by existing plain-text exports.
    re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16}|(?<!\w)(?<![\w-]-)sk-[A-Za-z0-9_-]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})\b'),
)


def contains_credential(text: str) -> bool:
    """Return whether text contains a recognizable credential or private key."""
    return any(pattern.search(text) for pattern in _CREDENTIAL_RES)


def redact_credentials(text: str, replacement: str) -> str:
    """Replace recognized credentials using a literal caller-supplied marker."""
    for pattern in _CREDENTIAL_RES:
        text = pattern.sub(lambda match: replacement, text)
    return text
