import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from supernote_module_generator.feature_generator import FeatureConfig, generate_feature, stage_feature
from supernote_module_generator.feature_model import StarterFamily


def config(tmp_path: Path, *starters: StarterFamily) -> FeatureConfig:
    return FeatureConfig(
        output=tmp_path / "document",
        npm_name="@local/document",
        package_version="2.0.0-dev.0",
        android_namespace="com.example.document",
        public_name="Document",
        starters=starters or (StarterFamily.NATIVE,),
    )


def test_native_and_jvm_starters_create_user_source_without_backend_metadata(tmp_path: Path):
    feature = generate_feature(
        config(tmp_path, StarterFamily.NATIVE, StarterFamily.JVM)
    )
    metadata = json.loads((feature / ".supernote-module.json").read_text())

    assert (feature / "android/src/main/cpp/feature.cpp").is_file()
    assert (
        feature
        / "android/src/main/java/com/example/document/FeatureApi.kt"
    ).is_file()
    assert metadata["implementation_roots"] == {
        "native": "android/src/main/cpp",
        "jvm": "android/src/main/java",
    }
    assert "backend" not in metadata
    assert "type" not in metadata
    assert "starters" not in metadata
    assert not (feature / "android/build.gradle.kts").exists()
    assert not (feature / "android/CMakeLists.txt").exists()


def test_update_preserves_added_and_deleted_user_starter_files(tmp_path: Path):
    feature = generate_feature(config(tmp_path, StarterFamily.NATIVE))
    starter = feature / "android/src/main/cpp/feature.cpp"
    starter.unlink()
    custom = feature / "android/src/main/java/com/example/document/Custom.java"
    custom.parent.mkdir(parents=True)
    custom.write_text("class Custom {}\n")

    staged = stage_feature(
        config(tmp_path, StarterFamily.NATIVE), preserve_sources_from=feature
    )
    assert not (staged / "android/src/main/cpp/feature.cpp").exists()
    assert (staged / custom.relative_to(feature)).read_text() == "class Custom {}\n"


def test_update_preserves_last_generated_types_until_common_codegen_runs(tmp_path: Path):
    feature = generate_feature(config(tmp_path, StarterFamily.JVM))
    generated_types = "export interface DocumentFeature { pageCount(): number; }\n"
    (feature / "index.d.ts").write_text(generated_types, encoding="utf-8")

    staged = stage_feature(
        config(tmp_path, StarterFamily.JVM), preserve_sources_from=feature
    )

    assert (staged / "index.d.ts").read_text(encoding="utf-8") == generated_types


def test_initial_feature_readme_explains_import_generation_and_source_ownership(
    tmp_path: Path,
):
    feature = generate_feature(config(tmp_path))
    readme = (feature / "README.md").read_text(encoding="utf-8")
    package = json.loads((feature / "package.json").read_text())

    assert "import Document from '@local/document';" in readme
    assert "No JavaScript-public declarations are currently generated" in readme
    assert "C/C++: `android/src/main/cpp/`" in readme
    assert "sn-module-gen update @local/document" in readme
    assert "replace this README and `index.d.ts`" in readme
    assert "preserve the C++, Kotlin, and Java implementation source" in readme
    assert "README.md" in package["files"]


def test_feature_package_uses_shared_runtime_proxy_and_no_native_package(tmp_path: Path):
    feature = generate_feature(config(tmp_path))
    index = (feature / "index.js").read_text()
    package = json.loads((feature / "package.json").read_text())

    assert "globalThis.__supernoteModule" in index
    assert index.startswith("/* global globalThis */\n")
    assert "if (property === ERROR_CONSTRUCTOR_PROPERTY) return" not in index
    assert "{...descriptor, configurable: true}" in index
    assert '"supernote:feature:' not in index
    assert "runtime.feature(" in index
    assert "export function isFeatureAvailable()" in index
    assert "export function getFeatureStatus()" in index
    assert "export function nativeObjectInfo(value)" in index
    assert "if (current.status !== 'available') {" in index
    assert "if (typeof inspect !== 'function') {" in index
    assert "if (info !== undefined) {" in index
    assert "if (current.status !== 'available') return" not in index
    assert "if (typeof inspect !== 'function') continue" not in index
    assert "if (info !== undefined) return" not in index
    assert "__supernoteCppObjectInfo" in index
    assert "__supernoteJvmObjectInfo" in index
    assert "new Proxy(" in index
    assert package["main"] == "index.js"
    assert "react-native" not in package


def test_feature_package_imports_before_runtime_install_and_resolves_lazily(
    tmp_path: Path,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the generated ES module")

    feature = generate_feature(config(tmp_path))
    index = (feature / "index.js").read_text(encoding="utf-8")
    encoded = base64.b64encode(index.encode("utf-8")).decode("ascii")
    feature_id = json.loads(
        (feature / ".supernote-module.json").read_text(encoding="utf-8")
    )["feature_id"]
    script = f"""
const generated = await import('data:text/javascript;base64,{encoded}');

if (generated.getFeatureStatus() !== 'runtime-unavailable' ||
    generated.isFeatureAvailable()) {{
  throw new Error('missing runtime availability was reported incorrectly');
}}
if (generated.nativeObjectInfo({{}}) !== undefined) {{
  throw new Error('object inspection should be absent without a runtime');
}}

let earlyError;
try {{
  generated.default.greet;
}} catch (error) {{
  earlyError = error;
}}
if (!earlyError || earlyError.message !==
    'Document is not installed in the Supernote generated runtime') {{
  throw new Error(`unexpected early-access result: ${{earlyError}}`);
}}

const nativeValue = {{native: true}};
const first = {{
  firstOnly: 1,
  greet: name => `first:${{name}}`,
  __supernoteCppObjectInfo(value) {{
    return value === nativeValue
      ? {{type: 'Stroke', originFamily: 'cpp'}}
      : undefined;
  }},
}};
globalThis.__supernoteModule = {{
  feature(id) {{
    if (id !== {json.dumps(feature_id)}) throw new Error(`wrong id: ${{id}}`);
    return first;
  }},
}};
if (generated.default.greet('Ada') !== 'first:Ada') {{
  throw new Error('feature did not resolve after runtime installation');
}}
if (!generated.isFeatureAvailable() ||
    generated.getFeatureStatus() !== 'available') {{
  throw new Error('installed feature availability was reported incorrectly');
}}
const info = generated.nativeObjectInfo(nativeValue);
if (!info || info.type !== 'Stroke' || info.originFamily !== 'cpp') {{
  throw new Error(`unexpected native object information: ${{JSON.stringify(info)}}`);
}}
if (first.__supernoteErrorConstructor !== generated.SupernoteError) {{
  throw new Error('SupernoteError constructor was not installed on the feature');
}}
if (Object.prototype.propertyIsEnumerable.call(
      first,
      '__supernoteErrorConstructor',
    )) {{
  throw new Error('SupernoteError constructor was enumerable on the feature');
}}

if (!('greet' in generated.default) || 'missing' in generated.default) {{
  throw new Error('feature membership did not reflect the current feature');
}}
const firstKeys = Reflect.ownKeys(generated.default);
if (!firstKeys.includes('greet') || !firstKeys.includes('firstOnly')) {{
  throw new Error(`feature keys were not forwarded: ${{firstKeys}}`);
}}
if (firstKeys.includes('__supernoteErrorConstructor')) {{
  throw new Error('internal error constructor leaked through feature keys');
}}
if (firstKeys.includes('__supernoteCppObjectInfo') ||
    '__supernoteCppObjectInfo' in generated.default ||
    generated.default.__supernoteCppObjectInfo !== undefined) {{
  throw new Error('internal object inspector leaked through the feature proxy');
}}
const greetDescriptor = Object.getOwnPropertyDescriptor(
  generated.default,
  'greet',
);
if (!greetDescriptor || greetDescriptor.value !== first.greet ||
    greetDescriptor.configurable !== true) {{
  throw new Error('feature property descriptor was not forwarded safely');
}}
if (generated.default.__supernoteErrorConstructor !== undefined ||
    '__supernoteErrorConstructor' in generated.default ||
    Object.getOwnPropertyDescriptor(
      generated.default,
      '__supernoteErrorConstructor',
    ) !== undefined) {{
  throw new Error('internal error constructor was visible through the proxy');
}}

const second = {{greet: name => `second:${{name}}`, secondOnly: 2}};
globalThis.__supernoteModule = {{feature: () => second}};
if (generated.default.greet('Ada') !== 'second:Ada') {{
  throw new Error('feature wrapper retained a stale runtime binding');
}}
if ('firstOnly' in generated.default || !('secondOnly' in generated.default)) {{
  throw new Error('feature membership retained a stale runtime binding');
}}
const secondKeys = Reflect.ownKeys(generated.default);
if (secondKeys.includes('firstOnly') || !secondKeys.includes('secondOnly')) {{
  throw new Error(`feature keys retained a stale runtime binding: ${{secondKeys}}`);
}}
if (second.__supernoteErrorConstructor !== generated.SupernoteError) {{
  throw new Error('SupernoteError constructor was not installed on replacement');
}}

const typeError = new TypeError('bad');
Object.assign(typeError, {{
  reason: 'TYPE_MISMATCH',
  path: 'Drawing.stroke',
  expected: 'Stroke',
  actual: 'object',
}});
if (!generated.isSupernoteTypeError(typeError) ||
    generated.isSupernoteRangeError(typeError) ||
    generated.isSupernoteTypeError(new TypeError('plain'))) {{
  throw new Error('validation error guards returned the wrong result');
}}
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
