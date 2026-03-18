#----------------------------------------------------------------
# Generated CMake target import file.
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "go2_sport_api::go2_sport_api" for configuration ""
set_property(TARGET go2_sport_api::go2_sport_api APPEND PROPERTY IMPORTED_CONFIGURATIONS NOCONFIG)
set_target_properties(go2_sport_api::go2_sport_api PROPERTIES
  IMPORTED_LOCATION_NOCONFIG "${_IMPORT_PREFIX}/lib/libgo2_sport_api.so"
  IMPORTED_SONAME_NOCONFIG "libgo2_sport_api.so"
  )

list(APPEND _IMPORT_CHECK_TARGETS go2_sport_api::go2_sport_api )
list(APPEND _IMPORT_CHECK_FILES_FOR_go2_sport_api::go2_sport_api "${_IMPORT_PREFIX}/lib/libgo2_sport_api.so" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
