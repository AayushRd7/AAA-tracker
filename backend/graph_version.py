"""Single source of truth for the Meta Graph API version.

Meta's Graph API is date-versioned and each version is supported for roughly two years, after
which calls against it start failing. The version used to be hardcoded in two places (the
cost-sync defaults and the OAuth flow), so it drifted: the console showed a newer version while
the code still called an ageing one.

One default, overridable by ``META_GRAPH_VERSION`` in the environment, and the per-tenant
``meta_ads.api_version`` setting still wins when an operator sets it explicitly.
"""
import os

DEFAULT_GRAPH_VERSION = (os.environ.get("META_GRAPH_VERSION") or "v26.0").strip()
