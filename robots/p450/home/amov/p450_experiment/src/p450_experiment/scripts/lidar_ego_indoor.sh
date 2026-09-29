gnome-terminal --window -e 'bash -c "roslaunch p450_experiment LDS-50C-3.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 2; roslaunch p450_experiment filter_lidar.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 4; roslaunch p450_experiment scan_to_octomap_indoor.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 6; roslaunch p450_experiment ego_planner_octomap_indoor.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 8; source ~/.bashrc; roslaunch p450_experiment rviz_2dlidar.launch; exit ; exec bash"' \
