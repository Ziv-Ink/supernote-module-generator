"""Canonical V3 generated-artifact schema identities.

V3 deliberately has no V2 manifest reader or compatibility mode.  Every
generated boundary imports its identity from this module so schema changes are
explicit, reviewable, and cannot drift independently between frontends.
"""

SEMANTIC_MANIFEST_SCHEMA_VERSION = 3
SEMANTIC_MANIFEST_KIND = "supernote_v3_semantic_manifest"

JVM_SOURCE_MANIFEST_SCHEMA_VERSION = 3
JVM_SOURCE_MANIFEST_KIND = "supernote_v3_jvm_source_manifest"

FEATURE_MANIFEST_SCHEMA_VERSION = 3
FEATURE_MANIFEST_KIND = "supernote_v3_feature"

PLUGIN_REGISTRY_SCHEMA_VERSION = 2
PLUGIN_REGISTRY_KIND = "supernote_v3_plugin_runtime_registry"

GENERATED_OWNERSHIP_SCHEMA_VERSION = 2
GENERATED_OWNERSHIP_KIND = "supernote_v3_plugin_runtime_ownership"
