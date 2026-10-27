// Comfort lookup over the generated ASHRAE 55 table (bilinear in temp and RH).
#pragma once
#include "duet/types.h"

namespace duet {

class Comfort {
 public:
  static constexpr int kMaxStep = 6;
  // PMV for the given air temperature, humidity and fan step (0 = off).
  static float pmv(Profile p, float temp_c, float rh, int step);
  // Lowest fan step whose PMV is at or below `limit`; -1 if none.
  static int lowest_step_at_or_below(Profile p, float temp_c, float rh, float limit,
                                     int max_step = kMaxStep);
  static float air_speed(int step);
};

}  // namespace duet
