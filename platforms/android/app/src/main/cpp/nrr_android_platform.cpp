// nrr_android_platform.cpp — Android platform layer for the NRR power manager.
//
// Upstream gap being filled: runtime/mobile/nrr_power_manager.cpp calls four
// functions under `#ifdef __ANDROID__` --
//
//     android_get_battery_level()   -> float, 0.0..1.0, -1 unknown
//     android_get_battery_status()  -> int,   1 charging, 0 discharging, -1
//     android_get_thermal_headroom()-> float, 0.0 critical .. 1.0 nominal
//     android_is_low_power()        -> int,   1 enabled, 0 disabled
//
// -- but runtime/platform/android/nrr_android.{h,cpp} neither declares nor
// defines any of them (and what it does declare lives in nrr::android, while
// the call sites resolve in nrr::mobile).  Phase 13's Android path therefore
// never compiled.  These are implemented here, in nrr::mobile, from Android
// sysfs -- no JNI, no Context, no extra dependencies.
//
// Deliberate "unknown" behavior: the power manager compares
// thermal_headroom < 0.25f to pick SUSTAINED, so returning -1 there would
// throttle a healthy device.  Unknown thermal state reports 1.0 (nominal),
// and only a real reading can throttle.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

namespace nrr {
namespace mobile {

namespace {

// Read the first line of a sysfs path (trimmed). Empty string when the path
// is absent or unreadable, which is the common case on many devices/emulators.
std::string read_sysfs_line(const char* path) {
    FILE* f = fopen(path, "r");
    if (!f) return std::string();
    char buf[128] = {0};
    const char* got = fgets(buf, sizeof(buf), f);
    fclose(f);
    if (!got) return std::string();
    std::string s(buf);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' ||
                          s.back() == ' ' || s.back() == '\t')) {
        s.pop_back();
    }
    return s;
}

// Walk /sys/class/power_supply/*/type and return the first directory that is
// a real battery, so we do not hard-code "battery" (it is "Battery" on some
// kernels, and a device may expose several supplies).
std::string battery_dir() {
    static const char* kCandidates[] = {
        "/sys/class/power_supply/battery",
        "/sys/class/power_supply/Battery",
        "/sys/class/power_supply/BATT",
        "/sys/class/power_supply/bms",
    };
    for (const char* dir : kCandidates) {
        std::string cap = std::string(dir) + "/capacity";
        FILE* f = fopen(cap.c_str(), "r");
        if (f) {
            fclose(f);
            return dir;
        }
    }
    return std::string();
}

// Highest readable thermal-zone temperature in degrees Celsius, or -1.
// Zone ordering is not meaningful across SoCs, so the max is used.
float max_thermal_zone_c() {
    float worst = -1.0f;
    for (int i = 0; i < 40; ++i) {
        char path[128];
        snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/temp", i);
        std::string raw = read_sysfs_line(path);
        if (raw.empty()) continue;
        float t = static_cast<float>(strtod(raw.c_str(), nullptr));
        if (t <= 0.0f) continue;
        // Most kernels report milli-degrees; some report degrees.
        if (t > 1000.0f) t /= 1000.0f;
        if (t > worst) worst = t;
    }
    return worst;
}

}  // namespace

float android_get_battery_level() {
    const std::string dir = battery_dir();
    if (dir.empty()) return -1.0f;
    std::string raw = read_sysfs_line((dir + "/capacity").c_str());
    if (raw.empty()) return -1.0f;
    float pct = static_cast<float>(strtod(raw.c_str(), nullptr));
    if (pct < 0.0f) return -1.0f;
    if (pct > 100.0f) pct = 100.0f;
    return pct / 100.0f;
}

int android_get_battery_status() {
    const std::string dir = battery_dir();
    if (dir.empty()) return -1;
    std::string s = read_sysfs_line((dir + "/status").c_str());
    if (s.empty()) return -1;
    if (s == "Charging" || s == "Full") return 1;
    if (s == "Discharging") return 0;
    if (s == "Not charging") return 0;
    return -1;
}

float android_get_thermal_headroom() {
    const float t = max_thermal_zone_c();
    if (t < 0.0f) return 1.0f;  // unknown: do not invent thermal pressure
    const float nominal_c = 40.0f;   // below this: full headroom
    const float critical_c = 75.0f;  // at/above this: none
    float h = (critical_c - t) / (critical_c - nominal_c);
    if (h < 0.0f) h = 0.0f;
    if (h > 1.0f) h = 1.0f;
    return h;
}

int android_is_low_power() {
    // PowerManager.isPowerSaveMode() needs a Context and only the Java layer
    // has one. The Kotlin side already surfaces this through its own thermal
    // monitor, so the native runtime reports "unknown" rather than guessing.
    return 0;
}

}  // namespace mobile
}  // namespace nrr
