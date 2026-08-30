"""Canonical generated-artifact schema identities.

The public format deliberately has no legacy manifest reader or compatibility
mode. Every generated boundary imports its identity from this module so schema
changes are explicit, reviewable, and cannot drift independently between
frontends.
"""

SEMANTIC_MANIFEST_SCHEMA_VERSION = "1.0"
SEMANTIC_MANIFEST_KIND = "supernote_module_semantic_manifest"

JVM_SOURCE_MANIFEST_SCHEMA_VERSION = "1.0"
JVM_SOURCE_MANIFEST_KIND = "supernote_module_jvm_source_manifest"

FEATURE_MANIFEST_SCHEMA_VERSION = "1.0"
FEATURE_MANIFEST_KIND = "supernote_module_feature"

PLUGIN_REGISTRY_SCHEMA_VERSION = "1.0"
PLUGIN_REGISTRY_KIND = "supernote_module_plugin_runtime_registry"

GENERATED_OWNERSHIP_SCHEMA_VERSION = "1.0"
GENERATED_OWNERSHIP_KIND = "supernote_module_plugin_runtime_ownership"
