cmake_minimum_required(VERSION 3.25)
project(tradebot LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Phase 1 skeleton (c++.text §9 PHASE 1) + Phase 5 pure-logic leaves.
# Drogon is heavy and only earns its place from Phase 6 (89 routes); keeping
# it optional means the parity harness and the logic tests -- the things that
# gate every later phase -- build in seconds.
option(TB_WITH_HTTP "Build the Drogon HTTP server (Phase 1 /health)" ON)
option(TB_SANITIZE   "Build with ASan+UBSan (invariant I11)"          OFF)
# glaze (used only by the parity harness driver) requires std::expected, which
# libstdc++ gates behind __cpp_concepts >= 202002L -- a value clang 18 does not
# define. The clang leg of CI exists for a sanitiser/UB second opinion on OUR
# code (§11.1), not to vet a third-party JSON library, so it builds with this
# OFF. gcc builds everything.
option(TB_WITH_REPLAY "Build replay_cpp, the parity harness driver (needs glaze)" ON)

# §11.3: pin the microarchitecture. NEVER -march=native -- AWS can move the
# instance to different silicon between reboots and the binary would fault.
set(TB_MARCH "" CACHE STRING "Target microarchitecture, e.g. cascadelake")

# vcpkg ships a CMake config for both; Ubuntu's libre2-dev ships ONLY a
# pkg-config file, so fall back to that rather than making the server depend on
# vcpkg just to build.
find_package(re2 CONFIG QUIET)
if(NOT re2_FOUND)
  find_package(PkgConfig REQUIRED)
  pkg_check_modules(RE2 REQUIRED IMPORTED_TARGET re2)
  add_library(re2::re2 ALIAS PkgConfig::RE2)
endif()

find_package(spdlog CONFIG REQUIRED)

add_library(tb_core STATIC
  src/config.cpp
  src/schemas.cpp
  src/str.cpp
  src/value.cpp
  src/fast_track.cpp
  src/rules_engine.cpp
  src/market_clock.cpp
  src/event_profiles.cpp
)
target_include_directories(tb_core PUBLIC include)
target_link_libraries(tb_core PUBLIC re2::re2 spdlog::spdlog)

if(TB_MARCH)
  target_compile_options(tb_core PUBLIC -march=${TB_MARCH})
endif()

if(TB_SANITIZE)
  target_compile_options(tb_core PUBLIC -fsanitize=address,undefined -fno-omit-frame-pointer -g)
  target_link_options(tb_core PUBLIC -fsanitize=address,undefined)
endif()

# -- parity harness driver (c++.text §10) -------------------------------------
# The single most important artifact in the migration: reads one corpus case
# on stdin as JSON, writes this stack's decision to stdout as JSON.
if(TB_WITH_REPLAY)
find_package(glaze CONFIG QUIET)
if(NOT glaze_FOUND)
  include(FetchContent)
  FetchContent_Declare(glaze
    GIT_REPOSITORY https://github.com/stephenberry/glaze.git
    GIT_TAG v5.5.4
    GIT_SHALLOW TRUE)
  FetchContent_MakeAvailable(glaze)
endif()
add_executable(replay_cpp tools/replay_cpp.cpp)
target_link_libraries(replay_cpp PRIVATE tb_core glaze::glaze)
endif()

# -- tests --------------------------------------------------------------------
# Plain assert-based mains: no framework, no fixtures, runs anywhere, and a
# failure is a non-zero exit code that CI already understands.
enable_testing()
# -UNDEBUG is load-bearing: RelWithDebInfo defines NDEBUG, which compiles
# assert() to nothing -- every test below would then "pass" without checking
# anything at all.
foreach(t test_fast_track test_rules_engine test_market_clock test_sizing test_hist)
  add_executable(${t} tests/${t}.cpp)
  target_link_libraries(${t} PRIVATE tb_core)
  target_compile_options(${t} PRIVATE -UNDEBUG)
  add_test(NAME ${t} COMMAND ${t})
endforeach()

# -- HTTP server (Phase 1 exit criterion: curl :8001/health) ------------------
if(TB_WITH_HTTP)
  find_package(Drogon CONFIG REQUIRED)
  add_executable(tradebot src/main.cpp)
  target_link_libraries(tradebot PRIVATE tb_core Drogon::Drogon)
endif()
