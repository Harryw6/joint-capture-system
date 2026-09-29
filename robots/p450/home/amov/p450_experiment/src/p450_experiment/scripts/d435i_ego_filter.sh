gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment depth_to_octomap_filter.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 4; source ~/.bashrc; roslaunch p450_experiment radius_filter_test.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 4; source ~/.bashrc; roslaunch p450_experiment ego_planner_octomap_filter.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 6; source ~/.bashrc; roslaunch p450_experiment rviz_d435i.launch; exit ; exec bash"' \
