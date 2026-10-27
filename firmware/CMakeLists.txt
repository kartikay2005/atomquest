cmake_minimum_required(VERSION 3.16)
project(duet_firmware CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -Wno-unused-parameter -fno-exceptions -fno-rtti)

# Control core: identical sources to the ESP32-C3 build.
add_library(duet_core STATIC src/core/comfort.cpp src/core/governor.cpp src/core/controller.cpp)
target_include_directories(duet_core PUBLIC include)

# Shared library the Python twin loads (software in the loop).
add_library(duet_sil SHARED src/sil/sil_api.cpp)
target_link_libraries(duet_sil PRIVATE duet_core)

add_executable(test_core test/test_core.cpp)
target_link_libraries(test_core PRIVATE duet_core)
