gnome-terminal --window -e 'bash -c "sleep 1; roslaunch p450_experiment mid360_to_octomap.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 2; roslaunch p450_experiment ego_planner_basic_mid360.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 3; roslaunch p450_experiment rviz_mid360.launch; exit ; exec bash"' \
