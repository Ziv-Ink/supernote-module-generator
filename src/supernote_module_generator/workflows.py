"""Explicit decision-state machines for each guided command workflow."""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .arguments import ParsedArguments
from .errors import ConfigurationError, OperationCancelled, ValidationError
from .interaction import (
    BackRequested,
    CancelRequested,
    InputClosed,
    InterruptRequested,
    Interaction,
    MenuItem,
)
from .models import WarningInfo
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
from .operations import AddDecisions, RemoveDecisions, UpdateDecisions, ValidateDecisions
from .project import (
    ManagedModule,
    dependency_link_path,
    dependency_value,
    find_module,
    git_status,
    managed_modules,
    manager_evidence,
    read_parent_package,
)

TYPE_ITEMS = [
    MenuItem(
        "native",
        "Native Module",
        "Kotlin/Java",
        completed_label="Native Module — Kotlin/Java",
        plain_description="Kotlin/Java",
        plain_completed_label="Native Module - Kotlin/Java",
        explanation=(
            "For Kotlin/Java code and Android APIs through the React Native "
            "bridge."
        ),
    ),
    MenuItem(
        "jni",
        "Native JNI Module",
        "C/C++ via JNI",
        completed_label="Native JNI Module — C/C++ via JNI",
        plain_description="C/C++ via JNI",
        plain_completed_label="Native JNI Module - C/C++ via JNI",
        explanation=(
            "For C/C++ behind an asynchronous Kotlin/Java React Native bridge."
        ),
    ),
    MenuItem(
        "jsi",
        "JSI Module",
        "C/C++ (synchronous)",
        completed_label="JSI Module — C/C++ (synchronous)",
        plain_description="C/C++",
        plain_completed_label="JSI Module - C/C++",
        explanation=(
            "Experimental synchronous C++; requires target PluginHost support."
        ),
    ),
]
DOCTOR_ITEMS = [
    MenuItem("all", "All"),
    MenuItem("native", "Native Module"),
    MenuItem("jni", "Native JNI Module"),
    MenuItem("jsi", "JSI Module"),
]


class ReturnToMenu(Exception):
    pass


class AddState(Enum):
    TYPE = auto()
    PACKAGE = auto()
    DESCRIPTION = auto()
    JAVASCRIPT = auto()
    NAMESPACE = auto()
    VERSION = auto()
    INSTALL = auto()
    MANAGER = auto()
    EXECUTE = auto()


class UpdateState(Enum):
    SELECT = auto()
    MANAGER = auto()
    PLAN = auto()
    CONFIRM = auto()
    EXECUTE = auto()


class ValidateState(Enum):
    SELECT = auto()
    BUILD = auto()
    EXECUTE = auto()


class RemoveState(Enum):
    SELECT = auto()
    MANAGER = auto()
    PLAN = auto()
    CONFIRM = auto()
    EXECUTE = auto()


class DoctorState(Enum):
    SELECT = auto()
    EXECUTE = auto()


@dataclass
class CollectionContext:
    warnings: List[WarningInfo]


class DecisionCollector:
    def __init__(
        self,
        root: Path,
        arguments: ParsedArguments,
        interaction: Optional[Interaction],
        *,
        launched_from_menu: bool = False,
    ) -> None:
        self.root = root
        self.args = arguments
        self.ui = interaction
        self.launched_from_menu = launched_from_menu
        self.context = CollectionContext([])

    @property
    def interactive(self) -> bool:
        return self.ui is not None

    def _first_back(self, command: str) -> None:
        if self.launched_from_menu:
            raise ReturnToMenu
        raise OperationCancelled(command)

    def _cancel(self, command: str) -> None:
        if self.launched_from_menu:
            raise ReturnToMenu
        raise OperationCancelled(command)

    def _interrupt(self, command: str) -> None:
        raise OperationCancelled(command, interrupted=True)

    def _normalize_identifier(self, value: str) -> str:
        normalized = strip_ascii(value)
        if normalized.changed and self.ui is not None:
            self.ui.info(
                f'Using "{normalized.value}" (surrounding whitespace removed).',
                dim=True,
            )
        return normalized.value

    def _empty(self, command: str, message: str) -> Tuple[None, Optional[str]]:
        if self.interactive:
            assert self.ui is not None
            label = "Validate module" if command == "validate" else f"{command.capitalize()} module"
            self.ui.header(label)
            print(f"\n{message}", file=self.ui.terminal)
            if self.launched_from_menu:
                self.ui.wait_for_return()
                raise ReturnToMenu
            return None, None
        return None, message

    def _manager(
        self,
        *,
        required: bool,
        allow_default: bool,
        current: Optional[str] = None,
    ) -> Optional[str]:
        explicit = self.args.value("package_manager")
        if not required:
            return explicit
        evidence = manager_evidence(self.root)
        if explicit:
            if evidence.sole and evidence.sole != explicit:
                self.context.warnings.append(
                    WarningInfo(
                        "lockfile_conflict",
                        f"{explicit} was selected, but the project contains only the other manager's lockfile.",
                        "collect_decisions",
                        None,
                    )
                )
            return explicit
        if evidence.sole:
            return evidence.sole
        if not self.interactive:
            if allow_default and not evidence.conflicting:
                return "npm"
            raise ConfigurationError("package manager is ambiguous")
        assert self.ui is not None
        if evidence.conflicting:
            self.ui.warning("Both package-lock.json and yarn.lock were found.")
            default = current
        else:
            default = current or "npm"
        return self.ui.menu(
            "Package manager",
            [MenuItem("npm", "npm"), MenuItem("yarn", "Yarn")],
            default=default,
            collapse_label="Package manager",
        )

    def add(self) -> AddDecisions:
        package_arg = (
            strip_ascii(self.args.positional).value
            if self.args.positional is not None
            else None
        )
        description_arg = (
            normalize_description(self.args.value("description") or "")
            if self.args.value("description") is not None
            else None
        )
        javascript_arg = (
            strip_ascii(str(self.args.value("javascript_name"))).value
            if self.args.value("javascript_name") is not None
            else None
        )
        namespace_arg = (
            strip_ascii(str(self.args.value("android_namespace"))).value
            if self.args.value("android_namespace") is not None
            else None
        )
        version_arg = (
            strip_ascii(str(self.args.value("package_version"))).value
            if self.args.value("package_version") is not None
            else None
        )
        if package_arg is not None:
            validate_package_name(package_arg)
        if javascript_arg is not None:
            validate_javascript_name(javascript_arg)
        if namespace_arg is not None:
            validate_android_namespace(namespace_arg)
        if version_arg is not None:
            validate_package_version(version_arg)
        if not self.interactive:
            return self._add_noninteractive()
        assert self.ui is not None
        self.ui.header("Add module")
        explicit = self.args.provided
        values: Dict[str, object] = {
            "type": self.args.value("type"),
            "package": package_arg,
            "description": description_arg,
            "javascript": javascript_arg,
            "namespace": namespace_arg,
            "version": version_arg,
            "install": False if self.args.has("skip_install") else None,
            "manager": self.args.value("package_manager"),
        }
        if self.args.has("yes"):
            values["type"] = values["type"] or "native"
            values["description"] = values["description"] if "description" in explicit else ""
            values["version"] = values["version"] or "0.1.0"
            values["install"] = not self.args.has("skip_install")
        supporting_answers = False
        for label, key in (
            ("Module type", "type"),
            ("Package name", "package"),
            ("Description", "description"),
            ("JavaScript name", "javascript"),
            ("Android namespace", "namespace"),
            ("Package version", "version"),
        ):
            if values[key] is not None and (
                key == "package" or key in explicit or self.args.has("yes")
            ):
                if key == "type":
                    item = next(item for item in TYPE_ITEMS if item.value == values[key])
                    shown = item.completed_label or item.label
                else:
                    shown = str(values[key]) if values[key] != "" else "(omitted)"
                self.ui.supporting_answer(label, shown)
                supporting_answers = True
        if supporting_answers:
            print(file=self.ui.terminal)

        state = AddState.TYPE
        history: List[AddState] = []
        revisit: Optional[AddState] = None
        while state is not AddState.EXECUTE:
            try:
                current_state = state
                revisiting = revisit is current_state
                prompted = False
                if state is AddState.TYPE:
                    if values["type"] is None or revisiting:
                        prompted = True
                        values["type"] = self.ui.menu(
                            "Module type",
                            TYPE_ITEMS,
                            default=str(values["type"] or "native"),
                            collapse_label="Module type",
                        )
                    state = AddState.PACKAGE
                elif state is AddState.PACKAGE:
                    if values["package"] is None or revisiting:
                        prompted = True
                        previous = str(values["package"] or "") or None
                        value = self.ui.text(
                            "Package name",
                            default=previous,
                            guidance="Used as the local folder and npm or Yarn dependency name.",
                            validate=validate_package_name,
                            normalize=self._normalize_identifier,
                        )
                        if value != values.get("package"):
                            if "javascript_name" not in explicit:
                                values["javascript"] = None
                            if "android_namespace" not in explicit:
                                values["namespace"] = None
                        values["package"] = value
                    state = AddState.DESCRIPTION
                elif state is AddState.DESCRIPTION:
                    if "description" not in explicit and not self.args.has("yes"):
                        prompted = True
                        value = self.ui.text(
                            "Description (optional)",
                            default=str(values["description"]) if values["description"] else None,
                            normalize=normalize_description,
                            optional=True,
                        )
                        values["description"] = value
                    state = AddState.JAVASCRIPT
                elif state is AddState.JAVASCRIPT:
                    package = str(values["package"])
                    try:
                        derived = infer_javascript_name(package)
                    except ValidationError:
                        derived = None
                    if values["javascript"] is None or revisiting:
                        if not self.args.has("yes") or derived is None or revisiting:
                            prompted = True
                            suggested = str(values["javascript"] or derived or "") or None
                            values["javascript"] = self.ui.text(
                                "JavaScript name",
                                default=suggested,
                                validate=validate_javascript_name,
                                normalize=self._normalize_identifier,
                                ghost_default=suggested is not None,
                            )
                        else:
                            values["javascript"] = derived
                            self.ui.answer("JavaScript name", f"{derived}  (derived)")
                    state = AddState.NAMESPACE
                elif state is AddState.NAMESPACE:
                    package = str(values["package"])
                    try:
                        derived_namespace = infer_android_namespace(package)
                    except ValidationError:
                        derived_namespace = None
                    if values["namespace"] is None or revisiting:
                        if not self.args.has("yes") or derived_namespace is None or revisiting:
                            prompted = True
                            suggested = str(values["namespace"] or derived_namespace or "") or None
                            values["namespace"] = self.ui.text(
                                "Android namespace",
                                default=suggested,
                                guidance=(
                                    "Enter a Java-style namespace, for example com.example.local_math."
                                    if derived_namespace is None
                                    else None
                                ),
                                validate=validate_android_namespace,
                                normalize=self._normalize_identifier,
                                ghost_default=suggested is not None,
                            )
                        else:
                            values["namespace"] = derived_namespace
                            self.ui.answer("Android namespace", f"{derived_namespace}  (derived)")
                    state = AddState.VERSION
                elif state is AddState.VERSION:
                    if values["version"] is None or revisiting:
                        prompted = True
                        values["version"] = self.ui.text(
                            "Package version",
                            default=str(values["version"] or "0.1.0"),
                            validate=validate_package_version,
                            normalize=self._normalize_identifier,
                            ghost_default=True,
                        )
                    state = AddState.INSTALL
                elif state is AddState.INSTALL:
                    if values["install"] is None or revisiting:
                        prompted = True
                        values["install"] = self.ui.confirm(
                            "Install the local dependency now?",
                            default=bool(values["install"]) if revisiting else True,
                        )
                    state = AddState.MANAGER
                elif state is AddState.MANAGER:
                    if values["install"]:
                        prompted = values["manager"] is None and manager_evidence(self.root).sole is None
                        values["manager"] = self._manager(
                            required=True,
                            allow_default=self.args.has("yes"),
                            current=(
                                str(values["manager"])
                                if revisiting and values["manager"] is not None
                                else None
                            ),
                        )
                    else:
                        values["manager"] = self.args.value("package_manager")
                    state = AddState.EXECUTE
                revisit = None
                if prompted:
                    history.append(_previous_state(state))
            except BackRequested:
                if not history:
                    self._first_back("add")
                state = history.pop()
                revisit = state
            except (CancelRequested, InputClosed):
                self._cancel("add")
            except InterruptRequested:
                self._interrupt("add")
        return AddDecisions(
            str(values["package"]),
            str(values["type"]),
            str(values["description"] or ""),
            str(values["javascript"]),
            str(values["namespace"]),
            str(values["version"]),
            bool(values["install"]),
            str(values["manager"]) if values["manager"] is not None else None,
            self.args.has("build"),
        )

    def _add_noninteractive(self) -> AddDecisions:
        package = (
            strip_ascii(self.args.positional).value
            if self.args.positional is not None
            else None
        )
        if not package:
            raise ConfigurationError("package name is required")
        validate_package_name(package)
        yes = self.args.has("yes")
        missing: List[str] = []
        if not yes:
            for key, display in (
                ("type", "--type <native|jni|jsi>"),
                ("description", '--description <TEXT> or --description ""'),
                ("javascript_name", "--javascript-name <NAME>"),
                ("android_namespace", "--android-namespace <NAMESPACE>"),
                ("package_version", "--package-version <VERSION>"),
            ):
                if key not in self.args.provided:
                    missing.append(display)
            if not self.args.has("skip_install") and not self.args.value("package_manager") and manager_evidence(self.root).sole is None:
                missing.append("--package-manager <npm|yarn>")
        if missing:
            if missing == ["--type <native|jni|jsi>"]:
                raise ConfigurationError(
                    "--type is required without --yes in non-interactive mode"
                )
            raise ConfigurationError(
                "non-interactive Add is missing required decisions\n\nProvide:\n  "
                + "\n  ".join(missing)
                + "\n\nUse --yes to accept documented defaults where available."
            )
        module_type = self.args.value("type") or "native"
        description = normalize_description(self.args.value("description") or "")
        javascript = self.args.value("javascript_name")
        namespace = self.args.value("android_namespace")
        javascript = strip_ascii(javascript).value if javascript is not None else None
        namespace = strip_ascii(namespace).value if namespace is not None else None
        if javascript is None:
            try:
                javascript = infer_javascript_name(package)
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
        validate_javascript_name(javascript)
        validate_android_namespace(namespace)
        supplied_version = self.args.value("package_version")
        version = strip_ascii(supplied_version).value if supplied_version is not None else "0.1.0"
        validate_package_version(version)
        install = not self.args.has("skip_install")
        manager = self._manager(required=install, allow_default=yes)
        return AddDecisions(
            package,
            module_type,
            description,
            javascript,
            namespace,
            version,
            install,
            manager,
            self.args.has("build"),
        )

    def _select_modules(self, command: str, include_all: str) -> Tuple[List[ManagedModule], bool]:
        modules = managed_modules(self.root)
        if not modules:
            return [], False
        synthetic: List[MenuItem] = []
        if include_all == "first":
            synthetic.append(MenuItem("__all__", "All modules", f"{len(modules)} modules"))
        items = [
            MenuItem(
                module.config.npm_name,
                module.config.npm_name,
                module.info().type_label,
            )
            for module in modules
        ]
        if include_all == "last":
            items.append(
                MenuItem(
                    "__all__",
                    "All modules",
                    f"Permanently delete all {len(modules)} modules",
                    separator_before=True,
                )
            )
        items = synthetic + items
        assert self.ui is not None
        selected = self.ui.menu(
            "Module",
            items,
            default=items[0].value,
            collapse_label="Module",
        )
        return (modules, True) if selected == "__all__" else ([find_module(self.root, selected)], False)

    def update(self) -> Tuple[Optional[UpdateDecisions], Optional[str]]:
        modules = managed_modules(self.root)
        if not modules:
            return self._empty(
                "update",
                "No modules were found in this plugin.\nAdd one with `supernote-module add`.",
            )
        if not self.interactive and not self.args.positional:
            raise ConfigurationError("Update needs more information in non-interactive mode\n\n  missing  module")
        if not self.interactive and not self.args.has("yes"):
            raise ConfigurationError("Update requires --yes in non-interactive mode")
        module = (
            find_module(self.root, strip_ascii(self.args.positional).value)
            if self.args.positional
            else None
        )
        if self.interactive:
            assert self.ui is not None
            self.ui.header("Update module")
            if module is not None:
                self.ui.supporting_answer("Module", module.config.npm_name)
        state = UpdateState.SELECT
        history: List[UpdateState] = []
        manager: Optional[str] = None
        refresh = False
        git = "status unavailable"
        while state is not UpdateState.EXECUTE:
            try:
                if state is UpdateState.SELECT:
                    if module is None:
                        assert self.ui is not None
                        selected, _ = self._select_modules("update", "none")
                        module = selected[0]
                        history.append(UpdateState.SELECT)
                    refresh = _refresh_required(self.root, module)
                    if not refresh:
                        irrelevant = []
                        if self.args.has("skip_install"):
                            irrelevant.append("--skip-install")
                        if self.args.value("package_manager") is not None:
                            irrelevant.append("--package-manager")
                        if irrelevant:
                            options = " and ".join(irrelevant)
                            raise ConfigurationError(
                                f"{options} does not affect this update because "
                                "dependency refresh is not required"
                            )
                    state = UpdateState.MANAGER
                elif state is UpdateState.MANAGER:
                    manager_needed = refresh and not self.args.has("skip_install")
                    manager_prompted = (
                        manager_needed
                        and self.args.value("package_manager") is None
                        and manager_evidence(self.root).sole is None
                    )
                    manager = self._manager(
                        required=manager_needed,
                        allow_default=False,
                        current=manager,
                    )
                    if manager_prompted:
                        history.append(UpdateState.MANAGER)
                    state = UpdateState.PLAN
                elif state is UpdateState.PLAN:
                    git = git_status(self.root)
                    if self.interactive and not self.args.has("yes"):
                        assert self.ui is not None
                        _show_update_plan(self.ui, module, refresh, self.args.has("build"), git)
                        state = UpdateState.CONFIRM
                    else:
                        state = UpdateState.EXECUTE
                elif state is UpdateState.CONFIRM:
                    assert self.ui is not None
                    if not self.ui.confirm("Update this module?", default=True):
                        if self.launched_from_menu:
                            print("Update cancelled.", file=self.ui.renderer.stdout)
                            raise ReturnToMenu
                        raise OperationCancelled("update")
                    state = UpdateState.EXECUTE
            except BackRequested:
                if not history:
                    self._first_back("update")
                previous = history.pop()
                if previous is UpdateState.SELECT:
                    module = None
                state = previous
            except (CancelRequested, InputClosed):
                self._cancel("update")
            except InterruptRequested:
                self._interrupt("update")
        assert module is not None
        return UpdateDecisions(module.config.npm_name, manager, self.args.has("skip_install"), self.args.has("build")), None

    def validate(self) -> Tuple[Optional[ValidateDecisions], Optional[str]]:
        modules = managed_modules(self.root)
        if not modules:
            return self._empty("validate", "No modules were found in this plugin.")
        if not self.interactive and not self.args.has("all") and not self.args.positional:
            raise ConfigurationError("Validate needs more information in non-interactive mode\n\n  missing  module or --all")
        selected: Optional[List[ManagedModule]] = None
        all_selected = False
        if self.args.has("all"):
            selected, all_selected = modules, True
        elif self.args.positional:
            selected = [find_module(self.root, strip_ascii(self.args.positional).value)]
        if self.interactive:
            assert self.ui is not None
            self.ui.header("Validate module")
        state = ValidateState.SELECT
        history: List[ValidateState] = []
        build = self.args.has("build")
        while state is not ValidateState.EXECUTE:
            try:
                if state is ValidateState.SELECT:
                    if selected is None:
                        assert self.ui is not None
                        selected, all_selected = self._select_modules("validate", "first")
                        history.append(ValidateState.SELECT)
                    state = ValidateState.BUILD
                elif state is ValidateState.BUILD:
                    if self.interactive and not self.args.has("build"):
                        assert self.ui is not None
                        build = self.ui.confirm("Run an Android build too?", default=False)
                    state = ValidateState.EXECUTE
            except BackRequested:
                if not history:
                    self._first_back("validate")
                previous = history.pop()
                if previous is ValidateState.SELECT:
                    selected = None
                state = previous
            except (CancelRequested, InputClosed):
                self._cancel("validate")
            except InterruptRequested:
                self._interrupt("validate")
        assert selected is not None
        return ValidateDecisions([module.config.npm_name for module in selected], all_selected, build), None

    def remove(self) -> Tuple[Optional[RemoveDecisions], Optional[str]]:
        modules = managed_modules(self.root)
        if not modules:
            return self._empty("remove", "No modules were found in this plugin.")
        if self.args.has("yes") and not self.args.has("all") and not self.args.positional:
            raise ConfigurationError("--yes requires an explicit module or --all")
        if not self.interactive and not self.args.has("all") and not self.args.positional:
            raise ConfigurationError("Remove needs more information in non-interactive mode\n\n  missing  module or --all")
        if not self.interactive and not self.args.has("yes"):
            raise ConfigurationError("Remove requires --yes in non-interactive mode")
        selected: Optional[List[ManagedModule]] = None
        all_selected = False
        if self.args.has("all"):
            selected, all_selected = modules, True
        elif self.args.positional:
            selected = [find_module(self.root, strip_ascii(self.args.positional).value)]
        if self.interactive:
            assert self.ui is not None
            self.ui.header("Remove module")
        manager: Optional[str] = None
        state = RemoveState.SELECT
        history: List[RemoveState] = []
        while state is not RemoveState.EXECUTE:
            try:
                if state is RemoveState.SELECT:
                    if selected is None:
                        assert self.ui is not None
                        selected, all_selected = self._select_modules("remove", "last")
                        history.append(RemoveState.SELECT)
                    state = RemoveState.MANAGER
                elif state is RemoveState.MANAGER:
                    manager_needed = not self.args.has("skip_install")
                    manager_prompted = (
                        manager_needed
                        and self.args.value("package_manager") is None
                        and manager_evidence(self.root).sole is None
                    )
                    manager = self._manager(
                        required=manager_needed,
                        allow_default=False,
                        current=manager,
                    )
                    if manager_prompted:
                        history.append(RemoveState.MANAGER)
                    state = RemoveState.PLAN
                elif state is RemoveState.PLAN:
                    if self.interactive and not self.args.has("yes"):
                        assert self.ui is not None and selected is not None
                        _show_remove_plan(self.ui, selected, all_selected, git_status(self.root))
                        state = RemoveState.CONFIRM
                    else:
                        state = RemoveState.EXECUTE
                elif state is RemoveState.CONFIRM:
                    assert self.ui is not None and selected is not None
                    expected = "REMOVE ALL" if all_selected else selected[0].config.npm_name
                    prompt = (
                        'Type "REMOVE ALL" to continue: '
                        if all_selected
                        else f'Type "{expected}" to continue: '
                    )
                    confirmed = self.ui.typed_confirmation(prompt, expected)
                    if not confirmed:
                        if self.launched_from_menu:
                            print("Remove cancelled.", file=self.ui.renderer.stdout)
                            raise ReturnToMenu
                        raise OperationCancelled("remove")
                    state = RemoveState.EXECUTE
            except BackRequested:
                if not history:
                    self._first_back("remove")
                previous = history.pop()
                if previous is RemoveState.SELECT:
                    selected = None
                state = previous
            except (CancelRequested, InputClosed):
                self._cancel("remove")
            except InterruptRequested:
                self._interrupt("remove")
        assert selected is not None
        return RemoveDecisions([module.config.npm_name for module in selected], all_selected, manager, self.args.has("skip_install")), None

    def doctor_scope(self) -> str:
        explicit = self.args.value("type")
        if explicit:
            return explicit
        if not self.interactive:
            return "all"
        assert self.ui is not None
        self.ui.header("Doctor")
        state = DoctorState.SELECT
        scope = "all"
        while state is not DoctorState.EXECUTE:
            try:
                if state is DoctorState.SELECT:
                    scope = self.ui.menu("Check", DOCTOR_ITEMS, default="all", collapse_label="Check")
                    state = DoctorState.EXECUTE
            except BackRequested:
                self._first_back("doctor")
            except (CancelRequested, InputClosed):
                self._cancel("doctor")
            except InterruptRequested:
                self._interrupt("doctor")
        return scope


def _previous_state(next_state: AddState) -> AddState:
    order = list(AddState)
    return order[max(0, order.index(next_state) - 1)]


def _refresh_required(root: Path, module: ManagedModule) -> bool:
    _, parent = read_parent_package(root)
    dependencies = parent.get("dependencies", {})
    value = dependencies.get(module.config.npm_name) if isinstance(dependencies, dict) else None
    link = dependency_link_path(root, module.config.npm_name)
    try:
        linked = link.exists() and link.resolve() == module.path.resolve()
    except OSError:
        linked = False
    return value != dependency_value(module.config.npm_name) or not linked


def _show_update_plan(ui: Interaction, module: ManagedModule, refresh: bool, build: bool, git: str) -> None:
    print(f'\nUpdate "{module.config.npm_name}"\n', file=ui.terminal)
    print("  Replace", file=ui.terminal)
    print("    Generated Kotlin source" if module.type == "native" else "    Generated native bindings", file=ui.terminal)
    print("    Generated Gradle configuration", file=ui.terminal)
    print("    Generated package metadata\n", file=ui.terminal)
    print("  Preserve", file=ui.terminal)
    print("    Implementation source", file=ui.terminal)
    print("    Description and package version\n", file=ui.terminal)
    print("  Parent changes", file=ui.terminal)
    print("    Dependency refresh required" if refresh else "    No dependency refresh required", file=ui.terminal)
    if build:
        print("\n  Verify\n    Android build", file=ui.terminal)
    print(f"\n  Git\n    {git}\n", file=ui.terminal)


def _show_remove_plan(ui: Interaction, modules: Sequence[ManagedModule], all_selected: bool, git: str) -> None:
    if all_selected:
        print("\nRemove all modules\n", file=ui.terminal)
        print(f"  {len(modules)} modules will be permanently deleted:", file=ui.terminal)
        for module in modules:
            print(f"    {module.config.npm_name}", file=ui.terminal)
        print("\n  Parent dependency and Gradle integration will be removed.\n", file=ui.terminal)
        print("This will permanently delete every module and its implementation source.", file=ui.terminal)
        return
    module = modules[0]
    print(f'\nRemove "{module.config.npm_name}"\n', file=ui.terminal)
    print(f"  Module path   {module.path}", file=ui.terminal)
    print("  Parent state  Dependency and Gradle integration will be removed", file=ui.terminal)
    print(f"  Git           {git}\n", file=ui.terminal)
    print("This will permanently delete the module and its implementation source.", file=ui.terminal)
