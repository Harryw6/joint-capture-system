gnome-terminal --window -e 'bash -c "source ~/.bashrc; roslaunch p450_experiment ego_planner_depth_indoor.launch; exec bash"' \
--tab -e 'bash -c "sleep 6; source ~/.bashrc; roslaunch p450_experiment rviz_d435i.launch; exec bash"' \
