cmake_minimum_required(VERSION 3.16)
project(duet_firmware CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
if(MSVC)
  add_compile_options(/W3 /GR- /D_CRT_SECURE_NO_WARNINGS)
else()
  add_compile_options(-Wall -Wextra -Wno-unused-parameter -fno-exceptions -fno-rtti)
endif()

# Control core: identical sources to the ESP32-C3 build.
add_library(duet_core STATIC src/core/comfort.cpp src/core/governor.cpp src/core/controller.cpp)
target_include_directories(duet_core PUBLIC include)

# Shared library the Python twin loads (software in the loop).
add_library(duet_sil SHARED src/sil/sil_api.cpp)
target_link_libraries(duet_sil PRIVATE duet_core)
set_target_properties(duet_sil PROPERTIES PREFIX "lib" CXX_VISIBILITY_PRESET hidden)

# Unit tests for the core.
add_executable(test_core test/test_core.cpp)
target_link_libraries(test_core PRIVATE duet_core)

# Put the library and test binary directly in the build folder on every generator
# (Visual Studio would otherwise use build/Release/).
foreach(t duet_sil test_core)
  set_target_properties(${t} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    RUNTIME_OUTPUT_DIRECTORY_RELEASE ${CMAKE_BINARY_DIR}
    RUNTIME_OUTPUT_DIRECTORY_DEBUG ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY_RELEASE ${CMAKE_BINARY_DIR}
    LIBRARY_OUTPUT_DIRECTORY_DEBUG ${CMAKE_BINARY_DIR})
endforeach()

if(MINGW)
  # Self-contained DLL and test binary for people without a compiler.
  target_link_options(duet_sil PRIVATE -static -static-libgcc -static-libstdc++)
  target_link_options(test_core PRIVATE -static -static-libgcc -static-libstdc++)
endif()
