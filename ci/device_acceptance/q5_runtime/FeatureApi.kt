package com.zivink.q5runtimeprobe

import android.util.Log
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.delay
import supernote.generated.annotations.SupernotePluginAsync
import supernote.generated.annotations.SupernotePluginExport

private val jvmCalls = AtomicInteger(0)
private val jvmAsyncCalls = AtomicInteger(0)

@SupernotePluginExport
fun resetJvmCallCount() {
    jvmCalls.set(0)
    jvmAsyncCalls.set(0)
}

@SupernotePluginExport
fun jvmCallCount(): Int = jvmCalls.get()

@SupernotePluginExport
fun jvmAsyncCallCount(): Int = jvmAsyncCalls.get()

@SupernotePluginExport
fun jvmEchoInt32(value: Int): Int {
    jvmCalls.incrementAndGet()
    return value
}

@SupernotePluginExport
fun jvmEchoBytes(value: ByteArray): ByteArray {
    jvmCalls.incrementAndGet()
    return value.copyOf()
}

@SupernotePluginExport
fun jvmNullableStrings(values: List<String?>): List<String?> {
    jvmCalls.incrementAndGet()
    return values.toList()
}

@SupernotePluginExport
fun jvmMaybe(value: String?): String? {
    jvmCalls.incrementAndGet()
    return value
}

@SupernotePluginExport
@SupernotePluginAsync
suspend fun jvmSuspend(value: Int): Int {
    jvmAsyncCalls.incrementAndGet()
    Log.i("Q5R7Jvm", "Q5_R7_JVM_SUSPEND_START value=$value")
    delay(25)
    Log.i("Q5R7Jvm", "Q5_R7_JVM_SUSPEND_END value=$value")
    return value + 1
}
