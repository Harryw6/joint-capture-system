gnome-terminal --window -e 'bash -c "roslaunch p450_experiment g1_aruco_clicked_tracking.launch; exit ; exec bash"' \
--tab -e 'bash -c "sleep 2; roslaunch p450_experiment gimbal_aruco_with_landing.launch; exit ; exec bash"' \

sleep 3
rostopic pub -1 /uav1/prometheus/param_settings prometheus_msgs/ParamSettings "param_name:
- '/video_streaming/mode'
param_value:
- '1'"
