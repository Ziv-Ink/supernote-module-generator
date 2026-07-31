from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re

METADATA_FILE = ".supernote-module.json"
NATIVE_GENERATOR_METADATA_FILE = ".supernote-native-module.json"
LEGACY_METADATA_FILE = ".rn-legacy-module.json"
METADATA_FILES = (
    METADATA_FILE,
    NATIVE_GENERATOR_METADATA_FILE,
    LEGACY_METADATA_FILE,
)
BACKENDS = ("kotlin", "jni", "jsi")
LEGACY_BACKEND_ALIASES = {"cpp": "jni"}
PUBLIC_TYPES = ("native", "jni", "jsi")
TYPE_TO_BACKEND = {"native": "kotlin", "jni": "jni", "jsi": "jsi"}
BACKEND_TO_TYPE = {value: key for key, value in TYPE_TO_BACKEND.items()}
TYPE_LABELS = {
    "native": "Native Module",
    "jni": "Native JNI Module",
    "jsi": "JSI Module",
}


def normalize_backend(value: str) -> str:
    """Return the canonical backend identifier used by new metadata."""
    return LEGACY_BACKEND_ALIASES.get(value, value)


def backend_for_type(value: str) -> str:
    return TYPE_TO_BACKEND[value]


def public_type(value: str) -> str:
    return BACKEND_TO_TYPE[normalize_backend(value)]


def gradle_project_name(npm_name: str) -> str:
    """Return a stable Gradle-safe project leaf from an npm package name."""
    return re.sub(r"[^A-Za-z0-9_]+", "-", npm_name.removeprefix("@").replace("/", "-"))


def native_class_prefix(npm_name: str) -> str:
    """Return a deterministic PascalCase prefix for generated Android classes."""
    name = npm_name.rsplit("/", 1)[-1]
    for prefix in ("react-native-", "local-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.endswith("-plugin"):
        name = name[: -len("-plugin")]
    words = [part for part in re.split(r"[^A-Za-z0-9]+", name) if part]
    result = (
        "".join(part[:1].upper() + part[1:] for part in words)
        or "LocalModule"
    )
    return f"Module{result}" if result[0].isdigit() else result


def native_library_name(npm_name: str) -> str:
    base = re.sub(
        r"[^a-z0-9_]+",
        "_",
        npm_name.lower().removeprefix("@").replace("/", "_"),
    ).strip("_")
    digest = hashlib.sha256(npm_name.encode("utf-8")).hexdigest()[:8]
    return f"sn_{base or 'module'}_{digest}"


def jsi_global_name(npm_name: str) -> str:
    base = re.sub(
        r"[^A-Za-z0-9_]+",
        "_",
        npm_name.removeprefix("@").replace("/", "_"),
    ).strip("_")
    digest = hashlib.sha256(npm_name.encode("utf-8")).hexdigest()[:8]
    return f"__supernoteJsi_{base or 'module'}_{digest}"


@dataclass(frozen=True)
class ProjectConfig:
    output: Path
    npm_name: str
    package_version: str
    android_namespace: str
    module_name: str
    backend: str = "kotlin"
    native_library_name: str | None = None
    jsi_global_name: str | None = None
    description: str = "Local native code module"
    min_sdk: int = 27
    force: bool = False
    toolchain_versions: dict[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", normalize_backend(self.backend))

    def metadata(self) -> dict[str, object]:
        result = asdict(self)
        # Output location and overwrite permission are invocation state, not
        # portable project metadata. Older metadata containing them remains
        # readable because callers replace both values when loading a module.
        result.pop("output")
        result.pop("force")
        return result
