from __future__ import annotations

SHARED_NAMESPACE = "general"

MEMORY_NAMESPACES = {
    SHARED_NAMESPACE: "Shared / general",
    "pj-digi": "PJ Digi",
    "jsv-fashion": "JSV Fashion",
    "andhrawala": "Andhrawala",
    "vsports": "VSports",
}

NAMESPACE_ROWS = tuple(
    {
        "slug": slug,
        "display_name": display_name,
        "is_shared": slug == SHARED_NAMESPACE,
    }
    for slug, display_name in MEMORY_NAMESPACES.items()
)
