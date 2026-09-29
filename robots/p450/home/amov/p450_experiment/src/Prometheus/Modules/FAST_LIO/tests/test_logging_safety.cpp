#include <cassert>
#include <ctime>
#include <iostream>
#include <string>

#include "logging_safety.hpp"

int main() {
    using p450_logging::make_log_filename;
    using p450_logging::should_delete_log;

    const std::time_t jan_1970 = 0;
    const std::time_t sep_2026 = 1789920000;
    const std::time_t six_days = 6 * 24 * 60 * 60;

    assert(!should_delete_log(jan_1970, 0, "odometry_19700101_080000.txt", six_days));
    assert(!should_delete_log(sep_2026, 0, "odometry_19700101_080000.txt", six_days));
    assert(should_delete_log(sep_2026, sep_2026 - six_days - 1,
                             "odometry_20260914_080000_boot1234_42.txt", six_days));
    assert(!should_delete_log(sep_2026, sep_2026 - six_days + 1,
                              "odometry_20260916_080000_boot1234_42.txt", six_days));

    const auto first = make_log_filename(sep_2026, "boot1234", 42);
    const auto second = make_log_filename(sep_2026, "boot5678", 42);
    assert(first == "odometry_20260921_000000_boot1234_42.txt");
    assert(first != second);

    std::cout << "logging safety tests passed\n";
}
