package com.example.device_probe

import android.os.Build
import kotlinx.coroutines.delay
import supernote.generated.annotations.SupernotePluginAsync
import supernote.generated.annotations.SupernotePluginExport

@SupernotePluginExport
fun jvmEcho(value: String): String = "jvm:$value"

@SupernotePluginExport
fun androidModel(): String = Build.MODEL

@SupernotePluginExport
fun androidSdk(): Int = Build.VERSION.SDK_INT

@SupernotePluginExport
@SupernotePluginAsync
suspend fun jvmAsyncEcho(value: String): String {
  delay(20)
  return "jvm-async:$value"
}
