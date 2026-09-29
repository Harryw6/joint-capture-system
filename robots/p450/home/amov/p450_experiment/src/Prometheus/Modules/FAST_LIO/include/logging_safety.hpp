#pragma once

#include <ctime>
#include <iomanip>
#include <sstream>
#include <string>

namespace p450_logging {

inline bool is_valid_wall_clock(std::time_t value) {
    std::tm broken_down{};
    if (localtime_r(&value, &broken_down) == nullptr) {
        return false;
    }
    const int year = broken_down.tm_year + 1900;
    return year >= 2024 && year <= 2099;
}

inline bool filename_has_valid_date(const std::string& filename) {
    constexpr const char* prefix = "odometry_";
    if (filename.compare(0, 9, prefix) != 0 || filename.size() < 13) {
        return false;
    }
    int year = 0;
    for (std::size_t index = 9; index < 13; ++index) {
        const char digit = filename[index];
        if (digit < '0' || digit > '9') {
            return false;
        }
        year = year * 10 + (digit - '0');
    }
    return year >= 2024 && year <= 2099;
}

inline bool should_delete_log(std::time_t now,
                              std::time_t modified,
                              const std::string& filename,
                              std::time_t retention_seconds) {
    return is_valid_wall_clock(now) && filename_has_valid_date(filename) &&
           modified <= now && now - modified > retention_seconds;
}

inline std::string make_log_filename(std::time_t now,
                                     const std::string& boot_id,
                                     long process_id) {
    std::tm broken_down{};
    localtime_r(&now, &broken_down);
    std::ostringstream name;
    name << std::put_time(&broken_down, "odometry_%Y%m%d_%H%M%S_")
         << boot_id << '_' << process_id << ".txt";
    return name.str();
}

}  // namespace p450_logging
