# Installed-consumer @supernote/runtime native component.
cmake_minimum_required(VERSION 3.24)
cmake_policy(SET CMP0131 NEW)
project(sn_supernote_runtime_8e5aabfdb931 LANGUAGES C CXX)

if(NOT DEFINED SUPERNOTE_VARIANT)
  message(FATAL_ERROR "SUPERNOTE_VARIANT must be debug or release")
endif()
if(NOT DEFINED SUPERNOTE_COMPOSITION_DIR OR
   NOT IS_ABSOLUTE "${SUPERNOTE_COMPOSITION_DIR}")
  message(FATAL_ERROR "SUPERNOTE_COMPOSITION_DIR must be an absolute path")
endif()
if(NOT DEFINED SUPERNOTE_COMPOSITION_INVENTORY OR
   NOT IS_ABSOLUTE "${SUPERNOTE_COMPOSITION_INVENTORY}")
  message(FATAL_ERROR "SUPERNOTE_COMPOSITION_INVENTORY must be an absolute path")
endif()

set(_SUPERNOTE_RUNTIME_ROOT "${CMAKE_CURRENT_LIST_DIR}/..")
set(_SUPERNOTE_COMPONENT sn_supernote_runtime_8e5aabfdb931)
set(_SUPERNOTE_REGISTRATION_COMPONENT
    sn_supernote_runtime_8e5aabfdb931_registration)
set(_SUPERNOTE_PLUGIN_BINDINGS
    "${SUPERNOTE_COMPOSITION_DIR}/plugin_bindings.cpp")
set(_SUPERNOTE_FEATURE_REGISTRY
    "${SUPERNOTE_COMPOSITION_DIR}/feature_registry.cpp")
if(NOT EXISTS "${_SUPERNOTE_PLUGIN_BINDINGS}" OR
   NOT EXISTS "${_SUPERNOTE_FEATURE_REGISTRY}")
  message(FATAL_ERROR "Supernote consumer-owned composition sources are missing")
endif()

add_library(${_SUPERNOTE_COMPONENT} SHARED
    "${_SUPERNOTE_RUNTIME_ROOT}/native/src/runtime_bootstrap.cpp"
    "${_SUPERNOTE_FEATURE_REGISTRY}"
    "${_SUPERNOTE_PLUGIN_BINDINGS}")
add_library(${_SUPERNOTE_REGISTRATION_COMPONENT} SHARED
    "${_SUPERNOTE_RUNTIME_ROOT}/native/src/runtime_registration_bridge.c")

find_package(Threads REQUIRED)
find_package(ReactAndroid REQUIRED CONFIG)
find_package(fbjni REQUIRED CONFIG)
set(_SUPERNOTE_COMPILE_TARGET ${_SUPERNOTE_COMPONENT}_compile_contract)
add_library(${_SUPERNOTE_COMPILE_TARGET} INTERFACE)
target_compile_features(
    ${_SUPERNOTE_COMPILE_TARGET} INTERFACE c_std_23 cxx_std_23)
target_include_directories(${_SUPERNOTE_COMPILE_TARGET} INTERFACE
    "${_SUPERNOTE_RUNTIME_ROOT}/native/src"
    "${_SUPERNOTE_RUNTIME_ROOT}/native/include")
target_link_libraries(${_SUPERNOTE_COMPILE_TARGET} INTERFACE
    Threads::Threads
    ReactAndroid::jsi
    ReactAndroid::reactnative
    fbjni::fbjni
    log)

target_compile_features(${_SUPERNOTE_COMPONENT} PRIVATE cxx_std_23)
set_target_properties(${_SUPERNOTE_COMPONENT} PROPERTIES
    C_STANDARD 23
    C_STANDARD_REQUIRED YES
    C_VISIBILITY_PRESET hidden
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN YES)
target_link_options(
    ${_SUPERNOTE_COMPONENT} PRIVATE "-Wl,-Bsymbolic-functions")
target_link_libraries(
    ${_SUPERNOTE_COMPONENT} PRIVATE ${_SUPERNOTE_COMPILE_TARGET})

include("${CMAKE_CURRENT_LIST_DIR}/SupernoteModules.cmake")
supernote_configure_runtime(${_SUPERNOTE_COMPONENT})
supernote_link_modules(
    ${_SUPERNOTE_COMPONENT}
    ${_SUPERNOTE_COMPILE_TARGET}
    "${SUPERNOTE_COMPOSITION_INVENTORY}")

set_target_properties(${_SUPERNOTE_REGISTRATION_COMPONENT} PROPERTIES
    C_STANDARD 23
    C_STANDARD_REQUIRED YES)
target_link_libraries(${_SUPERNOTE_REGISTRATION_COMPONENT} PRIVATE dl log)

unset(_SUPERNOTE_COMPILE_TARGET)
unset(_SUPERNOTE_FEATURE_REGISTRY)
unset(_SUPERNOTE_PLUGIN_BINDINGS)
unset(_SUPERNOTE_REGISTRATION_COMPONENT)
unset(_SUPERNOTE_COMPONENT)
unset(_SUPERNOTE_RUNTIME_ROOT)
