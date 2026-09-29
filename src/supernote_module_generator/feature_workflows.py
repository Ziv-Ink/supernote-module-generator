"""Public CLI decisions for language-neutral logical features."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from .arguments import ParsedArguments
from .errors import ConfigurationError, OperationCancelled, ValidationError
from .feature_model import StarterFamily
from .feature_operations import FeatureOperationService
from .interaction import (
    BackRequested,
    CancelRequested,
    InputClosed,
    Interaction,
    InterruptRequested,
    MenuItem,
)
from .naming import (
    infer_android_namespace,
    infer_javascript_name,
    normalize_description,
    strip_ascii,
    validate_android_namespace,
    validate_javascript_name,
    validate_package_name,
    validate_package_version,
)
STARTER_ITEMS = [
    MenuItem(
        "cpp",
        "C/C++ (native)",
        "Creates a C++ starter; C23 files can be added to the same native root.",
    ),
    MenuItem(
        "kotlin",
        "Kotlin/Java (JVM)",
        "Creates a Kotlin starter; Java files can be added to the same JVM root.",
    ),
]


@dataclass(frozen=True)
class FeatureAddDecisions:
    package_name: str
    starters: tuple[StarterFamily, ...]
    description: str
    public_name: str
    android_namespace: str
    package_version: str


@dataclass(frozen=True)
class FeatureUpdateDecisions:
    """Confirmation that the complete current project should be regenerated."""


@dataclass(frozen=True)
class FeatureValidateDecisions:
    package_names: tuple[str, ...]


class FeatureDecisionCollector:
    def __init__(
        self,
        root: Path,
        arguments: ParsedArguments,
        interaction: Interaction | None,
        *,
        launched_from_menu: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.args = arguments
        self.ui = interaction
        self.launched_from_menu = launched_from_menu
        self.features = FeatureOperationService(self.root)
        self.warnings: list[object] = []

    @property
    def interactive(self) -> bool:
        return self.ui is not None

    def add(self) -> FeatureAddDecisions:
        if not self.interactive:
            return self._add_noninteractive()
        assert self.ui is not None
        package = self._provided_identifier(self.args.positional)
        description = (
            normalize_description(self.args.value("description") or "")
            if self.args.value("description") is not None
            else None
        )
        public_name = self._provided_identifier(self.args.value("javascript_name"))
        namespace = self._provided_identifier(self.args.value("android_namespace"))
        version = self._provided_identifier(self.args.value("package_version"))
        selected = list(self.args.values_for("starter"))
        if self.args.has("yes"):
            selected = selected or ["cpp"]
            if description is None:
                description = ""
            if version is None:
                version = "0.1.0"
            if package is not None and public_name is None:
                try:
                    public_name = infer_javascript_name(package)
                except ValidationError:
                    pass
            if package is not None and namespace is None:
                try:
                    namespace = infer_android_namespace(package)
                except ValidationError:
                    pass
        if package is not None:
            validate_package_name(package)
        if public_name is not None:
            validate_javascript_name(public_name)
        if namespace is not None:
            validate_android_namespace(namespace)
        if version is not None:
            validate_package_version(version)
        self.ui.header("Add feature")
        fields = ["starters", "package", "description", "public", "namespace", "version"]
        index = 0
        revisit: str | None = None
        while index < len(fields):
            field = fields[index]
            try:
                if field == "starters" and (not selected or revisit == field):
                    selected = self.ui.multi_menu(
                        "Select starter code",
                        STARTER_ITEMS,
                        defaults=tuple(selected) or ("cpp",),
                        collapse_label="Starter code",
                    )
                elif field == "package" and (package is None or revisit == field):
                    previous_package = package
                    package = self.ui.text(
                        "Package name",
                        default=package,
                        validate=validate_package_name,
                        guidance=(
                            "Used as the local folder and npm or Yarn dependency name."
                        ),
                    )
                    if package != previous_package:
                        if "javascript_name" not in self.args.provided:
                            public_name = None
                        if "android_namespace" not in self.args.provided:
                            namespace = None
                elif field == "description" and (description is None or revisit == field):
                    description = self.ui.text(
                        "Description (optional)",
                        default=description or None,
                        optional=True,
                        normalize=normalize_description,
                    )
                elif field == "public" and (public_name is None or revisit == field):
                    assert package is not None
                    try:
                        default_public = infer_javascript_name(package)
                    except ValidationError:
                        default_public = None
                    public_name = self.ui.text(
                        "JavaScript feature name",
                        default=public_name or default_public,
                        validate=validate_javascript_name,
                    )
                elif field == "namespace" and (namespace is None or revisit == field):
                    assert package is not None
                    try:
                        default_namespace = infer_android_namespace(package)
                    except ValidationError:
                        default_namespace = None
                    namespace = self.ui.text(
                        "Android namespace",
                        default=namespace or default_namespace,
                        validate=validate_android_namespace,
                    )
                elif field == "version" and (version is None or revisit == field):
                    version = self.ui.text(
                        "Package version",
                        default=version or "0.1.0",
                        validate=validate_package_version,
                    )
                revisit = None
                index += 1
            except BackRequested:
                if index == 0:
                    self._back_or_cancel("add")
                index -= 1
                revisit = fields[index]
            except (CancelRequested, InputClosed):
                raise OperationCancelled("add")
            except InterruptRequested:
                raise OperationCancelled("add", interrupted=True)
        assert package is not None
        assert public_name is not None
        assert namespace is not None
        assert version is not None
        return FeatureAddDecisions(
            package,
            _starter_families(selected),
            description or "",
            public_name,
            namespace,
            version,
        )

    def _add_noninteractive(self) -> FeatureAddDecisions:
        package = self._provided_identifier(self.args.positional)
        if not package:
            raise ConfigurationError("package name is required")
        validate_package_name(package)
        yes = self.args.has("yes")
        missing: List[str] = []
        if not yes:
            for provided, label in (
                (bool(self.args.values_for("starter")), "--starter <cpp|kotlin>"),
                ("description" in self.args.provided, '--description <TEXT> or --description ""'),
                ("javascript_name" in self.args.provided, "--javascript-name <NAME>"),
                ("android_namespace" in self.args.provided, "--android-namespace <NAMESPACE>"),
                ("package_version" in self.args.provided, "--package-version <VERSION>"),
            ):
                if not provided:
                    missing.append(label)
        if missing:
            if missing == ["--starter <cpp|kotlin>"]:
                raise ConfigurationError(
                    "--starter is required without --yes in non-interactive mode"
                )
            raise ConfigurationError(
                "non-interactive Add is missing required decisions\n\nProvide:\n  "
                + "\n  ".join(missing)
                + "\n\nUse --yes to accept documented defaults where available."
            )
        starters = self.args.values_for("starter") or ("cpp",)
        description = normalize_description(self.args.value("description") or "")
        public_name = self._provided_identifier(self.args.value("javascript_name"))
        namespace = self._provided_identifier(self.args.value("android_namespace"))
        if public_name is None:
            try:
                public_name = infer_javascript_name(package)
            except ValidationError as exc:
                raise ConfigurationError(
                    f'could not derive a valid JavaScript name from "{package}"'
                ) from exc
        if namespace is None:
            try:
                namespace = infer_android_namespace(package)
            except ValidationError as exc:
                raise ConfigurationError(
                    f'could not derive a valid Android namespace from "{package}"'
                ) from exc
        version = self._provided_identifier(self.args.value("package_version")) or "0.1.0"
        validate_javascript_name(public_name)
        validate_android_namespace(namespace)
        validate_package_version(version)
        return FeatureAddDecisions(
            package,
            _starter_families(starters),
            description,
            public_name,
            namespace,
            version,
        )

    def update(self) -> FeatureUpdateDecisions:
        records = self.features.records()
        if self.interactive and not self.args.has("yes"):
            assert self.ui is not None
            print("\nUpdate the complete project", file=self.ui.terminal)
            print(
                f"  Regenerate {len(records)} authored "
                f"{'feature' if len(records) == 1 else 'features'}",
                file=self.ui.terminal,
            )
            print(
                "  Preserve authored source, package metadata, dependencies, and builds",
                file=self.ui.terminal,
            )
            print(
                "  Overwrite every generator-owned output and shared runtime file\n",
                file=self.ui.terminal,
            )
            if not self._confirm("update", "Update the project?", default=True):
                raise OperationCancelled("update")
        return FeatureUpdateDecisions()

    def validate(self) -> FeatureValidateDecisions:
        records = self.features.records()
        return FeatureValidateDecisions(
            tuple(record.manifest.npm_name for record in records)
        )

    def doctor_scope(self) -> str:
        if self.interactive:
            assert self.ui is not None
            self.ui.header("Doctor")
            self.ui.info("Checking the tool requirements for this plugin.", dim=True)
        return "plugin"

    def _provided_identifier(self, value: str | None) -> str | None:
        return strip_ascii(value).value if value is not None else None

    def _text(self, label: str, **kwargs) -> str:
        assert self.ui is not None
        try:
            return self.ui.text(label, **kwargs)
        except BackRequested:
            self._back_or_cancel("add")
        except (CancelRequested, InputClosed):
            raise OperationCancelled("add")
        except InterruptRequested:
            raise OperationCancelled("add", interrupted=True)

    def _confirm(self, command: str, label: str, *, default: bool) -> bool:
        assert self.ui is not None
        try:
            return self.ui.confirm(label, default=default)
        except (CancelRequested, InputClosed):
            raise OperationCancelled(command)
        except InterruptRequested:
            raise OperationCancelled(command, interrupted=True)

    def _back_or_cancel(self, command: str) -> None:
        raise OperationCancelled(command)


def _starter_families(values) -> tuple[StarterFamily, ...]:
    mapping = {"cpp": StarterFamily.NATIVE, "kotlin": StarterFamily.JVM}
    return tuple(mapping[value] for value in values)
