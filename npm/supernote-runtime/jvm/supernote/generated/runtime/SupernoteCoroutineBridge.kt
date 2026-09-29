package supernote.generated.runtime

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

object SupernoteCoroutineBridge {
  private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

  @JvmStatic
  fun launch(completionToken: Long, block: suspend () -> Any?): Job =
      scope.launch {
        try {
          nativeComplete(completionToken, block(), null, false)
        } catch (_: CancellationException) {
          nativeComplete(completionToken, null, null, true)
        } catch (failure: Throwable) {
          nativeComplete(
              completionToken,
              null,
              (failure.message ?: failure.javaClass.name).toByteArray(Charsets.UTF_8),
              false,
          )
        }
      }

  @JvmStatic
  fun cancel(job: Job) {
    job.cancel()
  }

  @JvmStatic
  private external fun nativeComplete(
      completionToken: Long,
      result: Any?,
      failureMessageUtf8: ByteArray?,
      cancelled: Boolean,
  )
}
