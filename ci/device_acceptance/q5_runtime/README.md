# Q5-r7 disposable PluginHost fixture

This scratch-only fixture drives the generated `q5-r7-final-runtime-probe` package through the
real Supernote PluginHost JSI runtime. `feature_v1.cpp` and `App_v1.tsx` leave an
asynchronous completion and nominal receiver alive. They persist only a scalar
generation handle and a private completion-marker path through the fixture's Android
module; no raw JSI object crosses runtimes. The package is then replaced in the same
PluginHost process by the v2 sources.

V2 separately proves global isolation and wrong-family nominal rejection, rejects the
persisted v1 generation handle through newly generated code, and polls the private
marker until the old native worker independently records completion. Only after that
barrier does v2 execute fresh feature status, synchronous, and asynchronous calls. A
persistent callback sentinel must remain absent before and after those calls, so the
old promise cannot silently affect the replacement runtime. The terminal marker also
covers the previously accepted conversion, error, object, JNI, and suspend corpus.

The production plugin template and its build/package/deploy scripts are inputs and are
not modified. Generated output is regenerated only with the frozen installed wheel.
The fixture-only `q5-runtime-autolink` package delegates to the generated runtime package;
its Android module owns only the private barrier files. `settings.gradle` includes the
generated runtime and annotation projects without changing the canonical template or
its scripts.
