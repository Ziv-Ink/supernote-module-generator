"""Public CLI decisions for language-neutral logical features."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .arguments import ParsedArguments
from .errors import ConfigurationError, OperationCancelled, ValidationError
from .feature_model import StarterFamily
from .feature_operations import FeatureOperationService, FeatureRecord
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
from .project import dependency_link_path, dependency_value, manager_evidence, read_parent_package


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
    install: bool
    package_manager: Optional[str]
    build: bool


@dataclass(frozen=True)
class FeatureUpdateDecisions:
    package_name: str
    package_manager: Optional[str]
    skip_install: bool
    build: bool


@dataclass(frozen=True)
class FeatureValidateDecisions:
    package_names: tuple[str, ...]
    all: bool
    build: bool


@dataclass(frozen=True)
class FeatureRemoveDecisions:
    package_names: tuple[str, ...]
    all: bool
    package_manager: Optional[str]
    skip_install: bool
    delete_build_files: bool


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
        install = not self.args.has("skip_install")
        if not self.args.has("skip_install") and not self.args.has("yes"):
            install = self._confirm("add", "Install dependencies now?", default=True)
        manager = self._manager(required=install, allow_default=self.args.has("yes"))
        return FeatureAddDecisions(
            package,
            _starter_families(selected),
            description or "",
            public_name,
            namespace,
            version,
            install,
            manager,
            self.args.has("build"),
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
            if (
                not self.args.has("skip_install")
                and not self.args.value("package_manager")
                and manager_evidence(self.root).sole is None
            ):
                missing.append("--package-manager <npm|yarn>")
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
        install = not self.args.has("skip_install")
        return FeatureAddDecisions(
            package,
            _starter_families(starters),
            description,
            public_name,
            namespace,
            version,
            install,
            self._manager(required=install, allow_default=yes),
            self.args.has("build"),
        )

    def update(self) -> FeatureUpdateDecisions | None:
        records = self.features.records()
        if not records:
            self._reject_missing_explicit_target()
            return None
        record = self._choose_one("Update feature", records)
        if not self.interactive and not self.args.has("yes"):
            raise ConfigurationError("Update requires --yes in non-interactive mode")
        refresh = self._refresh_required(record)
        if not refresh:
            irrelevant = []
            if self.args.value("package_manager") is not None:
                irrelevant.append("--package-manager")
            if irrelevant:
                raise ConfigurationError(
                    " and ".join(irrelevant)
                    + " does not affect this update because dependency refresh "
                    "is not required"
                )
        manager = self._manager(
            required=refresh and not self.args.has("skip_install"),
            allow_default=False,
        )
        if self.interactive and not self.args.has("yes"):
            assert self.ui is not None
            print(f'\nUpdate "{record.manifest.npm_name}"', file=self.ui.terminal)
            print("  Preserve all user-owned C/C++ and Kotlin/Java sources", file=self.ui.terminal)
            print(
                "  Regenerate feature metadata, JavaScript API docs, and the shared plugin runtime\n",
                file=self.ui.terminal,
            )
            if not self._confirm("update", "Update this feature?", default=True):
                raise OperationCancelled("update")
        return FeatureUpdateDecisions(
            record.manifest.npm_name,
            manager,
            self.args.has("skip_install"),
            self.args.has("build"),
        )

    def validate(self) -> FeatureValidateDecisions | None:
        records = self.features.records()
        if not records:
            self._reject_missing_explicit_target()
            return None
        if self.args.has("all"):
            selected = records
            all_selected = True
        elif self.args.positional:
            selected = [self.features.find_record(self._provided_identifier(self.args.positional) or "")]
            all_selected = False
        elif self.interactive:
            selected = [self._menu_record("Validate feature", records, include_all=True)]
            if selected[0] is None:  # type: ignore[comparison-overlap]
                selected = records
                all_selected = True
            else:
                all_selected = False
        else:
            raise ConfigurationError(
                "Validate needs more information in non-interactive mode\n\n  missing  feature or --all"
            )
        build = self.args.has("build")
        if self.interactive and not build:
            build = self._confirm(
                "validate", "Run an Android build too?", default=False
            )
        return FeatureValidateDecisions(
            tuple(record.manifest.npm_name for record in selected),
            all_selected,
            build,
        )

    def remove(self) -> FeatureRemoveDecisions | None:
        records = self.features.records()
        if not records:
            self._reject_missing_explicit_target()
            return None
        if self.args.has("yes") and not self.args.has("all") and not self.args.positional:
            raise ConfigurationError("--yes requires an explicit module or --all")
        if self.args.has("all"):
            selected = records
            all_selected = True
        elif self.args.positional:
            selected = [self.features.find_record(self._provided_identifier(self.args.positional) or "")]
            all_selected = False
        elif self.interactive:
            record = self._menu_record("Remove feature", records, include_all=True)
            selected = records if record is None else [record]
            all_selected = record is None
        else:
            raise ConfigurationError(
                "Remove needs more information in non-interactive mode\n\n  missing  feature or --all"
            )
        if not self.interactive and not self.args.has("yes"):
            raise ConfigurationError("Remove requires --yes in non-interactive mode")
        delete_build_files = self.args.has("delete_build_files")
        if self.interactive and not self.args.has("yes") and not delete_build_files:
            delete_build_files = self._confirm(
                "remove", "Also delete generated plugin build files?", default=False
            )
        if self.interactive and not self.args.has("yes"):
            assert self.ui is not None
            expected = "REMOVE ALL" if all_selected else selected[0].manifest.npm_name
            prompt = (
                'Type "REMOVE ALL" to continue: '
                if all_selected
                else f'Type "{expected}" to continue: '
            )
            if not self.ui.typed_confirmation(prompt, expected):
                self.ui.error(
                    f'Confirmation did not match. Type "{expected}" exactly, '
                    "or type :cancel."
                )
                if not self.ui.typed_confirmation(prompt, expected):
                    raise OperationCancelled("remove")
        skip_install = self.args.has("skip_install")
        manager = self._manager(required=not skip_install, allow_default=False)
        return FeatureRemoveDecisions(
            tuple(record.manifest.npm_name for record in selected),
            all_selected,
            manager,
            skip_install,
            delete_build_files,
        )

    def doctor_scope(self) -> str:
        if self.interactive:
            assert self.ui is not None
            self.ui.header("Doctor")
            self.ui.info("Checking the tool requirements for this plugin.", dim=True)
        return "plugin"

    def _choose_one(self, heading: str, records: list[FeatureRecord]) -> FeatureRecord:
        if self.args.positional:
            return self.features.find_record(self._provided_identifier(self.args.positional) or "")
        if not self.interactive:
            raise ConfigurationError(
                f"{heading.split()[0]} needs more information in non-interactive mode\n\n  missing  feature"
            )
        record = self._menu_record(heading, records, include_all=False)
        assert record is not None
        return record

    def _reject_missing_explicit_target(self) -> None:
        """Resolve a supplied target before the valid empty-project outcome."""

        target = self._provided_identifier(self.args.positional)
        if target is not None:
            self.features.find_record(target)

    def _menu_record(
        self, heading: str, records: list[FeatureRecord], *, include_all: bool
    ) -> FeatureRecord | None:
        assert self.ui is not None
        self.ui.header(heading)
        items = [
            MenuItem(record.manifest.npm_name, record.manifest.npm_name, "Supernote feature")
            for record in records
        ]
        if include_all:
            count = len(records)
            items.insert(
                0,
                MenuItem(
                    "__all__",
                    "All features",
                    f"{count} {'feature' if count == 1 else 'features'}",
                ),
            )
        selected = self.ui.menu("Feature", items, default=items[0].value)
        if selected == "__all__":
            return None
        return next(record for record in records if record.manifest.npm_name == selected)

    def _manager(self, *, required: bool, allow_default: bool) -> Optional[str]:
        explicit = self.args.value("package_manager")
        if not required:
            return explicit
        evidence = manager_evidence(self.root)
        if explicit:
            return explicit
        if evidence.sole:
            return evidence.sole
        if not self.interactive:
            if allow_default and not evidence.conflicting:
                return "npm"
            raise ConfigurationError("package manager is ambiguous")
        assert self.ui is not None
        return self.ui.menu(
            "Package manager",
            [MenuItem("npm", "npm"), MenuItem("yarn", "Yarn")],
            default="npm",
        )

    def _refresh_required(self, record: FeatureRecord) -> bool:
        _, package = read_parent_package(self.root)
        dependencies = package.get("dependencies", {})
        value = (
            dependencies.get(record.manifest.npm_name)
            if isinstance(dependencies, dict)
            else None
        )
        link = dependency_link_path(self.root, record.manifest.npm_name)
        try:
            linked = link.exists() and link.resolve() == record.path.resolve()
        except OSError:
            linked = False
        return value != dependency_value(record.manifest.npm_name) or not linked

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
