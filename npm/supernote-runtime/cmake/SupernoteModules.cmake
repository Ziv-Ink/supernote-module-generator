# Installed-consumer integration for generated Supernote module packages.
cmake_minimum_required(VERSION 3.24)

function(supernote_configure_runtime runtime_target)
  if(NOT TARGET "${runtime_target}")
    message(FATAL_ERROR "Supernote runtime target does not exist: ${runtime_target}")
  endif()
  get_filename_component(
    _supernote_runtime_root "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/.." ABSOLUTE)
  target_sources(
    "${runtime_target}" PRIVATE
    "${_supernote_runtime_root}/native/src/runtime_services.cpp")
  target_compile_features("${runtime_target}" PUBLIC c_std_23 cxx_std_23)
  target_include_directories(
    "${runtime_target}" PUBLIC
    "${_supernote_runtime_root}/native/src"
    "${_supernote_runtime_root}/native/include")
  unset(_supernote_runtime_root)
endfunction()

function(supernote_link_modules runtime_target compile_target inventory_file)
  if(NOT TARGET "${runtime_target}")
    message(FATAL_ERROR "Supernote runtime target does not exist: ${runtime_target}")
  endif()
  if(NOT TARGET "${compile_target}")
    message(FATAL_ERROR "Supernote compile-contract target does not exist: ${compile_target}")
  endif()

  if(NOT IS_ABSOLUTE "${inventory_file}" OR
     NOT EXISTS "${inventory_file}")
    message(FATAL_ERROR
      "Supernote validated composition inventory is missing: ${inventory_file}")
  endif()
  set_property(
    DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${inventory_file}")
  file(READ "${inventory_file}" _supernote_inventory)

  string(JSON _supernote_count LENGTH "${_supernote_inventory}" modules)
  if(_supernote_count GREATER 0)
    math(EXPR _supernote_last "${_supernote_count} - 1")
    foreach(_supernote_index RANGE 0 ${_supernote_last})
      string(JSON _supernote_cmake GET
             "${_supernote_inventory}" modules ${_supernote_index} cmake)
      string(JSON _supernote_target GET
             "${_supernote_inventory}" modules ${_supernote_index} cmakeTarget)
      string(JSON _supernote_name GET
             "${_supernote_inventory}" modules ${_supernote_index} name)
      get_filename_component(_supernote_cmake_dir
                             "${_supernote_cmake}" DIRECTORY)
      set(SUPERNOTE_RUNTIME_COMPILE_TARGET "${compile_target}")
      set(SUPERNOTE_RUNTIME_NATIVE_ABI "supernote-jsi-v1")
      unset(SUPERNOTE_PACKAGE_FEATURE_TARGET)
      add_subdirectory(
        "${_supernote_cmake_dir}"
        "${CMAKE_CURRENT_BINARY_DIR}/supernote-modules/${_supernote_index}")
      if(NOT TARGET "${_supernote_target}" OR
         NOT SUPERNOTE_PACKAGE_FEATURE_TARGET STREQUAL "${_supernote_target}")
        message(FATAL_ERROR
          "Supernote package target mismatch for ${_supernote_name}")
      endif()
      target_link_libraries(
        "${runtime_target}" PRIVATE "$<LINK_ONLY:${_supernote_target}>")
      unset(SUPERNOTE_PACKAGE_FEATURE_TARGET)
      unset(SUPERNOTE_RUNTIME_NATIVE_ABI)
      unset(SUPERNOTE_RUNTIME_COMPILE_TARGET)
    endforeach()
  endif()
endfunction()
