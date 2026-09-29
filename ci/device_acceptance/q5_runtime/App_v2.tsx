import React, {useEffect, useState} from 'react';
import {NativeModules, Text, View} from 'react-native';
import Probe, {
  SupernoteError,
  getFeatureStatus,
  isFeatureAvailable,
  isSupernoteRangeError,
  isSupernoteTypeError,
  nativeObjectInfo,
} from 'q5-r7-final-runtime-probe';
import * as Unavailable from './fixtures/unavailable-feature';

type CheckRecord = {
  id: string;
  status: 'pass' | 'fail';
  actual?: unknown;
  message?: string;
};

type ValidationDetails = {
  reason?: string;
  path?: string;
  expected?: string;
  actual?: string;
};

type RuntimeBarrier = {
  markerPath(): Promise<string>;
  readReceiverHandle(): Promise<string | null>;
  readOldCallback(): Promise<string | null>;
  clear(): Promise<boolean>;
};

const Barrier = NativeModules.Q5RuntimeBarrier as RuntimeBarrier;

function emit(prefix: string, payload: object): void {
  console.log(`${prefix} ${JSON.stringify(payload)}`);
}

function assertThat(condition: boolean, message: string): asserts condition {
  if (!condition) {
    throw new Error(message);
  }
}

function normalizeError(error: unknown): ValidationDetails & {name: string; message: string; code?: string} {
  const value = error as ValidationDetails & {name?: string; message?: string; code?: string};
  return {
    name: value?.name ?? typeof error,
    message: value?.message ?? String(error),
    code: value?.code,
    reason: value?.reason,
    path: value?.path,
    expected: value?.expected,
    actual: value?.actual,
  };
}

function rejectedCall(
  callable: {checkArguments: (...args: unknown[]) => {ok: boolean; error?: unknown}; (...args: unknown[]): unknown},
  args: unknown[],
  expectedReason: string,
  expectedKind: 'type' | 'range',
): ReturnType<typeof normalizeError> {
  const checked = callable.checkArguments(...args);
  assertThat(!checked.ok && checked.error !== undefined, 'checkArguments accepted rejected input');
  const helper = normalizeError(checked.error);
  assertThat(helper.reason === expectedReason, `helper reason ${helper.reason} != ${expectedReason}`);
  assertThat(
    expectedKind === 'range'
      ? isSupernoteRangeError(checked.error)
      : isSupernoteTypeError(checked.error),
    `helper error predicate mismatch for ${expectedReason}`,
  );
  let thrown: unknown;
  try {
    callable(...args);
  } catch (error) {
    thrown = error;
  }
  assertThat(thrown !== undefined, 'actual call accepted rejected input');
  const actual = normalizeError(thrown);
  const comparison = JSON.stringify({helper, actual});
  assertThat(actual.reason === helper.reason, `helper/call reason mismatch ${comparison}`);
  assertThat(actual.path === helper.path, `helper/call path mismatch ${comparison}`);
  assertThat(actual.expected === helper.expected, `helper/call expected mismatch ${comparison}`);
  assertThat(actual.actual === helper.actual, `helper/call actual mismatch ${comparison}`);
  assertThat(
    expectedKind === 'range'
      ? isSupernoteRangeError(thrown)
      : isSupernoteTypeError(thrown),
    `actual error predicate mismatch for ${expectedReason}`,
  );
  return actual;
}

async function runChecks(): Promise<CheckRecord[]> {
  const checks: CheckRecord[] = [];
  const check = async (id: string, operation: () => unknown | Promise<unknown>) => {
    try {
      const actual = await operation();
      checks.push({id, status: 'pass', actual});
      emit('Q5_R7_TEST_EVENT', {schema: '1.0', id, status: 'pass', actual});
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      checks.push({id, status: 'fail', message});
      emit('Q5_R7_TEST_EVENT', {schema: '1.0', id, status: 'fail', message});
    }
  };

  await check('real-jsi-runtime-status-lazy-proxy', () => {
    assertThat(isFeatureAvailable(), 'feature is not available');
    assertThat(getFeatureStatus() === 'available', `unexpected status ${getFeatureStatus()}`);
    assertThat(Probe.binaryRevision() === 2, 'v2 native binary did not load');
    assertThat(Unavailable.isFeatureAvailable() === false, 'unregistered feature reported available');
    assertThat(Unavailable.getFeatureStatus() === 'feature-unavailable', 'wrong unregistered status');
    const statusAgain = getFeatureStatus();
    assertThat(statusAgain === 'available', 'main feature status changed');
    return {main: statusAgain, unavailable: Unavailable.getFeatureStatus(), revision: 2};
  });

  await check('cpp-validation-counters-scalar-arity-range', () => {
    Probe.resetCallCount();
    assertThat(Probe.echoInt32.accepts(2147483647), 'int32 max not accepted');
    assertThat(Probe.echoInt32.checkArguments(2147483647).ok, 'int32 max check failed');
    assertThat(Probe.callCount() === 0, 'validation invoked int32 implementation');
    assertThat(Probe.echoInt32(2147483647) === 2147483647, 'int32 max call failed');
    assertThat(Probe.callCount() === 1, 'accepted int32 call count mismatch');
    const range = rejectedCall(Probe.echoInt32 as any, [2147483648], 'OUT_OF_RANGE', 'range');
    const arity = rejectedCall(Probe.echoInt32 as any, [], 'ARITY_MISMATCH', 'type');
    const type = rejectedCall(Probe.echoInt32 as any, ['1'], 'TYPE_MISMATCH', 'type');
    assertThat(Probe.callCount() === 1, 'rejected int32 calls reached implementation');
    assertThat(Probe.echoInt64(-9223372036854775808n) === -9223372036854775808n, 'int64 min failed');
    assertThat(Probe.echoInt64(9223372036854775807n) === 9223372036854775807n, 'int64 max failed');
    assertThat(Probe.callCount() === 3, 'int64 call count mismatch');
    return {range, arity, type, calls: Probe.callCount()};
  });

  await check('cpp-byte-view-copy-and-budget', () => {
    Probe.resetCallCount();
    const backing = new Uint8Array([9, 8, 1, 2, 3, 7]);
    const view = new Uint8Array(backing.buffer, 2, 3);
    const echoed = Probe.echoBytes(view);
    assertThat(echoed !== view, 'byte result reused input container');
    assertThat(Array.from(echoed).join(',') === '1,2,3', 'byte view offset/length was ignored');
    view[0] = 99;
    assertThat(echoed[0] === 1, 'byte result did not retain copied snapshot');
    assertThat(Probe.callCount() === 1, 'byte copy call count mismatch');
    const exact = new Uint8Array(33554432);
    exact[0] = 11;
    exact[exact.length - 1] = 13;
    assertThat(Probe.inspectBytes.checkArguments(exact).ok, 'exact byte limit rejected');
    assertThat(Probe.callCount() === 1, 'byte preflight invoked implementation');
    assertThat(Probe.inspectBytes(exact) === 33554432, 'exact byte limit call failed');
    const over = new Uint8Array(33554433);
    const failure = rejectedCall(Probe.inspectBytes as any, [over], 'LIMIT_EXCEEDED', 'range');
    assertThat(Probe.callCount() === 2, 'over-limit bytes reached implementation');
    return {slice: Array.from(echoed), exact: exact.length, over: over.length, failure, calls: Probe.callCount()};
  });

  await check('cpp-string-collection-nullability-budget', () => {
    Probe.resetCallCount();
    const exactString = 'x'.repeat(8388608);
    assertThat(Probe.inspectString.checkArguments(exactString).ok, 'exact string limit rejected');
    assertThat(Probe.inspectString(exactString) === 8388608, 'exact string limit call failed');
    const stringFailure = rejectedCall(
      Probe.inspectString as any,
      ['x'.repeat(8388609)],
      'LIMIT_EXCEEDED',
      'range',
    );
    const nullable = [null, 'alpha', null, 'omega'];
    assertThat(Probe.inspectNullableStrings(nullable) === 4, 'nullable collection call failed');
    const exactCollection = new Array<string | null>(65536).fill(null);
    assertThat(Probe.inspectNullableStrings.checkArguments(exactCollection).ok, 'exact array limit rejected');
    assertThat(Probe.inspectNullableStrings(exactCollection) === 65536, 'exact array call failed');
    const arrayFailure = rejectedCall(
      Probe.inspectNullableStrings as any,
      [new Array<string | null>(65537).fill(null)],
      'LIMIT_EXCEEDED',
      'range',
    );
    const sparse = new Array<string | null>(1);
    const sparseFailure = rejectedCall(Probe.inspectNullableStrings as any, [sparse], 'TYPE_MISMATCH', 'type');
    assertThat(Probe.callCount() === 3, 'rejected string/collection calls reached implementation');
    return {stringFailure, arrayFailure, sparseFailure, calls: Probe.callCount()};
  });

  await check('cpp-copied-value-enum-nullability', () => {
    Probe.resetCallCount();
    let extraReads = 0;
    const input: any = {
      count: 17,
      label: null,
      bytes: new Uint8Array([4, 5, 6]),
      mode: 'Two',
      tags: [null, 'copied'],
      score: null,
    };
    Object.defineProperty(input, 'extra', {get: () => { extraReads += 1; return 99; }});
    assertThat(Probe.echoPayload.checkArguments(input).ok, 'valid copied value rejected');
    assertThat(Probe.callCount() === 0 && extraReads === 0, 'value preflight invoked code or read extra field');
    const output = Probe.echoPayload(input);
    input.count = 99;
    input.bytes[0] = 99;
    input.tags[1] = 'mutated';
    assertThat(output.count === 17 && output.bytes[0] === 4 && output.tags[1] === 'copied', 'value was not copied');
    assertThat(Probe.lastPayloadCount() === 17 && extraReads === 0, 'native value snapshot changed');
    const missing = {...input};
    delete missing.count;
    const missingFailure = rejectedCall(Probe.echoPayload as any, [missing], 'MISSING_FIELD', 'type');
    const invalidEnum = {...input, label: null, mode: 'Three'};
    const enumFailure = rejectedCall(Probe.echoPayload as any, [invalidEnum], 'INVALID_ENUM', 'type');
    assertThat(Probe.callCount() === 1, 'rejected copied values reached implementation');
    return {output: {count: output.count, label: output.label, bytes: Array.from(output.bytes), mode: output.mode, tags: output.tags, score: output.score}, missingFailure, enumFailure, calls: Probe.callCount()};
  });

  await check('cpp-nominal-returned-only-and-global-isolation', () => {
    Probe.resetCallCount();
    const counter = Probe.ProbeCounter.create(40);
    assertThat(counter.add(2) === 42 && counter.value() === 42, 'counter state failed');
    assertThat(Probe.ProbeCounter.is(counter), 'counter type predicate failed');
    assertThat(Probe.ProbeCounter.check(counter).ok, 'counter type check failed');
    const info = nativeObjectInfo(counter);
    assertThat(info?.originFamily === 'cpp' && info.type.includes('ProbeCounter'), 'counter object info failed');
    const foreign = Probe.ForeignCounter.create(42);
    const nominal = rejectedCall(counter.same as any, [foreign], 'NOMINAL_MISMATCH', 'type');
    assertThat(counter.same(counter), 'same-object identity failed');
    const token = Probe.makeReturnedToken(73);
    assertThat(token.value() === 73 && Probe.ReturnedToken.is(token), 'returned-only object failed');
    assertThat((Probe.ReturnedToken as any).create === undefined, 'returned-only type exposed a constructor');
    const oldReceiver = (globalThis as any).__q5R7OldCounter;
    assertThat(oldReceiver === undefined, 'old runtime receiver leaked into replacement Hermes runtime');
    return {info, nominal, returnedOnly: nativeObjectInfo(token), globalIsolation: 'old-runtime-global-absent'};
  });

  await check('stale-old-generation-handle-rejection', async () => {
    const oldHandle = await Barrier.readReceiverHandle();
    assertThat(
      typeof oldHandle === 'string' && oldHandle.startsWith('q5-receiver-handle:1:'),
      `old receiver handle was not persisted safely: ${String(oldHandle)}`,
    );
    Probe.resetCallCount();
    let thrown: unknown;
    try {
      Probe.requireCurrentReceiverHandle(oldHandle);
    } catch (error) {
      thrown = error;
    }
    const rejection = normalizeError(thrown);
    assertThat(thrown instanceof SupernoteError, 'stale handle was not rejected by generated call');
    assertThat(rejection.code === 'IMPLEMENTATION_ERROR', `unexpected stale-handle code ${rejection.code}`);
    assertThat(rejection.message.includes('STALE_GENERATION_HANDLE'), 'stale-handle marker missing');
    assertThat(rejection.message.includes('expected_revision=2'), 'v2 generation was not enforced');
    assertThat(Probe.callCount() === 1, 'stale-handle oracle did not execute exactly one v2 call');
    return {oldHandle, rejection, calls: Probe.callCount(), rawJsiObjectTransferred: false};
  });

  await check('cpp-async-and-supernote-error', async () => {
    Probe.resetCallCount();
    assertThat(await Probe.asyncEcho(41) === 41, 'native async result mismatch');
    assertThat(Probe.asyncCallCount() === 1, 'native async call count mismatch');
    let thrown: unknown;
    try {
      Probe.throwImplementation();
    } catch (error) {
      thrown = error;
    }
    const normalized = normalizeError(thrown);
    assertThat(thrown instanceof SupernoteError, 'implementation failure was not SupernoteError');
    assertThat(normalized.code === 'IMPLEMENTATION_ERROR', `unexpected implementation code ${normalized.code}`);
    assertThat(normalized.message.includes('q5-r7-implementation-marker'), 'implementation message mismatch');
    const constructed = new SupernoteError('CANCELLED', 'constructed-marker');
    assertThat(constructed.code === 'CANCELLED' && constructed.message === 'constructed-marker', 'SupernoteError constructor failed');
    return {asyncCalls: Probe.asyncCallCount(), implementation: normalized, constructed: {code: constructed.code, message: constructed.message}};
  });

  await check('jvm-real-jsi-jni-validation-counters', () => {
    Probe.resetJvmCallCount();
    assertThat(Probe.jvmEchoInt32.accepts(2147483647), 'JVM int32 max not accepted');
    assertThat(Probe.jvmEchoInt32.checkArguments(2147483647).ok, 'JVM int32 preflight failed');
    assertThat(Probe.jvmCallCount() === 0, 'JVM validation invoked Kotlin adapter');
    assertThat(Probe.jvmEchoInt32(2147483647) === 2147483647, 'JVM int32 call failed');
    const range = rejectedCall(Probe.jvmEchoInt32 as any, [2147483648], 'OUT_OF_RANGE', 'range');
    const type = rejectedCall(Probe.jvmEchoInt32 as any, ['1'], 'TYPE_MISMATCH', 'type');
    assertThat(Probe.jvmCallCount() === 1, 'rejected JVM calls reached Kotlin adapter');
    const backing = new Uint8Array([8, 7, 4, 5, 6, 3]);
    const view = new Uint8Array(backing.buffer, 2, 3);
    const echoed = Probe.jvmEchoBytes(view);
    assertThat(echoed !== view && Array.from(echoed).join(',') === '4,5,6', 'JVM byte view/copy failed');
    view[0] = 99;
    assertThat(echoed[0] === 4, 'JVM byte snapshot was not copied');
    const list = Probe.jvmNullableStrings([null, 'jni', null]);
    assertThat(list.length === 3 && list[1] === 'jni' && list[0] === null, 'JVM nullable list failed');
    assertThat(Probe.jvmMaybe(null) === null && Probe.jvmMaybe('x') === 'x', 'JVM nullable scalar failed');
    return {range, type, bytes: Array.from(echoed), list, calls: Probe.jvmCallCount(), route: 'PluginHost Hermes JSI -> generated JNI -> real KSP adapter -> Kotlin'};
  });

  await check('jvm-suspend-real-jsi-jni', async () => {
    const before = Probe.jvmAsyncCallCount();
    const value = await Probe.jvmSuspend(41);
    assertThat(value === 42, 'JVM suspend result mismatch');
    assertThat(Probe.jvmAsyncCallCount() === before + 1, 'JVM suspend adapter call count mismatch');
    return {value, calls: Probe.jvmAsyncCallCount(), route: 'PluginHost Hermes Promise -> generated JNI coroutine bridge -> Kotlin suspend'};
  });

  await check('replacement-health-after-observed-old-completion', async () => {
    const markerPath = await Barrier.markerPath();
    const waitStarted = Date.now();
    const deadline = waitStarted + 330000;
    let marker = '';
    while (Date.now() < deadline) {
      marker = Probe.completionMarker(markerPath);
      if (marker === 'revision=1;native-work-complete') {
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    assertThat(marker === 'revision=1;native-work-complete', 'old native completion marker timed out');
    await new Promise(resolve => setTimeout(resolve, 1000));
    const callbackBefore = await Barrier.readOldCallback();
    assertThat(callbackBefore === null, `old callback affected persistent state: ${callbackBefore}`);
    const statusBefore = getFeatureStatus();
    const revisionBefore = Probe.binaryRevision();
    const sync = Probe.echoInt32(73);
    const asyncValue = await Probe.asyncEcho(74);
    const revisionAfter = Probe.binaryRevision();
    const statusAfter = getFeatureStatus();
    const callbackAfter = await Barrier.readOldCallback();
    assertThat(statusBefore === 'available' && statusAfter === 'available', 'replacement status changed');
    assertThat(revisionBefore === 2 && revisionAfter === 2, 'replacement revision changed');
    assertThat(sync === 73 && asyncValue === 74, 'post-completion v2 calls returned wrong values');
    assertThat(callbackAfter === null, `old callback affected v2 after health calls: ${callbackAfter}`);
    return {
      marker,
      markerPath,
      observedAfterMs: Date.now() - waitStarted,
      statusBefore,
      statusAfter,
      revisionBefore,
      revisionAfter,
      sync,
      asyncValue,
      oldCallbackBefore: callbackBefore,
      oldCallbackAfter: callbackAfter,
    };
  });

  return checks;
}

function App(): React.JSX.Element {
  const [summary, setSummary] = useState('running-v2');

  useEffect(() => {
    let active = true;
    runChecks().then(
      checks => {
        const failed = checks.filter(item => item.status !== 'pass');
        const result = {
          schema: '1.0',
          suite: 'q5-r7-pluginhost-jsi',
          phase: 'v2-terminal',
          pluginName: 'SnmgQ5R7Auth',
          status: failed.length === 0 ? 'pass' : 'fail',
          passed: checks.length - failed.length,
          total: checks.length,
          checks,
        };
        emit('Q5_R7_TEST_RESULT', result);
        Barrier.clear().then(
          () => emit('Q5_R7_BARRIER_CLEANUP', {status: 'pass'}),
          error => emit('Q5_R7_BARRIER_CLEANUP', {
            status: 'fail',
            message: error instanceof Error ? error.message : String(error),
          }),
        );
        if (active) {
          setSummary(`${result.status}:${result.passed}/${result.total}`);
        }
      },
      error => {
        const message = error instanceof Error ? error.message : String(error);
        emit('Q5_R7_TEST_RESULT', {schema: '1.0', suite: 'q5-r7-pluginhost-jsi', phase: 'v2-terminal', status: 'fail', fatal: message, checks: []});
        if (active) {
          setSummary(`fatal:${message}`);
        }
      },
    );
    return () => {
      active = false;
    };
  }, []);

  return (
    <View>
      <Text testID="q5-r7-result">{summary}</Text>
    </View>
  );
}

export default App;
