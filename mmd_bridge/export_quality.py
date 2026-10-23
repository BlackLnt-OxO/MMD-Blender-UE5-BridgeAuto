"""Nonfatal output diagnostics, distinct from unreadable files and import errors."""


class QualityMismatch(ValueError):
    """The file can be consumed, but its contents need human review."""


def check_quality(condition, message, issues, policy='strict'):
    if condition:
        return
    if policy != 'review':
        raise QualityMismatch(message)
    if message not in issues:
        issues.append(message)
