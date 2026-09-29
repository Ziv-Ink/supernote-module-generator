"""Generate concise, feature-specific usage documentation from semantics."""
from __future__ import annotations

from typing import Iterable

from .reachability import PublicApi, compute_public_api
from .semantic import (
    ExecutionMode,
    MemberScope,
    SemanticApi,
    SemanticBinding,
    SemanticEnumDeclaration,
    SemanticObjectDeclaration,
    SemanticParameter,
    SemanticValueDeclaration,
)
from .semantic_types import SemanticTypeKind
from .typescript_codegen import render_semantic_type


_RUNTIME_EXPORTS = (
    "SupernoteError",
    "getFeatureStatus",
    "isFeatureAvailable",
    "nativeObjectInfo",
    "isSupernoteTypeError",
    "isSupernoteRangeError",
)


def runtime_unavailable_message(npm_name: str) -> str:
    """Return the actionable missing-runtime message for one module package."""

    return (
        f"runtime-unavailable: @supernote/runtime is not loaded for {npm_name}. "
        "Add @supernote/runtime as a direct dependency and rebuild the plugin."
    )


def feature_unavailable_message(npm_name: str) -> str:
    """Return the actionable missing-feature message for one module package."""

    return (
        f"feature-unavailable: {npm_name} is not loaded in the Supernote runtime. "
        f"Add {npm_name} as a direct dependency and rebuild the plugin."
    )


def render_feature_readme(
    *,
    npm_name: str,
    public_name: str,
    description: str,
    generator_version: str,
    implementation_roots: tuple[tuple[str, str], ...],
    api: SemanticApi,
) -> str:
    """Render the README and public call summary from the semantic API."""

    public = compute_public_api(api, feature_name=public_name)
    names = {item.type_id: item.name for item in public.declarations}
    lines = [f"# {npm_name}", ""]
    if description:
        lines.extend((_markdown_text(description), ""))
    lines.extend(
        (
            "This package exposes one Supernote feature to JavaScript. The examples and",
            "API below are generated from its marked C++, Kotlin, and Java declarations.",
            "",
            "## Install and build",
            "",
            "Declare the shared runtime and this module as direct dependencies:",
            "",
            "```sh",
            f"npm install @supernote/runtime {npm_name}",
            "# or: yarn add @supernote/runtime " + npm_name,
            "```",
            "",
            "Yarn classic works directly. For modern Yarn, configure",
            "`nodeLinker: node-modules` in `.yarnrc.yml`. Yarn Plug'n'Play is not",
            "supported. Then run the plugin template's normal packaging command once:",
            "",
            "```sh",
            "./buildPlugin.sh",
            "# Windows: .\\buildPlugin.ps1",
            "```",
            "",
            "The normal build discovers and compiles declared modules. Do not apply the",
            "runtime Gradle helper manually, and do not run `sn-module-gen` in the consumer.",
            "",
            "## Import",
            "",
            "```ts",
            f"import {public_name} from '{npm_name}';",
            "```",
        )
    )
    type_names = sorted(item.name for item in public.declarations)
    if type_names:
        lines.extend(
            (
                "",
                "Feature types can be imported separately:",
                "",
                "```ts",
                f"import type {{ {', '.join(type_names)} }} from '{npm_name}';",
                "```",
            )
        )
    lines.extend(
        (
            "",
            "Optional runtime exports: "
            + ", ".join(f"`{name}`" for name in _RUNTIME_EXPORTS)
            + ".",
            "The complete support-type declarations are in `index.d.ts`.",
        )
    )

    if not public.functions and not public.declarations:
        lines.extend(
            (
                "",
                "## Public API",
                "",
                "No JavaScript-public declarations are currently generated for this feature.",
                "After marking an API, run plain `sn-module-gen update` to regenerate "
                "this README and `index.d.ts`.",
            )
        )
    else:
        examples = _quick_examples(public_name, public, names)
        if examples:
            lines.extend(("", "## Quick use", "", "```ts", *examples, "```"))
        lines.extend(("", "## Public API", ""))
        _append_functions(lines, public_name, public, names)
        _append_declarations(lines, public_name, public, names)
        lines.append(
            "All generated callables also provide `.accepts(...)` and "
            "`.checkArguments(...)`."
        )
        if type_names:
            lines.append(
                "Type companions such as "
                f"`{public_name}.{type_names[0]}.is(value)` provide safe runtime checks."
            )
        _append_call_behavior(lines, public_name, public)
        _append_value_behavior(lines, public)

    lines.extend(
        (
            "",
            "## Availability diagnostics",
            "",
            "`getFeatureStatus()` returns `available`, `runtime-unavailable`, or",
            "`feature-unavailable`. Accessing the feature while unavailable throws the",
            "matching actionable message:",
            "",
            f"- `{runtime_unavailable_message(npm_name)}`",
            f"- `{feature_unavailable_message(npm_name)}`",
            "",
            "## Implementation",
            "",
        )
    )
    for label, path in implementation_roots:
        lines.append(f"- {label}: `{path}`")
    lines.extend(
        (
            "",
            f"Generated by Supernote Module Generator `{generator_version}`. After changing",
            "marked declarations, run:",
            "",
            "```sh",
            "sn-module-gen update",
            "```",
            "",
            "Plain update replaces this generated README and `index.d.ts`.",
            "It preserves the C++, Kotlin, and Java implementation source.",
            "",
            "Generator guides:",
            "",
            "- [Managing modules](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Managing-Modules)",
            "- [Error handling](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Error-Handling)",
            "- [Troubleshooting](https://github.com/Ziv-Ink/supernote-module-generator/wiki/Troubleshooting)",
            "",
        )
    )
    return "\n".join(lines)


def _append_functions(
    lines: list[str],
    public_name: str,
    public: PublicApi,
    names: dict[str, str],
) -> None:
    if not public.functions:
        return
    lines.extend(("### Functions", ""))
    for binding in public.functions:
        lines.append(
            f"- `{_binding_signature(f'{public_name}.', binding, names)}`"
            f" — {_mode(binding)}"
        )
    lines.append("")


def _append_declarations(
    lines: list[str],
    public_name: str,
    public: PublicApi,
    names: dict[str, str],
) -> None:
    for item in public.declarations:
        if isinstance(item, SemanticEnumDeclaration):
            constants = " | ".join(repr(value) for value in item.constants)
            lines.extend(
                (
                    f"### `{item.name}` enum",
                    "",
                    f"- `{item.name} = {constants}`",
                    f"- Check unknown values with `{public_name}.{item.name}.is(value)`.",
                    "",
                )
            )
            continue
        if isinstance(item, SemanticValueDeclaration):
            lines.extend((f"### `{item.name}` — copied value", ""))
            for field in item.fields:
                lines.append(
                    f"- `{field.name}: {render_semantic_type(field.type, names)}`"
                )
            lines.append("")
            continue
        assert isinstance(item, SemanticObjectDeclaration)
        lines.extend((f"### `{item.name}` — native object", ""))
        if item.constructor is not None:
            parameters = _parameters(item.constructor.parameters, names)
            lines.append(
                f"- `{public_name}.{item.name}.create({parameters}): {item.name}`"
                " — sync"
            )
        for method in sorted(
            item.methods, key=lambda value: (value.name, value.binding_id)
        ):
            if not method.capabilities.javascript_public:
                continue
            prefix = (
                f"{public_name}.{item.name}."
                if method.member_scope is MemberScope.STATIC
                else f"{_variable(item.name)}."
            )
            lines.append(
                f"- `{_binding_signature(prefix, method, names)}` — {_mode(method)}"
            )
        for field in item.fields:
            access = "mutable" if field.mutable else "read-only"
            lines.append(
                f"- `{_variable(item.name)}.{field.name}: "
                f"{render_semantic_type(field.type, names)}` — {access}"
            )
        lines.append("")


def _append_call_behavior(
    lines: list[str], public_name: str, public: PublicApi
) -> None:
    asynchronous = _async_paths(public_name, public)
    lines.extend(("## Call behavior", ""))
    if asynchronous:
        lines.append("Async calls return promises and should normally be used with `await`:")
        lines.append("")
        lines.extend(f"- `{path}`" for path in asynchronous)
        lines.extend(
            (
                "",
                "All other listed calls are synchronous and finish before JavaScript continues.",
            )
        )
    else:
        lines.append(
            "All listed calls are synchronous and finish before JavaScript continues. "
            "Long-running synchronous work blocks JavaScript unless the implementation "
            "manages its own threading."
        )


def _append_value_behavior(lines: list[str], public: PublicApi) -> None:
    objects = any(
        isinstance(item, SemanticObjectDeclaration) for item in public.declarations
    )
    values = any(
        isinstance(item, (SemanticValueDeclaration, SemanticEnumDeclaration))
        for item in public.declarations
    )
    if not objects and not values:
        return
    lines.extend(("", "## Values and objects", ""))
    if objects:
        lines.append(
            "Native objects keep their native identity and lifetime while JavaScript holds them."
        )
    if values:
        lines.append(
            "Copied values are validated against their declared fields and copied across the bridge."
        )


def _quick_examples(
    public_name: str,
    public: PublicApi,
    names: dict[str, str],
) -> list[str]:
    examples: list[str] = []
    if public.functions:
        binding = public.functions[0]
        examples.extend(
            _example_binding_function(
                public_name,
                f"{public_name}.",
                binding,
                names,
            )
        )
    constructor = next(
        (
            item
            for item in public.declarations
            if isinstance(item, SemanticObjectDeclaration)
            and item.constructor is not None
        ),
        None,
    )
    if constructor is not None:
        parameters = constructor.constructor.parameters
        local_name = _example_identifier(
            _variable(constructor.name),
            {public_name},
        )
        aliases = _example_parameter_aliases(
            parameters,
            {public_name, local_name},
        )
        arguments = ", ".join(aliases)
        if examples:
            examples.append("")
        examples.extend(
            _example_function(
                _example_identifier("exampleCreate", {public_name}),
                parameters,
                aliases,
                f"const {local_name} = "
                f"{public_name}.{constructor.name}.create({arguments});",
                names,
            )
        )
    if not examples:
        static = next(
            (
                (item, method)
                for item in public.declarations
                if isinstance(item, SemanticObjectDeclaration)
                for method in item.methods
                if method.capabilities.javascript_public
                and method.member_scope is MemberScope.STATIC
            ),
            None,
        )
        if static is not None:
            item, binding = static
            examples.extend(
                _example_binding_function(
                    public_name,
                    f"{public_name}.{item.name}.",
                    binding,
                    names,
                )
            )
    return examples


def _example_binding_function(
    public_name: str,
    invocation_prefix: str,
    binding: SemanticBinding,
    declaration_names: dict[str, str],
) -> list[str]:
    result_name = (
        None
        if binding.result.kind is SemanticTypeKind.VOID
        else _example_identifier("result", {public_name})
    )
    aliases = _example_parameter_aliases(
        binding.parameters,
        {public_name, *(() if result_name is None else (result_name,))},
    )
    invocation = _invocation(invocation_prefix, binding, aliases)
    return _example_function(
        _example_identifier("exampleCall", {public_name}),
        binding.parameters,
        aliases,
        _statement(invocation, binding, result_name=result_name),
        declaration_names,
        asynchronous=binding.execution is ExecutionMode.ASYNC,
    )


def _example_function(
    name: str,
    parameters: tuple[SemanticParameter, ...],
    parameter_names: tuple[str, ...],
    statement: str,
    declaration_names: dict[str, str],
    *,
    asynchronous: bool = False,
) -> list[str]:
    prefix = "async " if asynchronous else ""
    return [
        f"{prefix}function {name}("
        f"{_example_parameters(parameters, parameter_names, declaration_names)}) {{",
        f"  {statement}",
        "}",
    ]


def _example_parameter_aliases(
    parameters: tuple[SemanticParameter, ...],
    reserved: set[str],
) -> tuple[str, ...]:
    original_names = {item.name for item in parameters}
    unavailable = set(reserved)
    aliases: list[str] = []
    for parameter in parameters:
        alias = parameter.name
        if alias in unavailable:
            alias = _example_identifier(
                parameter.name + "Value",
                unavailable | original_names,
            )
        aliases.append(alias)
        unavailable.add(alias)
    return tuple(aliases)


def _example_identifier(preferred: str, unavailable: set[str]) -> str:
    if preferred not in unavailable:
        return preferred
    base = preferred + "Value"
    candidate = base
    suffix = 2
    while candidate in unavailable:
        candidate = f"{base}{suffix}"
        suffix += 1
    return candidate


def _example_parameters(
    parameters: tuple[SemanticParameter, ...],
    names: tuple[str, ...],
    declaration_names: dict[str, str],
) -> str:
    return ", ".join(
        f"{name}: {render_semantic_type(parameter.type, declaration_names)}"
        for parameter, name in zip(parameters, names)
    )


def _statement(
    invocation: str,
    binding: SemanticBinding,
    *,
    result_name: str | None = None,
) -> str:
    awaited = (
        f"await {invocation}"
        if binding.execution is ExecutionMode.ASYNC
        else invocation
    )
    if binding.result.kind is SemanticTypeKind.VOID:
        return awaited + ";"
    assert result_name is not None
    return f"const {result_name} = {awaited};"


def _invocation(
    prefix: str,
    binding: SemanticBinding,
    arguments: tuple[str, ...] | None = None,
) -> str:
    selected = (
        tuple(parameter.name for parameter in binding.parameters)
        if arguments is None
        else arguments
    )
    arguments_text = ", ".join(selected)
    return f"{prefix}{binding.name}({arguments_text})"


def _binding_signature(
    prefix: str,
    binding: SemanticBinding,
    names: dict[str, str],
) -> str:
    result = render_semantic_type(binding.result, names)
    if binding.execution is ExecutionMode.ASYNC:
        result = f"Promise<{result}>"
    return f"{prefix}{binding.name}({_parameters(binding.parameters, names)}): {result}"


def _parameters(
    parameters: Iterable[SemanticParameter], names: dict[str, str]
) -> str:
    return ", ".join(
        f"{parameter.name}: {render_semantic_type(parameter.type, names)}"
        for parameter in parameters
    )


def _async_paths(public_name: str, public: PublicApi) -> list[str]:
    paths = [
        f"{public_name}.{binding.name}"
        for binding in public.functions
        if binding.execution is ExecutionMode.ASYNC
    ]
    for item in public.declarations:
        if not isinstance(item, SemanticObjectDeclaration):
            continue
        for method in item.methods:
            if (
                method.capabilities.javascript_public
                and method.execution is ExecutionMode.ASYNC
            ):
                prefix = (
                    f"{public_name}.{item.name}"
                    if method.member_scope is MemberScope.STATIC
                    else _variable(item.name)
                )
                paths.append(f"{prefix}.{method.name}")
    return sorted(paths)


def _mode(binding: SemanticBinding) -> str:
    return "async" if binding.execution is ExecutionMode.ASYNC else "sync"


def _variable(type_name: str) -> str:
    return type_name[:1].lower() + type_name[1:]


def _markdown_text(value: str) -> str:
    """Render a package description as one literal Markdown paragraph."""

    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace("<", "&lt;").replace(">", "&gt;")
    for character in "`*_[]#!":
        escaped = escaped.replace(character, "\\" + character)
    return escaped
