#include "duet/comfort.h"

#include "duet/comfort_table.h"

namespace duet {
namespace {

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

float cell(Profile p, int i, int j, int k) {
  const int8_t v = (p == Profile::Sleep) ? comfort::kSleep[i][j][k] : comfort::kEvening[i][j][k];
  return static_cast<float>(v) / comfort::kPmvScale;
}

}  // namespace

float Comfort::pmv(Profile p, float temp_c, float rh, int step) {
  using namespace comfort;
  if (step < 0) step = 0;
  if (step >= kNFan) step = kNFan - 1;
  const float ft = (clampf(temp_c, kTMin, kTMin + (kNT - 1) * kTStep) - kTMin) / kTStep;
  const float fr = (clampf(rh, kRhMin, kRhMin + (kNRh - 1) * kRhStep) - kRhMin) / kRhStep;
  int i0 = static_cast<int>(ft), j0 = static_cast<int>(fr);
  if (i0 >= kNT - 1) i0 = kNT - 2;
  if (j0 >= kNRh - 1) j0 = kNRh - 2;
  const float a = ft - i0, b = fr - j0;
  const float v00 = cell(p, i0, j0, step), v10 = cell(p, i0 + 1, j0, step);
  const float v01 = cell(p, i0, j0 + 1, step), v11 = cell(p, i0 + 1, j0 + 1, step);
  return (1 - a) * (1 - b) * v00 + a * (1 - b) * v10 + (1 - a) * b * v01 + a * b * v11;
}

int Comfort::lowest_step_at_or_below(Profile p, float temp_c, float rh, float limit,
                                     int max_step) {
  for (int s = 0; s <= max_step && s < comfort::kNFan; ++s) {
    if (pmv(p, temp_c, rh, s) <= limit) return s;
  }
  return -1;
}

float Comfort::air_speed(int step) {
  if (step < 0) step = 0;
  if (step >= comfort::kNFan) step = comfort::kNFan - 1;
  return comfort::kFanAirSpeed[step];
}

}  // namespace duet
