cmake_minimum_required(VERSION 3.18)
project(QuickieParse LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG")

# --- Python + nanobind ------------------------------------------------------
find_package(Python 3.10 COMPONENTS Interpreter Development.Module REQUIRED)

# Discover nanobind's CMake config from the active interpreter.
execute_process(
  COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
  OUTPUT_VARIABLE NB_CMAKE_DIR OUTPUT_STRIP_TRAILING_WHITESPACE)
list(APPEND CMAKE_PREFIX_PATH "${NB_CMAKE_DIR}")
find_package(nanobind CONFIG REQUIRED)

# --- simdjson (system, via pkg-config) --------------------------------------
find_package(PkgConfig REQUIRED)
pkg_check_modules(SIMDJSON REQUIRED simdjson)

# --- the native parser module ----------------------------------------------
nanobind_add_module(QuickieParse STABLE_ABI src/QuickieParse.cpp)
target_include_directories(QuickieParse PRIVATE ${SIMDJSON_INCLUDE_DIRS})
target_link_directories(QuickieParse PRIVATE ${SIMDJSON_LIBRARY_DIRS})
target_link_libraries(QuickieParse PRIVATE ${SIMDJSON_LIBRARIES})

# Drop the built .so next to the Python sources so `import QuickieParse` works
# from the OddsAPI dir without install.
set_target_properties(QuickieParse PROPERTIES
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
