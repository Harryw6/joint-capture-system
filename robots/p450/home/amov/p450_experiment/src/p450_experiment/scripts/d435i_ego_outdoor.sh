gnome-terminal --window -e 'bash -c "roslaunch p450_experiment ego_planner_depth_outdoor.launch; exec bash"' \
--tab -e 'bash -c "sleep 6; roslaunch p450_experiment rviz_d435i.launch; exec bash"' \
