"""The error contract shared by every castiron source adapter.

These live at the ``sources`` level rather than inside ``sources/openapi`` so the live-DB
(CI-010) and migrations (CI-020) sources reuse them. The split matters to callers: a
:class:`SourceFetchError` means *castiron never got a document* (network, auth, non-JSON
body), whereas a :class:`SourceParseError` means *the document arrived but is not a schema
castiron can read*. Only the first is worth retrying.
"""


class SourceError(Exception):
    """Base class for every failure raised by a castiron source adapter."""


class SourceFetchError(SourceError):
    """The source document could not be retrieved or was not JSON."""


class SourceParseError(SourceError):
    """The source document was retrieved but is not a schema castiron can read."""
