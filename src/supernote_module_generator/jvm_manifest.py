"""Versioned deterministic internal compiler interface emitted by KSP."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .semantic import SourceProvenance
from .v3_schemas import (
    JVM_SOURCE_MANIFEST_KIND as JVM_MANIFEST_KIND,
    JVM_SOURCE_MANIFEST_SCHEMA_VERSION as JVM_MANIFEST_SCHEMA_VERSION,
)
from .source_models import (
    DeclarationTarget,
    JvmConstructorSource,
    JvmDeclarationSource,
    JvmInjectedDependency,
    JvmFieldSource,
    JvmLanguage,
    JvmOwnerForm,
    JvmOwnerSource,
    JvmParameterSource,
    JvmTypeSource,
    MarkerOccurrence,
    SourceIntent,
    SupernoteMarker,
)


class JvmManifestError(ValueError):
    pass


@dataclass(frozen=True)
class JvmSourceManifest:
    feature_id: str
    frontend_version: str
    owners: tuple[JvmOwnerSource, ...]
    schema_version: int = JVM_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != JVM_MANIFEST_SCHEMA_VERSION:
            raise JvmManifestError(
                f"incompatible JVM manifest schema {self.schema_version}; "
                f"expected {JVM_MANIFEST_SCHEMA_VERSION}"
            )
        if not self.feature_id.startswith("supernote:feature:"):
            raise JvmManifestError("JVM manifest feature identity is invalid")
        if not self.frontend_version:
            raise JvmManifestError("JVM frontend version cannot be empty")
        identities = [owner.provenance.declaration_id for owner in self.owners]
        if len(identities) != len(set(identities)):
            raise JvmManifestError("duplicate JVM owner declaration identity")
        for owner in self.owners:
            if owner.provenance.declaration_id != jvm_owner_identity(owner.owner_class):
                raise JvmManifestError("JVM owner identity is not deterministic")
            for constructor in owner.constructors:
                expected = jvm_declaration_identity(
                    owner.owner_class, "<init>", constructor.jvm_descriptor
                )
                _validate_identity(constructor.provenance, constructor.adapter_identity, expected)
            for declaration in owner.declarations:
                expected = jvm_declaration_identity(
                    owner.owner_class,
                    declaration.jvm_name,
                    declaration.jvm_descriptor,
                )
                _validate_identity(declaration.provenance, declaration.adapter_identity, expected)
            for source_field in owner.fields:
                expected = jvm_field_identity(owner.owner_class, source_field.name)
                if source_field.provenance.declaration_id != expected:
                    raise JvmManifestError("JVM field identity is not deterministic")
                if source_field.accessor_identity != jvm_field_accessor_identity(expected):
                    raise JvmManifestError("JVM field accessor identity is not deterministic")

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": JVM_MANIFEST_KIND,
            "feature_id": self.feature_id,
            "frontend_version": self.frontend_version,
            "owners": [
                _owner_manifest(owner)
                for owner in sorted(
                    self.owners, key=lambda item: item.provenance.declaration_id
                )
            ],
        }

    def json(self) -> str:
        return json.dumps(self.manifest(), indent=2, sort_keys=True) + "\n"


def read_jvm_manifest(
    path: Path,
    *,
    expected_feature_id: str | None = None,
) -> JvmSourceManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JvmManifestError(f"{path}: JVM manifest could not be read: {exc}") from exc
    try:
        value = _object(raw, "manifest")
        _keys(
            value,
            {"schema_version", "kind", "feature_id", "frontend_version", "owners"},
            "manifest",
        )
        schema = _integer(value["schema_version"], "schema_version")
        if schema != JVM_MANIFEST_SCHEMA_VERSION:
            raise JvmManifestError(
                f"incompatible JVM manifest schema {schema}; "
                f"expected {JVM_MANIFEST_SCHEMA_VERSION}"
            )
        if _string(value["kind"], "kind") != JVM_MANIFEST_KIND:
            raise JvmManifestError("JVM manifest kind is invalid")
        feature_id = _string(value["feature_id"], "feature_id")
        if expected_feature_id is not None and feature_id != expected_feature_id:
            raise JvmManifestError(
                f"JVM manifest belongs to {feature_id!r}, expected "
                f"{expected_feature_id!r}"
            )
        owners_raw = _list(value["owners"], "owners")
        return JvmSourceManifest(
            feature_id=feature_id,
            frontend_version=_string(value["frontend_version"], "frontend_version"),
            owners=tuple(_parse_owner(item, index) for index, item in enumerate(owners_raw)),
            schema_version=schema,
        )
    except JvmManifestError as exc:
        if str(exc).startswith(str(path)):
            raise
        raise JvmManifestError(f"{path}: {exc}") from exc


def write_jvm_manifest(path: Path, manifest: JvmSourceManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(manifest.json())


def jvm_owner_identity(owner_class: str) -> str:
    return f"jvm:{owner_class}"


def jvm_declaration_identity(owner_class: str, name: str, descriptor: str) -> str:
    return f"jvm:{owner_class}#{name}{descriptor}"


def jvm_adapter_identity(declaration_id: str) -> str:
    digest = hashlib.sha256(declaration_id.encode("utf-8")).hexdigest()[:20]
    return f"supernote.jvm.adapter.{digest}"


def jvm_field_identity(owner_class: str, name: str) -> str:
    return f"jvm:{owner_class}#field:{name}"


def jvm_field_accessor_identity(declaration_id: str) -> str:
    digest = hashlib.sha256(declaration_id.encode("utf-8")).hexdigest()[:20]
    return f"supernote.jvm.field.{digest}"


def _owner_manifest(owner: JvmOwnerSource) -> dict[str, object]:
    return {
        "source": owner.provenance.manifest(),
        "language": owner.language.value,
        "owner_class": owner.owner_class,
        "source_name": owner.source_name,
        "form": owner.form.value,
        "visibility": owner.visibility,
        "markers": _intent_manifest(owner.intent),
        "constructors": [
            _constructor_manifest(item)
            for item in sorted(
                owner.constructors, key=lambda value: value.provenance.declaration_id
            )
        ],
        "declarations": [
            _declaration_manifest(item)
            for item in sorted(
                owner.declarations, key=lambda value: value.provenance.declaration_id
            )
        ],
        "fields": [
            _field_manifest(item)
            for item in owner.fields
        ],
        "enum_constants": list(owner.enum_constants),
        "is_data": owner.is_data,
        "is_record": owner.is_record,
        "is_final": owner.is_final,
        "type_parameter_count": owner.type_parameter_count,
        "supertypes": list(owner.supertypes),
    }


def _constructor_manifest(source: JvmConstructorSource) -> dict[str, object]:
    return {
        "source": source.provenance.manifest(),
        "jvm_descriptor": source.jvm_descriptor,
        "parameters": [_parameter_manifest(item) for item in source.parameters],
        "visibility": source.visibility,
        "markers": _intent_manifest(source.intent),
        "adapter_identity": source.adapter_identity,
    }


def _declaration_manifest(source: JvmDeclarationSource) -> dict[str, object]:
    return {
        "source": source.provenance.manifest(),
        "owner_declaration_id": source.owner_declaration_id,
        "owner_class": source.owner_class,
        "jvm_name": source.jvm_name,
        "jvm_descriptor": source.jvm_descriptor,
        "parameters": [_parameter_manifest(item) for item in source.parameters],
        "result_jvm_type": source.result_jvm_type,
        "result_nullable": source.result_nullable,
        "markers": _intent_manifest(source.intent),
        "visibility": source.visibility,
        "adapter_identity": source.adapter_identity,
        "language": source.language.value,
        "is_suspend": source.is_suspend,
        "is_static": source.is_static,
        "result_type_arguments": [
            _type_manifest(item) for item in source.result_type_arguments
        ],
    }


def _field_manifest(source: JvmFieldSource) -> dict[str, object]:
    return {
        "source": source.provenance.manifest(),
        "owner_declaration_id": source.owner_declaration_id,
        "name": source.name,
        "type": _type_manifest(source.type),
        "markers": _intent_manifest(source.intent),
        "visibility": source.visibility,
        "mutable": source.mutable,
        "is_static": source.is_static,
        "accessor_identity": source.accessor_identity,
    }


def _parameter_manifest(source: JvmParameterSource) -> dict[str, object]:
    return {
        "jvm_type": source.jvm_type,
        "name": source.name,
        "nullable": source.nullable,
        "injected": source.injected.value if source.injected is not None else None,
        "type_arguments": [_type_manifest(item) for item in source.type_arguments],
    }


def _type_manifest(source: JvmTypeSource) -> dict[str, object]:
    return {
        "jvm_type": source.jvm_type,
        "nullable": source.nullable,
        "arguments": [_type_manifest(item) for item in source.arguments],
    }


def _intent_manifest(intent: SourceIntent) -> list[dict[str, object]]:
    return [
        {
            "name": item.marker.value,
            "line": item.line,
            "column": item.column,
        }
        for item in intent.occurrences
    ]


def _parse_owner(raw: Any, index: int) -> JvmOwnerSource:
    label = f"owners[{index}]"
    value = _object(raw, label)
    _keys(
        value,
        {
            "source", "language", "owner_class", "source_name", "form",
            "markers", "constructors", "declarations", "visibility", "fields",
            "enum_constants", "is_data", "is_record", "is_final",
            "type_parameter_count", "supertypes",
        },
        label,
    )
    language = _enum(JvmLanguage, value["language"], f"{label}.language")
    owner_class = _string(value["owner_class"], f"{label}.owner_class")
    provenance = _parse_provenance(value["source"], f"{label}.source")
    if provenance.declaration_id != jvm_owner_identity(owner_class):
        raise JvmManifestError(
            f"{label}.source declaration identity is not the deterministic JVM owner identity"
        )
    if provenance.language != language.value:
        raise JvmManifestError(f"{label}.source language does not match owner language")
    owner_id = provenance.declaration_id
    owner_intent = _parse_intent(
        value["markers"], DeclarationTarget.CLASS, f"{label}.markers"
    )
    declaration_target = (
        DeclarationTarget.FUNCTION
        if not owner_intent.markers
        else DeclarationTarget.METHOD
    )
    return JvmOwnerSource(
        provenance=provenance,
        language=language,
        owner_class=owner_class,
        source_name=_string(value["source_name"], f"{label}.source_name"),
        form=_enum(JvmOwnerForm, value["form"], f"{label}.form"),
        intent=owner_intent,
        constructors=tuple(
            _parse_constructor(item, language, owner_class, f"{label}.constructors[{item_index}]")
            for item_index, item in enumerate(_list(value["constructors"], f"{label}.constructors"))
        ),
        declarations=tuple(
            _parse_declaration(
                item,
                language,
                owner_class,
                owner_id,
                declaration_target,
                f"{label}.declarations[{item_index}]",
            )
            for item_index, item in enumerate(_list(value["declarations"], f"{label}.declarations"))
        ),
        visibility=_string(value["visibility"], f"{label}.visibility"),
        fields=tuple(
            _parse_field(item, owner_id, f"{label}.fields[{item_index}]")
            for item_index, item in enumerate(
                _list(value["fields"], f"{label}.fields")
            )
        ),
        enum_constants=tuple(
            _string(item, f"{label}.enum_constants[{item_index}]")
            for item_index, item in enumerate(
                _list(value["enum_constants"], f"{label}.enum_constants")
            )
        ),
        is_data=_bool(value["is_data"], f"{label}.is_data"),
        is_record=_bool(value["is_record"], f"{label}.is_record"),
        is_final=_bool(value["is_final"], f"{label}.is_final"),
        type_parameter_count=_integer(
            value["type_parameter_count"], f"{label}.type_parameter_count"
        ),
        supertypes=tuple(
            _string(item, f"{label}.supertypes[{item_index}]")
            for item_index, item in enumerate(
                _list(value["supertypes"], f"{label}.supertypes")
            )
        ),
    )


def _parse_constructor(
    raw: Any, language: JvmLanguage, owner_class: str, label: str
) -> JvmConstructorSource:
    value = _object(raw, label)
    _keys(value, {"source", "jvm_descriptor", "parameters", "visibility", "markers", "adapter_identity"}, label)
    descriptor = _string(value["jvm_descriptor"], f"{label}.jvm_descriptor")
    provenance = _parse_provenance(value["source"], f"{label}.source")
    expected = jvm_declaration_identity(owner_class, "<init>", descriptor)
    _verify_declaration_identity(value, provenance, expected, label)
    return JvmConstructorSource(
        provenance=provenance,
        jvm_descriptor=descriptor,
        parameters=tuple(
            _parse_parameter(item, f"{label}.parameters[{index}]")
            for index, item in enumerate(_list(value["parameters"], f"{label}.parameters"))
        ),
        visibility=_string(value["visibility"], f"{label}.visibility"),
        intent=_parse_intent(value["markers"], DeclarationTarget.CONSTRUCTOR, f"{label}.markers"),
        adapter_identity=_string(value["adapter_identity"], f"{label}.adapter_identity"),
    )


def _parse_declaration(
    raw: Any,
    language: JvmLanguage,
    owner_class: str,
    owner_id: str,
    target: DeclarationTarget,
    label: str,
) -> JvmDeclarationSource:
    value = _object(raw, label)
    _keys(
        value,
        {
            "source", "owner_declaration_id", "owner_class", "jvm_name",
            "jvm_descriptor", "parameters", "result_jvm_type",
            "result_nullable", "markers", "visibility", "adapter_identity",
            "language", "is_suspend", "is_static",
            "result_type_arguments",
        },
        label,
    )
    manifest_language = _enum(JvmLanguage, value["language"], f"{label}.language")
    if manifest_language is not language:
        raise JvmManifestError(f"{label}.language does not match owner")
    if _string(value["owner_class"], f"{label}.owner_class") != owner_class:
        raise JvmManifestError(f"{label}.owner_class does not match owner")
    if _string(value["owner_declaration_id"], f"{label}.owner_declaration_id") != owner_id:
        raise JvmManifestError(f"{label}.owner_declaration_id does not match owner")
    name = _string(value["jvm_name"], f"{label}.jvm_name")
    descriptor = _string(value["jvm_descriptor"], f"{label}.jvm_descriptor")
    provenance = _parse_provenance(value["source"], f"{label}.source")
    expected = jvm_declaration_identity(owner_class, name, descriptor)
    _verify_declaration_identity(value, provenance, expected, label)
    return JvmDeclarationSource(
        provenance=provenance,
        owner_declaration_id=owner_id,
        owner_class=owner_class,
        jvm_name=name,
        jvm_descriptor=descriptor,
        parameters=tuple(
            _parse_parameter(item, f"{label}.parameters[{index}]")
            for index, item in enumerate(_list(value["parameters"], f"{label}.parameters"))
        ),
        result_jvm_type=_string(value["result_jvm_type"], f"{label}.result_jvm_type"),
        result_nullable=_bool(value["result_nullable"], f"{label}.result_nullable"),
        intent=_parse_intent(value["markers"], target, f"{label}.markers"),
        visibility=_string(value["visibility"], f"{label}.visibility"),
        adapter_identity=_string(value["adapter_identity"], f"{label}.adapter_identity"),
        language=language,
        is_suspend=_bool(value["is_suspend"], f"{label}.is_suspend"),
        is_static=_bool(value["is_static"], f"{label}.is_static"),
        result_type_arguments=tuple(
            _parse_type(item, f"{label}.result_type_arguments[{index}]")
            for index, item in enumerate(
                _list(
                    value["result_type_arguments"],
                    f"{label}.result_type_arguments",
                )
            )
        ),
    )


def _verify_declaration_identity(
    value: dict[str, Any], provenance: SourceProvenance, expected: str, label: str
) -> None:
    if provenance.declaration_id != expected:
        raise JvmManifestError(f"{label}.source declaration identity is not deterministic")
    adapter = _string(value["adapter_identity"], f"{label}.adapter_identity")
    if adapter != jvm_adapter_identity(expected):
        raise JvmManifestError(f"{label}.adapter_identity is not deterministic")


def _validate_identity(
    provenance: SourceProvenance, adapter_identity: str, expected: str
) -> None:
    if provenance.declaration_id != expected:
        raise JvmManifestError("JVM declaration identity is not deterministic")
    if adapter_identity != jvm_adapter_identity(expected):
        raise JvmManifestError("JVM adapter identity is not deterministic")


def _parse_parameter(raw: Any, label: str) -> JvmParameterSource:
    value = _object(raw, label)
    _keys(value, {"jvm_type", "name", "nullable", "injected", "type_arguments"}, label)
    injected_raw = value["injected"]
    injected = None if injected_raw is None else _enum(JvmInjectedDependency, injected_raw, f"{label}.injected")
    return JvmParameterSource(
        jvm_type=_string(value["jvm_type"], f"{label}.jvm_type"),
        name=_string(value["name"], f"{label}.name"),
        nullable=_bool(value["nullable"], f"{label}.nullable"),
        injected=injected,
        type_arguments=tuple(
            _parse_type(item, f"{label}.type_arguments[{index}]")
            for index, item in enumerate(
                _list(value["type_arguments"], f"{label}.type_arguments")
            )
        ),
    )


def _parse_type(raw: Any, label: str) -> JvmTypeSource:
    value = _object(raw, label)
    _keys(value, {"jvm_type", "nullable", "arguments"}, label)
    return JvmTypeSource(
        _string(value["jvm_type"], f"{label}.jvm_type"),
        _bool(value["nullable"], f"{label}.nullable"),
        tuple(
            _parse_type(item, f"{label}.arguments[{index}]")
            for index, item in enumerate(
                _list(value["arguments"], f"{label}.arguments")
            )
        ),
    )


def _parse_field(raw: Any, owner_id: str, label: str) -> JvmFieldSource:
    value = _object(raw, label)
    _keys(
        value,
        {
            "source", "owner_declaration_id", "name", "type", "markers",
            "visibility", "mutable", "is_static", "accessor_identity",
        },
        label,
    )
    if _string(value["owner_declaration_id"], f"{label}.owner_declaration_id") != owner_id:
        raise JvmManifestError(f"{label}.owner_declaration_id does not match owner")
    return JvmFieldSource(
        _parse_provenance(value["source"], f"{label}.source"),
        owner_id,
        _string(value["name"], f"{label}.name"),
        _parse_type(value["type"], f"{label}.type"),
        _parse_intent(value["markers"], DeclarationTarget.FIELD, f"{label}.markers"),
        _string(value["visibility"], f"{label}.visibility"),
        _bool(value["mutable"], f"{label}.mutable"),
        _bool(value["is_static"], f"{label}.is_static"),
        _string(value["accessor_identity"], f"{label}.accessor_identity"),
    )


def _parse_intent(raw: Any, target: DeclarationTarget, label: str) -> SourceIntent:
    occurrences = []
    for index, item in enumerate(_list(raw, label)):
        item_label = f"{label}[{index}]"
        value = _object(item, item_label)
        _keys(value, {"name", "line", "column"}, item_label)
        occurrences.append(
            MarkerOccurrence(
                _enum(SupernoteMarker, value["name"], f"{item_label}.name"),
                _integer(value["line"], f"{item_label}.line"),
                _integer(value["column"], f"{item_label}.column"),
            )
        )
    try:
        return SourceIntent(target, tuple(occurrences))
    except ValueError as exc:
        raise JvmManifestError(f"{label}: {exc}") from exc


def _parse_provenance(raw: Any, label: str) -> SourceProvenance:
    value = _object(raw, label)
    _keys(value, {"declaration_id", "language", "path", "line", "column"}, label)
    try:
        return SourceProvenance(
            _string(value["declaration_id"], f"{label}.declaration_id"),
            _string(value["language"], f"{label}.language"),
            _string(value["path"], f"{label}.path"),
            _integer(value["line"], f"{label}.line"),
            _integer(value["column"], f"{label}.column"),
        )
    except ValueError as exc:
        raise JvmManifestError(f"{label}: {exc}") from exc


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unknown " + ", ".join(extra))
        raise JvmManifestError(f"{label} has invalid fields: {'; '.join(detail)}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JvmManifestError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise JvmManifestError(f"{label} must be an array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise JvmManifestError(f"{label} must be a non-empty string")
    return value


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise JvmManifestError(f"{label} must be an integer")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise JvmManifestError(f"{label} must be a boolean")
    return value


def _enum(enum_type: type, value: Any, label: str):
    text = _string(value, label)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise JvmManifestError(f"{label} has unsupported value {text!r}") from exc
