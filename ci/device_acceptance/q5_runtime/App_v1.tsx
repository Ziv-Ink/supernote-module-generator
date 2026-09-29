import React, {useEffect, useState} from 'react';
import {NativeModules, Text, View} from 'react-native';
import Probe, {
  getFeatureStatus,
  isFeatureAvailable,
  nativeObjectInfo,
} from 'q5-r7-final-runtime-probe';

type GlobalWithOldCounter = typeof globalThis & {
  __q5R7OldCounter?: unknown;
};

type RuntimeBarrier = {
  reset(): Promise<string>;
  writeReceiverHandle(value: string): Promise<boolean>;
  recordOldCallback(value: string): Promise<boolean>;
};

const Barrier = NativeModules.Q5RuntimeBarrier as RuntimeBarrier;

function emit(prefix: string, payload: object): void {
  console.log(`${prefix} ${JSON.stringify(payload)}`);
}

function App(): React.JSX.Element {
  const [summary, setSummary] = useState('starting-v1');

  useEffect(() => {
    let active = true;
    const run = async () => {
      if (!isFeatureAvailable() || getFeatureStatus() !== 'available') {
        throw new Error(`feature unavailable: ${getFeatureStatus()}`);
      }
      const revision = Probe.binaryRevision();
      if (revision !== 1) {
        throw new Error(`expected v1 native payload, got ${revision}`);
      }
      const markerPath = await Barrier.reset();
      const counter = Probe.ProbeCounter.create(41);
      if (counter.add(1) !== 42) {
        throw new Error('v1 counter did not reach 42');
      }
      (globalThis as GlobalWithOldCounter).__q5R7OldCounter = counter;
      const receiverHandle = Probe.receiverHandle(counter);
      await Barrier.writeReceiverHandle(receiverHandle);
      const info = nativeObjectInfo(counter);
      const creatorThreadId = counter.creatorThreadId().toString();
      Probe.delayedRevision(300000, markerPath).then(
        async value => {
          await Barrier.recordOldCallback(`resolved:${value}`);
          emit('Q5_R7_OLD_PROMISE_RESOLVED', {value});
        },
        async error => {
          const message = error instanceof Error ? error.message : String(error);
          await Barrier.recordOldCallback(`rejected:${message}`);
          emit('Q5_R7_OLD_PROMISE_REJECTED', {message});
        },
      );
      const result = {
        schema: '1.0',
        suite: 'q5-r7-pluginhost-jsi',
        phase: 'v1-ready',
        revision,
        status: getFeatureStatus(),
        info,
        creatorThreadId,
        delayedRevisionStarted: true,
        markerPath,
        receiverHandle,
      };
      emit('Q5_R7_V1_READY', result);
      if (active) {
        setSummary('v1-ready');
      }
    };
    run().catch(error => {
      const message = error instanceof Error ? error.message : String(error);
      emit('Q5_R7_V1_FATAL', {message});
      if (active) {
        setSummary(`v1-fatal:${message}`);
      }
    });
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
