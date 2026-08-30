"""Canonical generated-artifact schema identities.

The public format deliberately has no legacy manifest reader or compatibility
mode. Every generated boundary imports its identity from this module so schema
changes are explicit, reviewable, and cannot drift independently between
frontends.
"""

SEMANTIC_MANIFEST_SCHEMA_VERSION = 3
SEMANTIC_MANIFEST_KIND = "supernote_v4_semantic_manifest"

JVM_SOURCE_MANIFEST_SCHEMA_VERSION = 3
JVM_SOURCE_MANIFEST_KIND = "supernote_v4_jvm_source_manifest"

FEATURE_MANIFEST_SCHEMA_VERSION = 3
FEATURE_MANIFEST_KIND = "supernote_v4_feature"

PLUGIN_REGISTRY_SCHEMA_VERSION = 2
PLUGIN_REGISTRY_KIND = "supernote_v4_plugin_runtime_registry"

GENERATED_OWNERSHIP_SCHEMA_VERSION = 2
GENERATED_OWNERSHIP_KIND = "supernote_v4_plugin_runtime_ownership"
