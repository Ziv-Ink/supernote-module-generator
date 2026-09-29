package com.zivink.q5runtimehost

import com.facebook.react.ReactPackage
import com.facebook.react.bridge.NativeModule
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod
import com.facebook.react.uimanager.ViewManager
import java.io.File
import supernote.generated.runtime.SupernoteModulePackage

class Q5RuntimeBarrierModule(
    reactContext: ReactApplicationContext,
) : ReactContextBaseJavaModule(reactContext) {
    private val root: File = File(reactContext.noBackupFilesDir, "snmg-q5-r7-auth-barrier")
    private val completion: File = File(root, "old-native-complete.txt")
    private val receiverHandle: File = File(root, "old-receiver-handle.txt")
    private val oldCallback: File = File(root, "old-callback.txt")

    override fun getName(): String = "Q5RuntimeBarrier"

    private fun guarded(promise: Promise, operation: () -> Any?) {
        try {
            root.mkdirs()
            promise.resolve(operation())
        } catch (error: Exception) {
            promise.reject("Q5_RUNTIME_BARRIER_IO", error)
        }
    }

    @ReactMethod
    fun reset(promise: Promise) = guarded(promise) {
        completion.delete()
        File(completion.path + ".tmp").delete()
        receiverHandle.delete()
        oldCallback.delete()
        completion.absolutePath
    }

    @ReactMethod
    fun markerPath(promise: Promise) = guarded(promise) {
        completion.absolutePath
    }

    @ReactMethod
    fun writeReceiverHandle(value: String, promise: Promise) = guarded(promise) {
        receiverHandle.writeText(value, Charsets.UTF_8)
        true
    }

    @ReactMethod
    fun readReceiverHandle(promise: Promise) = guarded(promise) {
        if (receiverHandle.isFile) receiverHandle.readText(Charsets.UTF_8) else null
    }

    @ReactMethod
    fun recordOldCallback(value: String, promise: Promise) = guarded(promise) {
        oldCallback.writeText(value, Charsets.UTF_8)
        true
    }

    @ReactMethod
    fun readOldCallback(promise: Promise) = guarded(promise) {
        if (oldCallback.isFile) oldCallback.readText(Charsets.UTF_8) else null
    }

    @ReactMethod
    fun clear(promise: Promise) = guarded(promise) {
        completion.delete()
        File(completion.path + ".tmp").delete()
        receiverHandle.delete()
        oldCallback.delete()
        root.delete()
        true
    }
}

class Q5RuntimeHostPackage : ReactPackage {
    private val delegate = SupernoteModulePackage()

    override fun createNativeModules(
        reactContext: ReactApplicationContext,
    ): List<NativeModule> =
        delegate.createNativeModules(reactContext) + Q5RuntimeBarrierModule(reactContext)

    override fun createViewManagers(
        reactContext: ReactApplicationContext,
    ): List<ViewManager<*, *>> = delegate.createViewManagers(reactContext)
}
