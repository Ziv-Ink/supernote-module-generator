'use strict';

const crypto = require('crypto');
const fs = require('fs');
const Module = require('module');
const path = require('path');

const CONTRACT_SCHEMA = '2.0';
const MODULE_KIND = 'supernote_module_distribution';
const MODULE_PROTOCOL = '2.0';
const RUNTIME_KIND = 'supernote_module_runtime';
const RUNTIME_DISTRIBUTION_KIND = 'supernote_runtime_distribution';
const RUNTIME_PROTOCOL = '2.0';
const NATIVE_ABI = 'supernote-jsi-v1';
const REGISTRATION_PATH = '.supernote-generated/android/registration.json';
const CMAKE_PATH = '.supernote-generated/android/CMakeLists.txt';
const JVM_REGISTRATION_PATH = '.supernote-generated/android/jvm/JvmRegistration.java';
const JVM_ADAPTER_PATH = '.supernote-generated/android/jvm/KspAdapters.kt';
const NATIVE_REGISTRATION_HEADER_PATH = '.supernote-generated/android/package_registration.hpp';
const NATIVE_REGISTRATION_SOURCE_PATH = '.supernote-generated/android/package_registration.cpp';

function fail(code, message) {
  const error = new Error(`Supernote package discovery [${code}]: ${message}`);
  error.code = code;
  throw error;
}

function readJson(file, code, label) {
  let bytes;
  try {
    bytes = fs.readFileSync(file, 'utf8');
  } catch (error) {
    fail(code, `${label} is unreadable: ${error.message}`);
  }
  try {
    return JSON.parse(bytes);
  } catch (error) {
    fail(code, `${label} is not valid JSON: ${error.message}`);
  }
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function canonicalRelative(value, code, label) {
  if (typeof value !== 'string' || value.length === 0 || value.includes('\\')) {
    fail(code, `${label} must be a non-empty portable relative path`);
  }
  const normalized = path.posix.normalize(value);
  if (
    normalized !== value ||
    value.startsWith('/') ||
    value === '.' ||
    value === '..' ||
    value.startsWith('../') ||
    /^[A-Za-z]:/.test(value)
  ) {
    fail(code, `${label} must be a canonical relative path`);
  }
  return value;
}

function relationInside(root, candidate) {
  const relation = path.relative(root, candidate);
  return relation !== '..' && !relation.startsWith(`..${path.sep}`) && !path.isAbsolute(relation);
}

function resolvePayload(root, relative, code, label, expectedKind = 'file') {
  canonicalRelative(relative, code, label);
  const lexical = path.resolve(root, ...relative.split('/'));
  if (!relationInside(root, lexical)) {
    fail(code, `${label} escapes its package root`);
  }
  let physical;
  try {
    physical = fs.realpathSync.native(lexical);
  } catch (error) {
    fail(code, `${label} is missing or unreadable: ${error.message}`);
  }
  if (!relationInside(root, physical)) {
    fail(code, `${label} resolves outside its package root`);
  }
  let stat;
  try {
    stat = fs.statSync(physical);
  } catch (error) {
    fail(code, `${label} cannot be inspected: ${error.message}`);
  }
  if (expectedKind === 'file' && !stat.isFile()) {
    fail(code, `${label} must resolve to a regular file`);
  }
  if (expectedKind === 'directory' && !stat.isDirectory()) {
    fail(code, `${label} must resolve to a directory`);
  }
  return physical;
}

function packageCandidates(root, packageName) {
  const requireFromRoot = Module.createRequire(path.join(root, '__supernote_consumer__.cjs'));
  const search = requireFromRoot.resolve.paths(packageName);
  if (!Array.isArray(search)) return [];
  const relative = packageName.startsWith('@') ? packageName.split('/') : [packageName];
  return search.map(base => path.join(base, ...relative));
}

function resolvePackageRoot(root, declaredName) {
  for (const candidate of packageCandidates(root, declaredName)) {
    let stat;
    try {
      stat = fs.statSync(candidate);
    } catch (error) {
      if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) continue;
      fail('SNMG_DEPENDENCY_UNREADABLE', `${declaredName} cannot be inspected: ${error.message}`);
    }
    if (!stat.isDirectory()) continue;
    const physical = fs.realpathSync.native(candidate);
    const packageJsonPath = resolvePayload(
      physical,
      'package.json',
      'SNMG_DEPENDENCY_METADATA_INVALID',
      `${declaredName} package.json`,
    );
    const packageJson = readJson(
      packageJsonPath,
      'SNMG_DEPENDENCY_METADATA_INVALID',
      `${declaredName} package.json`,
    );
    if (!isObject(packageJson) || packageJson.name !== declaredName) {
      fail(
        'SNMG_DEPENDENCY_IDENTITY_MISMATCH',
        `${declaredName} resolved to a package declaring ${JSON.stringify(packageJson && packageJson.name)}`,
      );
    }
    return {root: physical, packageJson, packageJsonPath};
  }
  fail('SNMG_DEPENDENCY_MISSING', `declared direct dependency ${declaredName} is not installed`);
}

function digest(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function requireHashedSourceTree(
  root,
  relative,
  payloadPaths,
  label,
  allowedSourceEntries = new Set(),
) {
  const first = resolvePayload(
    root,
    relative,
    'SNMG_MODULE_REGISTRATION_INVALID',
    label,
    'directory',
  );
  const pending = [{relative, physical: first}];
  const visited = new Set();
  while (pending.length > 0) {
    const current = pending.pop();
    if (visited.has(current.physical)) {
      fail('SNMG_MODULE_REGISTRATION_INVALID', `${label} contains a directory cycle or alias`);
    }
    visited.add(current.physical);
    let children;
    try {
      children = fs.readdirSync(current.physical, {withFileTypes: true});
    } catch (error) {
      fail('SNMG_MODULE_REGISTRATION_INVALID', `${label} is unreadable: ${error.message}`);
    }
    for (const child of children) {
      const childRelative = `${current.relative}/${child.name}`;
      const childPhysical = resolvePayload(
        root,
        childRelative,
        'SNMG_MODULE_REGISTRATION_INVALID',
        `${label} entry ${childRelative}`,
        'any',
      );
      const stat = fs.statSync(childPhysical);
      if (stat.isDirectory()) {
        pending.push({relative: childRelative, physical: childPhysical});
      } else if (stat.isFile()) {
        if (!payloadPaths.has(childRelative) && !allowedSourceEntries.has(childRelative)) {
          fail(
            'SNMG_MODULE_REGISTRATION_INVALID',
            `${label} selects unhashed source file ${childRelative}`,
          );
        }
      } else {
        fail('SNMG_MODULE_REGISTRATION_INVALID', `${label} contains an unsupported entry ${childRelative}`);
      }
    }
  }
}

function validateRuntime(dependency) {
  const metadata = dependency.packageJson.supernoteNativeRuntime;
  if (metadata === undefined) return null;
  if (!isObject(metadata)) {
    fail('SNMG_RUNTIME_METADATA_INVALID', `${dependency.packageJson.name} supernoteNativeRuntime must be an object`);
  }
  const expectedFields = ['kind', 'manifest', 'nativeAbi', 'protocol', 'schemaVersion'];
  if (Object.keys(metadata).sort().join('\0') !== expectedFields.join('\0')) {
    fail('SNMG_RUNTIME_METADATA_INVALID', `${dependency.packageJson.name} runtime metadata fields are invalid`);
  }
  if (metadata.schemaVersion !== CONTRACT_SCHEMA || metadata.kind !== RUNTIME_KIND) {
    fail('SNMG_RUNTIME_METADATA_INVALID', `${dependency.packageJson.name} runtime metadata identity is unsupported`);
  }
  if (metadata.protocol !== RUNTIME_PROTOCOL) {
    fail(
      'SNMG_RUNTIME_INCOMPATIBLE',
      `${dependency.packageJson.name} runtime protocol ${JSON.stringify(metadata.protocol)} is incompatible with ${RUNTIME_PROTOCOL}`,
    );
  }
  if (metadata.nativeAbi !== NATIVE_ABI) {
    fail(
      'SNMG_RUNTIME_ABI_INCOMPATIBLE',
      `${dependency.packageJson.name} native ABI ${JSON.stringify(metadata.nativeAbi)} is incompatible with ${NATIVE_ABI}`,
    );
  }
  const manifestRelative = canonicalRelative(
    metadata.manifest,
    'SNMG_RUNTIME_METADATA_INVALID',
    `${dependency.packageJson.name} runtime manifest`,
  );
  const manifestPath = resolvePayload(
    dependency.root,
    manifestRelative,
    'SNMG_RUNTIME_PAYLOAD_INVALID',
    `${dependency.packageJson.name} runtime manifest`,
  );
  const manifest = readJson(
    manifestPath,
    'SNMG_RUNTIME_PAYLOAD_INVALID',
    `${dependency.packageJson.name} runtime manifest`,
  );
  const expectedManifestFields = ['kind', 'nativeAbi', 'payload', 'protocol', 'schemaVersion'];
  if (
    !isObject(manifest) ||
    Object.keys(manifest).sort().join('\0') !== expectedManifestFields.join('\0') ||
    manifest.schemaVersion !== CONTRACT_SCHEMA ||
    manifest.kind !== RUNTIME_DISTRIBUTION_KIND ||
    manifest.protocol !== metadata.protocol ||
    manifest.nativeAbi !== metadata.nativeAbi ||
    !Array.isArray(manifest.payload) ||
    manifest.payload.length === 0
  ) {
    fail('SNMG_RUNTIME_PAYLOAD_INVALID', `${dependency.packageJson.name} runtime payload manifest is invalid`);
  }
  const payload = [];
  const payloadPaths = new Set();
  for (const [index, entry] of manifest.payload.entries()) {
    if (
      !isObject(entry) ||
      Object.keys(entry).sort().join('\0') !== ['path', 'sha256'].join('\0') ||
      typeof entry.sha256 !== 'string' ||
      !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) {
      fail('SNMG_RUNTIME_PAYLOAD_INVALID', `${dependency.packageJson.name} runtime payload[${index}] is invalid`);
    }
    const relative = canonicalRelative(
      entry.path,
      'SNMG_RUNTIME_PAYLOAD_INVALID',
      `${dependency.packageJson.name} runtime payload[${index}] path`,
    );
    if (payloadPaths.has(relative)) {
      fail('SNMG_RUNTIME_PAYLOAD_INVALID', `${dependency.packageJson.name} runtime payload repeats ${relative}`);
    }
    payloadPaths.add(relative);
    const physical = resolvePayload(
      dependency.root,
      relative,
      'SNMG_RUNTIME_PAYLOAD_INVALID',
      `${dependency.packageJson.name} runtime payload ${relative}`,
    );
    const actual = digest(physical);
    if (actual !== entry.sha256) {
      fail(
        'SNMG_RUNTIME_PAYLOAD_STALE',
        `${dependency.packageJson.name} runtime payload ${relative} has digest ${actual}, expected ${entry.sha256}`,
      );
    }
    payload.push({path: relative, sha256: entry.sha256, physical});
  }
  const required = [
    'native/include/supernote/conversion.hpp',
    'native/include/supernote/cpp_objects.hpp',
    'native/include/supernote/runtime.hpp',
    'native/src/runtime_services.hpp',
    'native/src/runtime_services.cpp',
    'native/src/runtime_bootstrap.cpp',
    'native/src/runtime_registration_bridge.c',
    'android/src/main/java/supernote/generated/runtime/SupernoteModule.kt',
    'jvm/supernote/generated/runtime/SupernoteCoroutineBridge.kt',
    'jvm/supernote/generated/runtime/SupernoteConversionBudget.kt',
    'android/build.gradle',
    'cmake/CMakeLists.txt',
    'cmake/SupernoteModules.cmake',
    'compose-packages.js',
    'consumer-rules.pro',
    'gradle/supernote-modules.gradle',
    'resolve-packages.js',
  ];
  for (const relative of required) {
    if (!payloadPaths.has(relative)) {
      fail('SNMG_RUNTIME_PAYLOAD_INVALID', `${dependency.packageJson.name} runtime payload omits ${relative}`);
    }
  }
  requireHashedSourceTree(
    dependency.root,
    'native',
    payloadPaths,
    `${dependency.packageJson.name} native runtime tree`,
  );
  for (const relative of ['android', 'cmake', 'gradle', 'jvm']) {
    requireHashedSourceTree(
      dependency.root,
      relative,
      payloadPaths,
      `${dependency.packageJson.name} ${relative} runtime tree`,
    );
  }
  return {
    name: dependency.packageJson.name,
    version: dependency.packageJson.version,
    protocol: metadata.protocol,
    nativeAbi: metadata.nativeAbi,
    manifest: manifestPath,
    payload,
    native: {
      source: resolvePayload(
        dependency.root,
        'native/src/runtime_services.cpp',
        'SNMG_RUNTIME_PAYLOAD_INVALID',
        `${dependency.packageJson.name} runtime source`,
      ),
      sourceRoot: resolvePayload(
        dependency.root,
        'native/src',
        'SNMG_RUNTIME_PAYLOAD_INVALID',
        `${dependency.packageJson.name} runtime source root`,
        'directory',
      ),
      includeRoot: resolvePayload(
        dependency.root,
        'native/include',
        'SNMG_RUNTIME_PAYLOAD_INVALID',
        `${dependency.packageJson.name} runtime include root`,
        'directory',
      ),
    },
    root: dependency.root,
  };
}

function validateString(value, code, label) {
  if (typeof value !== 'string' || value.length === 0) fail(code, `${label} must be a non-empty string`);
  return value;
}

function validateModule(dependency) {
  const packageMetadata = dependency.packageJson.supernoteNativeModule;
  if (packageMetadata === undefined) return null;
  if (!isObject(packageMetadata)) {
    fail('SNMG_MODULE_METADATA_INVALID', `${dependency.packageJson.name} supernoteNativeModule must be an object`);
  }
  const expectedMetadataFields = ['kind', 'manifest', 'protocol', 'schemaVersion'];
  if (Object.keys(packageMetadata).sort().join('\0') !== expectedMetadataFields.join('\0')) {
    fail('SNMG_MODULE_METADATA_INVALID', `${dependency.packageJson.name} module metadata fields are invalid`);
  }
  if (
    packageMetadata.schemaVersion !== CONTRACT_SCHEMA ||
    packageMetadata.kind !== MODULE_KIND ||
    packageMetadata.protocol !== MODULE_PROTOCOL
  ) {
    fail('SNMG_MODULE_INCOMPATIBLE', `${dependency.packageJson.name} module metadata identity or protocol is unsupported`);
  }
  const authoredPath = resolvePayload(
    dependency.root,
    '.supernote-module.json',
    'SNMG_MODULE_METADATA_INVALID',
    `${dependency.packageJson.name} authored module metadata`,
  );
  const authored = readJson(
    authoredPath,
    'SNMG_MODULE_METADATA_INVALID',
    `${dependency.packageJson.name} .supernote-module.json`,
  );
  if (!isObject(authored) || authored.kind !== 'supernote_module_feature' || authored.schema_version !== '1.0') {
    fail('SNMG_MODULE_METADATA_INVALID', `${dependency.packageJson.name} authored module metadata identity is unsupported`);
  }
  if (authored.npm_name !== dependency.packageJson.name) {
    fail('SNMG_MODULE_IDENTITY_MISMATCH', `${dependency.packageJson.name} authored npm identity disagrees`);
  }
  if (authored.package_version !== dependency.packageJson.version) {
    fail('SNMG_MODULE_IDENTITY_MISMATCH', `${dependency.packageJson.name} authored package version disagrees`);
  }

  const distributionPath = resolvePayload(
    dependency.root,
    packageMetadata.manifest,
    'SNMG_MODULE_PAYLOAD_MISSING',
    `${dependency.packageJson.name} distribution manifest`,
  );
  const distribution = readJson(
    distributionPath,
    'SNMG_MODULE_PAYLOAD_INVALID',
    `${dependency.packageJson.name} distribution manifest`,
  );
  if (!isObject(distribution) || distribution.schemaVersion !== CONTRACT_SCHEMA || distribution.kind !== MODULE_KIND) {
    fail('SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} distribution metadata identity is unsupported`);
  }
  for (const field of ['featureId', 'packageName', 'packageVersion', 'publicName', 'androidNamespace', 'protocol']) {
    validateString(distribution[field], 'SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} ${field}`);
  }
  if (
    distribution.packageName !== dependency.packageJson.name ||
    distribution.packageVersion !== dependency.packageJson.version ||
    distribution.featureId !== authored.feature_id ||
    distribution.publicName !== authored.public_name ||
    distribution.androidNamespace !== authored.android_namespace
  ) {
    fail('SNMG_MODULE_IDENTITY_MISMATCH', `${dependency.packageJson.name} authored and generated identities disagree`);
  }
  if (distribution.protocol !== MODULE_PROTOCOL) {
    fail(
      'SNMG_MODULE_INCOMPATIBLE',
      `${dependency.packageJson.name} module protocol ${JSON.stringify(distribution.protocol)} is incompatible with ${MODULE_PROTOCOL}`,
    );
  }
  if (!Array.isArray(distribution.payload) || distribution.payload.length === 0) {
    fail('SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} payload inventory must be non-empty`);
  }
  const payload = distribution.payload.map((entry, index) => {
    if (!isObject(entry) || typeof entry.path !== 'string' || !/^[0-9a-f]{64}$/.test(entry.sha256 || '')) {
      fail('SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} payload[${index}] is invalid`);
    }
    const physical = resolvePayload(
      dependency.root,
      entry.path,
      'SNMG_MODULE_PAYLOAD_INVALID',
      `${dependency.packageJson.name} payload[${index}]`,
    );
    const actual = digest(physical);
    if (actual !== entry.sha256) {
      fail('SNMG_MODULE_PAYLOAD_STALE', `${dependency.packageJson.name} payload ${entry.path} has digest ${actual}, expected ${entry.sha256}`);
    }
    return {path: entry.path, physical, sha256: actual};
  });
  const payloadPaths = new Set();
  for (const entry of payload) {
    if (payloadPaths.has(entry.path)) {
      fail('SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} payload repeats ${entry.path}`);
    }
    payloadPaths.add(entry.path);
  }
  const requiredPayload = [
    'package.json',
    '.supernote-module.json',
    '.supernote-generated/index.js',
    '.supernote-generated/index.d.ts',
    '.supernote-generated/android/semantic.json',
    '.supernote-generated/android/conversion.json',
    CMAKE_PATH,
    '.supernote-generated/android/feature.cpp',
    '.supernote-generated/android/internal.cpp',
    '.supernote-generated/android/internal.hpp',
    JVM_REGISTRATION_PATH,
    NATIVE_REGISTRATION_HEADER_PATH,
    NATIVE_REGISTRATION_SOURCE_PATH,
    REGISTRATION_PATH,
  ];
  for (const required of requiredPayload) {
    if (!payloadPaths.has(required)) {
      fail('SNMG_MODULE_PAYLOAD_INVALID', `${dependency.packageJson.name} payload omits required file ${required}`);
    }
  }
  canonicalRelative(
    distribution.registration,
    'SNMG_MODULE_PAYLOAD_INVALID',
    `${dependency.packageJson.name} registration`,
  );
  if (distribution.registration !== REGISTRATION_PATH) {
    fail(
      'SNMG_MODULE_PAYLOAD_INVALID',
      `${dependency.packageJson.name} registration must be ${REGISTRATION_PATH}`,
    );
  }
  if (!payloadPaths.has(distribution.registration)) {
    fail(
      'SNMG_MODULE_PAYLOAD_INVALID',
      `${dependency.packageJson.name} selected registration is absent from the hashed payload`,
    );
  }
  const registrationPath = resolvePayload(
    dependency.root,
    distribution.registration,
    'SNMG_MODULE_PAYLOAD_INVALID',
    `${dependency.packageJson.name} registration`,
  );
  const registration = readJson(
    registrationPath,
    'SNMG_MODULE_PAYLOAD_INVALID',
    `${dependency.packageJson.name} registration`,
  );
  const expectedRegistrationFields = [
    'androidNamespace',
    'bindingHeaders',
    'bindingSources',
    'cmake',
    'cmakeTarget',
    'conversion',
    'featureId',
    'installers',
    'jvmRegistrationSources',
    'jvmSourceDirs',
    'kind',
    'nativeAbi',
    'nativeSourceDirs',
    'packageName',
    'protocol',
    'publicName',
    'schemaVersion',
    'semantic',
  ];
  if (
    !isObject(registration) ||
    Object.keys(registration).sort().join('\0') !== expectedRegistrationFields.join('\0') ||
    registration.kind !== 'supernote_module_registration' ||
    registration.nativeAbi !== NATIVE_ABI ||
    registration.schemaVersion !== CONTRACT_SCHEMA ||
    registration.protocol !== MODULE_PROTOCOL ||
    registration.featureId !== distribution.featureId ||
    registration.packageName !== distribution.packageName ||
    registration.publicName !== distribution.publicName ||
    registration.androidNamespace !== distribution.androidNamespace ||
    !Array.isArray(registration.nativeSourceDirs) ||
    !Array.isArray(registration.jvmSourceDirs) ||
    !Array.isArray(registration.jvmRegistrationSources) ||
    !Array.isArray(registration.bindingSources) ||
    !Array.isArray(registration.bindingHeaders) ||
    !isObject(registration.installers) ||
    registration.cmake !== CMAKE_PATH ||
    typeof registration.cmakeTarget !== 'string' ||
    !/^supernote_feature_[0-9a-f]{16}$/.test(registration.cmakeTarget) ||
    registration.semantic !== '.supernote-generated/android/semantic.json' ||
    registration.conversion !== '.supernote-generated/android/conversion.json'
  ) {
    fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} registration identity or fields disagree`);
  }
  const suffix = registration.cmakeTarget.replace('supernote_feature_', '');
  const expectedNativeInstaller = `supernote_package_install_native_${suffix}`;
  const expectedJvmInstaller = `supernote_package_install_jvm_${suffix}`;
  const installerFields = ['source', 'symbol'];
  const nativeInstaller = registration.installers.native;
  const jvmInstaller = registration.installers.jvm;
  if (
    Object.keys(registration.installers).sort().join('\0') !== ['jvm', 'native'].join('\0') ||
    !isObject(nativeInstaller) ||
    Object.keys(nativeInstaller).sort().join('\0') !== installerFields.join('\0') ||
    nativeInstaller.source !== NATIVE_REGISTRATION_SOURCE_PATH ||
    nativeInstaller.symbol !== expectedNativeInstaller ||
    (jvmInstaller !== null &&
      (!isObject(jvmInstaller) ||
        Object.keys(jvmInstaller).sort().join('\0') !== installerFields.join('\0') ||
        jvmInstaller.source !== NATIVE_REGISTRATION_SOURCE_PATH ||
        jvmInstaller.symbol !== expectedJvmInstaller))
  ) {
    fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} callable installer metadata disagrees`);
  }
  const requiredBindingSources = [
    '.supernote-generated/android/feature.cpp',
    '.supernote-generated/android/internal.cpp',
    NATIVE_REGISTRATION_SOURCE_PATH,
  ];
  const hasJvmBinding = payloadPaths.has('.supernote-generated/android/jvm_feature.cpp');
  const expectedJvmRegistrationSources = [JVM_REGISTRATION_PATH];
  if (hasJvmBinding) {
    expectedJvmRegistrationSources.push(JVM_ADAPTER_PATH);
  }
  if (hasJvmBinding) {
    requiredBindingSources.push('.supernote-generated/android/jvm_feature.cpp');
  }
  if (hasJvmBinding && registration.jvmSourceDirs.length === 0) {
    fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} JVM binding has no JVM source directory`);
  }
  if (hasJvmBinding !== (jvmInstaller !== null)) {
    fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} JVM installer does not match its generated binding`);
  }
  if (
    registration.bindingSources.join('\0') !== requiredBindingSources.join('\0') ||
    registration.bindingHeaders.join('\0') !== [
      '.supernote-generated/android/internal.hpp',
      NATIVE_REGISTRATION_HEADER_PATH,
    ].join('\0') ||
    registration.jvmRegistrationSources.join('\0') !==
      expectedJvmRegistrationSources.join('\0')
  ) {
    fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} required binding inventory disagrees`);
  }
  for (const [field, entries] of [
    ['bindingSources', registration.bindingSources],
    ['bindingHeaders', registration.bindingHeaders],
    ['jvmRegistrationSources', registration.jvmRegistrationSources],
  ]) {
    for (const [index, relative] of entries.entries()) {
      canonicalRelative(relative, 'SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} ${field}[${index}]`);
      if (!payloadPaths.has(relative)) {
        fail('SNMG_MODULE_REGISTRATION_INVALID', `${dependency.packageJson.name} ${field}[${index}] is absent from the hashed payload`);
      }
    }
  }
  const jvmRegistrationSources = registration.jvmRegistrationSources.map(
    (relative, index) => resolvePayload(
      dependency.root,
      relative,
      'SNMG_MODULE_REGISTRATION_INVALID',
      `${dependency.packageJson.name} jvmRegistrationSources[${index}]`,
    ),
  );
  const allowedJvmRegistrationEntries = new Set(
    jvmRegistrationSources.map(physical =>
      path.relative(dependency.root, physical).split(path.sep).join('/')),
  );
  const generatedJvmSourceDirs = new Set(
    registration.jvmRegistrationSources.map(relative => path.posix.dirname(relative)),
  );
  for (const physical of jvmRegistrationSources) {
    const nativeRelative = path.relative(dependency.root, path.dirname(physical));
    if (nativeRelative === '') {
      fail(
        'SNMG_MODULE_REGISTRATION_INVALID',
        `${dependency.packageJson.name} generated JVM source directory must not resolve to the package root`,
      );
    }
    generatedJvmSourceDirs.add(nativeRelative.split(path.sep).join('/'));
  }
  for (const relative of [...generatedJvmSourceDirs].sort()) {
    requireHashedSourceTree(
      dependency.root,
      relative,
      payloadPaths,
      `${dependency.packageJson.name} generated JVM source directory ${relative}`,
      allowedJvmRegistrationEntries,
    );
  }
  for (const [field, entries] of [
    ['nativeSourceDirs', registration.nativeSourceDirs],
    ['jvmSourceDirs', registration.jvmSourceDirs],
  ]) {
    for (const [index, relative] of entries.entries()) {
      requireHashedSourceTree(
        dependency.root,
        relative,
        payloadPaths,
        `${dependency.packageJson.name} ${field}[${index}]`,
      );
    }
  }
  const cmake = resolvePayload(
    dependency.root,
    registration.cmake,
    'SNMG_MODULE_REGISTRATION_INVALID',
    `${dependency.packageJson.name} cmake`,
  );
  const jvmSourceDirs = registration.jvmSourceDirs.map((relative, index) =>
    resolvePayload(
      dependency.root,
      relative,
      'SNMG_MODULE_REGISTRATION_INVALID',
      `${dependency.packageJson.name} jvmSourceDirs[${index}]`,
      'directory',
    ));
  return {
    name: dependency.packageJson.name,
    version: dependency.packageJson.version,
    featureId: distribution.featureId,
    publicName: distribution.publicName,
    androidNamespace: distribution.androidNamespace,
    protocol: distribution.protocol,
    root: dependency.root,
    manifest: distributionPath,
    registration: registrationPath,
    cmake,
    cmakeTarget: registration.cmakeTarget,
    installers: {
      native: nativeInstaller,
      jvm: jvmInstaller,
    },
    jvmSourceDirs,
    jvmRegistrationSources,
    payload,
  };
}

function rejectConflict(map, key, value, code, label) {
  const normalized = key.toLocaleLowerCase('en-US');
  const previous = map.get(normalized);
  if (previous !== undefined) fail(code, `${label} ${JSON.stringify(key)} conflicts in ${previous} and ${value}`);
  map.set(normalized, value);
}

function discover(consumerRoot) {
  const root = fs.realpathSync.native(consumerRoot);
  if (fs.existsSync(path.join(root, '.pnp.cjs')) && !fs.existsSync(path.join(root, 'node_modules'))) {
    fail('SNMG_YARN_PNP_UNSUPPORTED', 'Yarn Plug\'n\'Play is unsupported; configure nodeLinker: node-modules');
  }
  const consumer = readJson(path.join(root, 'package.json'), 'SNMG_CONSUMER_METADATA_INVALID', 'consumer package.json');
  const dependencies = consumer.dependencies === undefined ? {} : consumer.dependencies;
  if (!isObject(dependencies)) {
    fail('SNMG_CONSUMER_METADATA_INVALID', 'consumer package.json dependencies must be an object');
  }

  const modules = [];
  const runtimes = [];
  const ignored = [];
  const physical = new Map();
  for (const name of Object.keys(dependencies).sort()) {
    const dependency = resolvePackageRoot(root, name);
    const prior = physical.get(dependency.root);
    if (prior !== undefined && prior !== name) {
      fail('SNMG_DEPENDENCY_ALIAS_CONFLICT', `${prior} and ${name} resolve to the same physical package`);
    }
    physical.set(dependency.root, name);
    const runtime = validateRuntime(dependency);
    if (runtime !== null) {
      runtimes.push(runtime);
      continue;
    }
    const module = validateModule(dependency);
    if (module !== null) modules.push(module);
    else ignored.push(name);
  }

  if (runtimes.length > 1) {
    fail('SNMG_RUNTIME_CONFLICT', `multiple direct Supernote runtimes are installed: ${runtimes.map(item => item.name).join(', ')}`);
  }
  if (modules.length > 0 && runtimes.length === 0) {
    fail('SNMG_RUNTIME_MISSING', 'Supernote modules require one compatible runtime declared as a direct dependency');
  }
  const featureIds = new Map();
  const publicNames = new Map();
  const namespaces = new Map();
  const cmakeTargets = new Map();
  for (const module of modules) {
    rejectConflict(featureIds, module.featureId, module.name, 'SNMG_MODULE_DUPLICATE_FEATURE', 'feature identity');
    rejectConflict(publicNames, module.publicName, module.name, 'SNMG_MODULE_PUBLIC_NAME_CONFLICT', 'JavaScript public name');
    rejectConflict(namespaces, module.androidNamespace, module.name, 'SNMG_MODULE_ANDROID_NAMESPACE_CONFLICT', 'Android namespace');
    rejectConflict(cmakeTargets, module.cmakeTarget, module.name, 'SNMG_MODULE_CMAKE_TARGET_CONFLICT', 'CMake target');
  }
  return {
    schemaVersion: CONTRACT_SCHEMA,
    runtime: runtimes.length === 1 ? runtimes[0] : null,
    modules,
    ignored,
  };
}

if (require.main === module) {
  if (process.argv.length !== 3) {
    process.stderr.write('usage: node resolve-packages.js <consumer-root>\n');
    process.exit(2);
  }
  try {
    process.stdout.write(`${JSON.stringify(discover(process.argv[2]))}\n`);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(1);
  }
}

module.exports = {discover};
